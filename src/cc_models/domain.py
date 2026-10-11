"""Core domain types for cc-models.

Pure dataclasses and validation. No I/O here: catalog fetching, settings
editing and local state persistence live in sibling modules.

Named data shape (per .agents/notes/implementation-contract.md):

- Catalog: validated entries keyed by exact raw model ID, bound to one
  endpoint identity plus a fetch timestamp. Duplicate raw IDs are deduped
  without mutating the ID strings.
- Selection: an ordered list of PickerOption (raw model ID, user label,
  optional description, plus any imported optional option fields preserved
  verbatim). ``behavesAs`` is never generated; it only survives when it was
  imported.
- DefaultEdit: an explicit ``preserve`` or ``set(<selected ID>)`` intent.
  An unset default is never implicitly deleted.
- Missing: derived from catalog membership. Missing selected options stay in
  the selection, stay in the generated picker, and are flagged in preview.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "http://localhost:2317"

RESERVED_OPTION_KEYS = ("model", "label", "description")

# Officially documented modelPicker option keys (settings-reference#modelPicker).
# ``model``/``label``/``description`` are first-class fields; only documented
# optional extras may be persisted in the local selection.
DOCUMENTED_OPTION_KEYS = ("model", "label", "description", "behavesAs")
# Extras persisted in local state: documented keys only. Unknown imported
# option fields are never persisted; they survive an apply via merge-by-raw-ID
# from the settings snapshot (see settings.compute_new_state).
PERSISTED_OPTION_EXTRA_KEYS = ("behavesAs",)


class CoreError(Exception):
    """Base class for all cc-models core errors."""


class EndpointError(CoreError):
    """Raised when a base URL violates the endpoint safety rules."""


class NetworkError(CoreError):
    """Raised for non-2xx responses, redirects, timeouts and connection failures."""


class CatalogError(CoreError):
    """Raised when a catalog payload does not validate."""


class ConfigError(CoreError):
    """Raised for malformed settings files, nonobject roots and symlink targets."""


class DriftError(CoreError):
    """Raised when the settings file changed, is missing or was deleted mid-apply."""


class BackupError(CoreError):
    """Raised when a private backup cannot be created (original is left unchanged)."""


class SelectionError(CoreError):
    """Raised for invalid selection shapes (duplicates, dangling defaults, empty apply)."""


class StateError(CoreError):
    """Raised for malformed or unsafe local state files."""


class RestoreRefusedError(CoreError):
    """Raised when a whole-snapshot restore is attempted without explicit confirmation."""


# --------------------------------------------------------------------------
# Strict JSON parsing (shared by settings and local state)
# --------------------------------------------------------------------------


def _reject_constant(name: str) -> object:
    raise ConfigError(f"nonstandard JSON constant not allowed: {name}")


def _object_pairs(pairs: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ConfigError(f"duplicate JSON key not allowed: {key}")
        obj[key] = value
    return obj


def parse_strict_json(text: str) -> object:
    """Strict JSON: malformed text, duplicate keys and NaN/Infinity are refused."""
    try:
        return json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"malformed JSON: {exc}") from exc


def _is_loopback_host(host: str) -> bool:
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_endpoint(raw: str | None, allow_insecure_http: bool = False) -> str:
    """Normalize a CPA base URL to an endpoint identity ending in ``/v1``.

    Rules (contract):
    - empty/None falls back to the default ``http://localhost:2317``;
    - credentials (user:pass@), query strings and fragments are rejected;
    - HTTPS is required for non-loopback hosts. Plain HTTP for a remote host
      is permitted only with the explicit ``allow_insecure_http=True`` opt-in
      (persisted in local state, default false); loopback HTTP is always
      allowed;
    - the path is normalized so the identity ends with exactly ``/v1``. The
      opt-in flag is not part of the endpoint identity.
    """
    value = (raw or "").strip() or DEFAULT_BASE_URL
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise EndpointError(f"endpoint scheme must be http or https, got: {value!r}")
    if parts.username or parts.password:
        raise EndpointError("endpoint must not embed credentials")
    if parts.query or parts.fragment:
        raise EndpointError("endpoint must not contain a query or fragment")
    host = parts.hostname or ""
    if not host:
        raise EndpointError(f"endpoint has no host: {value!r}")
    if parts.scheme == "http" and not _is_loopback_host(host) and not allow_insecure_http:
        raise EndpointError(
            "HTTP is only allowed for loopback hosts; use HTTPS or pass the explicit "
            "allow_insecure_http opt-in"
        )
    path = parts.path
    while path.endswith("/"):
        path = path[:-1]
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return f"{parts.scheme}://{parts.netloc}{path}"


def models_url(endpoint: str) -> str:
    """The catalog URL for a normalized endpoint identity."""
    return f"{normalize_endpoint(endpoint)}/models"


@dataclass(frozen=True)
class PickerOption:
    """One ordered picker entry: exact raw ID plus user label.

    ``extra`` holds imported optional per-option fields verbatim. We never
    generate ``behavesAs`` or any other optional field; extras only exist
    because they were imported from settings or state.
    """

    model: str
    label: str
    description: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise SelectionError("option model must be a nonempty string")
        if not isinstance(self.label, str) or not self.label:
            raise SelectionError("option label must be a nonempty string")
        if self.description is not None and not isinstance(self.description, str):
            raise SelectionError("option description must be a string or null")
        if not isinstance(self.extra, dict):
            raise SelectionError("option extra must be an object")
        # Store a frozen view: mutable extras must not gain reserved keys after
        # construction (P1: post-construction mutation cannot inject model/label).
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))
        self._validate_extra(self.extra)

    @staticmethod
    def _validate_extra(extra) -> None:
        for key in RESERVED_OPTION_KEYS:
            if key in extra:
                raise SelectionError(f"option extra must not override reserved key: {key}")
        for key in extra:
            if key not in PERSISTED_OPTION_EXTRA_KEYS:
                raise SelectionError(
                    f"option extra key is not a documented modelPicker field: {key!r}"
                )

    def to_settings(self) -> dict:
        # Revalidate at the serialization boundary: defense in depth against
        # any post-construction tampering with extras.
        self._validate_extra(self.extra)
        out: dict = {"model": self.model, "label": self.label}
        if self.description is not None:
            out["description"] = self.description
        out.update(dict(self.extra))
        if out["model"] != self.model or out["label"] != self.label:
            raise SelectionError("option serialization diverged from validated fields")
        return out

    @classmethod
    def from_settings(cls, obj: object) -> PickerOption:
        if not isinstance(obj, dict):
            raise SelectionError("picker option must be a JSON object")
        missing = [key for key in ("model", "label") if key not in obj]
        if missing:
            raise SelectionError(f"picker option missing required keys: {', '.join(missing)}")
        description = obj.get("description")
        # Only documented optional fields are persisted; unknown imported fields
        # (potential credential carriers) are dropped here and preserved during
        # apply via merge-by-raw-ID from the original settings snapshot.
        extra = {
            k: v
            for k, v in obj.items()
            if k in PERSISTED_OPTION_EXTRA_KEYS and k not in RESERVED_OPTION_KEYS
        }
        return cls(
            model=obj["model"],
            label=obj["label"],
            description=description,
            extra=extra,
        )


@dataclass(frozen=True)
class DefaultEdit:
    """Explicit default-model edit intent: ``preserve`` or ``set(<selected ID>)``."""

    kind: str
    value: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("preserve", "set"):
            raise SelectionError(f"unknown default edit kind: {self.kind!r}")
        if self.kind == "set" and (not isinstance(self.value, str) or not self.value):
            raise SelectionError("default edit 'set' requires a nonempty model ID")


PRESERVE_DEFAULT = DefaultEdit(kind="preserve")


@dataclass
class Selection:
    """Ordered picker selection plus the explicit default edit intent.

    ``replace_built_in`` is an optional explicit override for the picker's
    ``replaceBuiltInOptions`` toggle: ``None`` (default) keeps the imported
    file value (or true when the file does not set it), ``True``/``False``
    force the written value.
    """

    options: list[PickerOption] = field(default_factory=list)
    default_edit: DefaultEdit = PRESERVE_DEFAULT
    replace_built_in: bool | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        seen: set[str] = set()
        for option in self.options:
            if not isinstance(option, PickerOption):
                raise SelectionError("selection options must be PickerOption instances")
            # Revalidate each option at the boundary (P1 defense in depth).
            if not isinstance(option.model, str) or not option.model:
                raise SelectionError("option model must be a nonempty string")
            if not isinstance(option.label, str) or not option.label:
                raise SelectionError("option label must be a nonempty string")
            PickerOption._validate_extra(option.extra)
            if option.model in seen:
                raise SelectionError(f"duplicate selection ID: {option.model}")
            seen.add(option.model)
        if self.default_edit.kind == "set" and self.default_edit.value not in seen:
            raise SelectionError(
                f"default edit points at ID outside the selection: {self.default_edit.value}"
            )
        if self.replace_built_in is not None and not isinstance(self.replace_built_in, bool):
            raise SelectionError("replace_built_in must be a boolean or null")

    @property
    def ids(self) -> list[str]:
        return [option.model for option in self.options]

    def to_json(self) -> dict:
        self.validate()
        out: dict = {
            "default_edit": {"kind": self.default_edit.kind, "value": self.default_edit.value},
            "options": [option.to_settings() for option in self.options],
        }
        if self.replace_built_in is not None:
            out["replace_built_in"] = self.replace_built_in
        return out

    @classmethod
    def from_json(cls, obj: object) -> Selection:
        if not isinstance(obj, dict):
            raise SelectionError("selection must be a JSON object")
        options_obj = obj.get("options")
        if not isinstance(options_obj, list):
            raise SelectionError("selection options must be a list")
        options = [PickerOption.from_settings(item) for item in options_obj]
        edit_obj = obj.get("default_edit", {"kind": "preserve"})
        if not isinstance(edit_obj, dict) or "kind" not in edit_obj:
            raise SelectionError("selection default_edit must be an object with a kind")
        default_edit = DefaultEdit(kind=edit_obj["kind"], value=edit_obj.get("value"))
        selection = cls(options=options, default_edit=default_edit)
        if "replace_built_in" in obj:
            selection.replace_built_in = obj["replace_built_in"]
        selection.validate()
        return selection


@dataclass
class Catalog:
    """Validated catalog entries keyed by exact raw ID, bound to one endpoint."""

    endpoint: str
    fetched_at: str
    model_ids: list[str] = field(default_factory=list)
    duplicates: dict[str, int] = field(default_factory=dict)  # raw ID -> extra occurrences

    def __post_init__(self) -> None:
        self._id_set = frozenset(self.model_ids)
        if len(self._id_set) != len(self.model_ids):
            raise CatalogError("catalog model_ids must already be deduplicated")

    def contains(self, model_id: str) -> bool:
        return model_id in self._id_set

    def missing_from(self, selection: Selection) -> list[str]:
        """Selected IDs that are not in this catalog, in selection order."""
        return [model_id for model_id in selection.ids if not self.contains(model_id)]

    @classmethod
    def from_response(cls, endpoint: str, fetched_at: str, payload: object) -> Catalog:
        if not isinstance(payload, dict):
            raise CatalogError("catalog payload root must be a JSON object")
        data = payload.get("data")
        if not isinstance(data, list):
            raise CatalogError("catalog payload must contain a 'data' list")
        model_ids: list[str] = []
        duplicates: dict[str, int] = {}
        for entry in data:
            if not isinstance(entry, dict):
                raise CatalogError("catalog data entries must be JSON objects")
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                raise CatalogError("catalog data entries must carry a nonempty string 'id'")
            if model_id in model_ids:
                duplicates[model_id] = duplicates.get(model_id, 1) + 1
            else:
                model_ids.append(model_id)
        return cls(
            endpoint=endpoint,
            fetched_at=fetched_at,
            model_ids=model_ids,
            duplicates=duplicates,
        )
