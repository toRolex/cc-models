"""Local state persistence: selection intent, endpoint config and catalog cache.

Local state never contains tokens or credentials. The state file holds an
explicit schema only:

- ``version``: currently 1;
- ``config``: endpoint configuration — the configured endpoint, the explicit
  ``allow_insecure_http`` opt-in (default false; required for remote plain
  HTTP) and a ``token_env`` variable *name* reference (never a value);
- ``selection``: ordered picker options (with preserved extras) plus the
  explicit default edit intent;
- ``cache``: last successful fetch bound to its normalized endpoint identity,
  usable offline only for that same endpoint.

A malformed or endpoint-mismatched cache never discards valid selection
intent: it is dropped with a warning while the selection is kept.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .domain import (
    Catalog,
    EndpointError,
    Selection,
    StateError,
    normalize_endpoint,
    parse_strict_json,
)

STATE_FILENAME = "state.json"
STATE_VERSION = 1
DEFAULT_TOKEN_ENV = "CPA_API_KEY"


@dataclass(frozen=True)
class EndpointConfig:
    """Endpoint configuration persisted in local state (no token values).

    ``endpoint`` is the configured base URL; ``allow_insecure_http`` is the
    explicit opt-in required before a remote plain-HTTP endpoint is used
    (loopback HTTP never needs it); ``token_env`` names the environment
    variable carrying the API token — the token value itself is never stored.

    The endpoint is validated and normalized at construction: URLs embedding
    credentials, a query or a fragment are refused, and a remote plain-HTTP
    endpoint is refused without the explicit opt-in — so a credential-bearing
    URL can never be persisted to local state.
    """

    endpoint: str | None = None
    allow_insecure_http: bool = False
    token_env: str = DEFAULT_TOKEN_ENV

    def __post_init__(self) -> None:
        if not isinstance(self.allow_insecure_http, bool):
            raise ValueError("config allow_insecure_http must be a boolean")
        if not isinstance(self.token_env, str) or not self.token_env:
            raise ValueError("config token_env must be a nonempty variable name")
        if self.endpoint is not None:
            if not isinstance(self.endpoint, str) or not self.endpoint:
                raise ValueError("config endpoint must be a nonempty string or null")
            # Raises EndpointError for credentials/query/fragment and for
            # remote plain HTTP without the opt-in. Store the normalized form.
            object.__setattr__(
                self,
                "endpoint",
                normalize_endpoint(self.endpoint, allow_insecure_http=self.allow_insecure_http),
            )

    def to_json(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "allow_insecure_http": self.allow_insecure_http,
            "token_env": self.token_env,
        }

    @classmethod
    def from_json(cls, obj: object) -> EndpointConfig:
        if obj is None:
            return cls()
        if not isinstance(obj, dict):
            raise ValueError("config must be an object")
        unknown = set(obj) - {"endpoint", "allow_insecure_http", "token_env"}
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        endpoint = obj.get("endpoint")
        if endpoint is not None and (not isinstance(endpoint, str) or not endpoint):
            raise ValueError("config endpoint must be a nonempty string or null")
        allow = obj.get("allow_insecure_http", False)
        if not isinstance(allow, bool):
            raise ValueError("config allow_insecure_http must be a boolean")
        token_env = obj.get("token_env", DEFAULT_TOKEN_ENV)
        if not isinstance(token_env, str) or not token_env:
            raise ValueError("config token_env must be a nonempty variable name")
        return cls(endpoint=endpoint, allow_insecure_http=allow, token_env=token_env)


@dataclass(frozen=True)
class CachedCatalog:
    """A cached fetch bound to one endpoint identity."""

    endpoint: str
    fetched_at: str
    model_ids: list[str]
    duplicates: dict[str, int] = field(default_factory=dict)


@dataclass
class LocalState:
    selection: Selection = field(default_factory=Selection)
    cache: CachedCatalog | None = None
    config: EndpointConfig = field(default_factory=EndpointConfig)
    warnings: list[str] = field(default_factory=list)


def state_path(state_dir: str | os.PathLike[str]) -> Path:
    return Path(state_dir) / STATE_FILENAME


def _validate_cache(obj: object) -> CachedCatalog | None:
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise ValueError("cache must be an object")
    endpoint = obj.get("endpoint")
    model_ids = obj.get("model_ids")
    fetched_at = obj.get("fetched_at")
    duplicates = obj.get("duplicates", {})
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("cache endpoint must be a nonempty string")
    if not isinstance(fetched_at, str):
        raise ValueError("cache fetched_at must be a string")
    if not isinstance(model_ids, list) or not all(isinstance(i, str) and i for i in model_ids):
        raise ValueError("cache model_ids must be a list of nonempty strings")
    if len(set(model_ids)) != len(model_ids):
        raise ValueError("cache model_ids must be deduplicated")
    if not isinstance(duplicates, dict) or not all(
        isinstance(k, str) and isinstance(v, int) for k, v in duplicates.items()
    ):
        raise ValueError("cache duplicates must map strings to integers")
    return CachedCatalog(
        endpoint=endpoint,
        fetched_at=fetched_at,
        model_ids=list(model_ids),
        duplicates=dict(duplicates),
    )


def load_state(state_dir: str | os.PathLike[str]) -> LocalState:
    """Load local state; missing state yields an empty state.

    A malformed state file or selection raises StateError (never silently
    discarded). A malformed cache is dropped with a warning while a valid
    selection intent is preserved.
    """
    path = state_path(state_dir)
    if path.is_symlink():
        raise StateError(f"refusing symlink state file: {path}")
    if not path.exists():
        return LocalState()
    text = path.read_text(encoding="utf-8")
    try:
        data = parse_strict_json(text)
    except Exception as exc:  # ConfigError
        raise StateError(f"malformed local state: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError("local state root must be a JSON object")
    if data.get("version") != STATE_VERSION:
        raise StateError(f"unsupported local state version: {data.get('version')!r}")
    if not set(data) <= {"version", "selection", "cache", "config"}:
        raise StateError(
            "unexpected keys in local state: "
            f"{sorted(set(data) - {'version', 'selection', 'cache', 'config'})}"
        )

    selection_obj = data.get("selection")
    if selection_obj is None:
        selection = Selection()
    else:
        try:
            selection = Selection.from_json(selection_obj)
        except Exception as exc:
            raise StateError(f"invalid selection in local state: {exc}") from exc

    warnings: list[str] = []
    cache: CachedCatalog | None = None
    if "cache" in data:
        try:
            cache = _validate_cache(data["cache"])
        except ValueError as exc:
            warnings.append(f"dropped invalid cache, selection preserved: {exc}")
            cache = None

    try:
        config = EndpointConfig.from_json(data.get("config"))
    except (ValueError, EndpointError) as exc:
        raise StateError(f"invalid endpoint config in local state: {exc}") from exc

    return LocalState(selection=selection, cache=cache, config=config, warnings=warnings)


def save_state(state_dir: str | os.PathLike[str], state: LocalState) -> Path:
    """Persist local state atomically as a 0600 file in a 0700 directory."""
    directory = Path(state_dir)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    payload: dict = {"version": STATE_VERSION}
    if (
        state.selection.options
        or state.selection.default_edit.kind != "preserve"
        or state.selection.replace_built_in is not None
    ):
        payload["selection"] = state.selection.to_json()
    if state.cache is not None:
        payload["cache"] = {
            "endpoint": state.cache.endpoint,
            "fetched_at": state.cache.fetched_at,
            "model_ids": list(state.cache.model_ids),
            "duplicates": dict(state.cache.duplicates),
        }
    if state.config != EndpointConfig():
        payload["config"] = state.config.to_json()
    content = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    path = state_path(directory)
    fd, temp_name = tempfile.mkstemp(dir=str(directory), prefix=".cc-models-state-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def cache_for_endpoint(
    state: LocalState, endpoint: str, allow_insecure_http: bool = False
) -> Catalog | None:
    """Return the cached catalog only when the endpoint identity matches.

    Offline use of the cache is refused for any other endpoint; the cache is
    also normalized through Catalog so its invariants are re-validated. A
    remote plain-HTTP endpoint requires the same explicit opt-in here as on
    the network path.
    """
    from .domain import normalize_endpoint

    if state.cache is None:
        return None
    if state.cache.endpoint != normalize_endpoint(
        endpoint, allow_insecure_http=allow_insecure_http
    ):
        return None
    return Catalog(
        endpoint=state.cache.endpoint,
        fetched_at=state.cache.fetched_at,
        model_ids=list(state.cache.model_ids),
        duplicates=dict(state.cache.duplicates),
    )


def record_fetch(
    state_dir: str | os.PathLike[str],
    catalog: Catalog,
    selection: Selection,
) -> Path:
    """Persist a successful fetch and the (possibly edited) selection.

    Called only after a successful fetch, so a fetch failure can never touch
    the persisted selection.
    """
    state = load_state(state_dir)
    state.cache = CachedCatalog(
        endpoint=catalog.endpoint,
        fetched_at=catalog.fetched_at,
        model_ids=list(catalog.model_ids),
        duplicates=dict(catalog.duplicates),
    )
    state.selection = selection
    return save_state(state_dir, state)


def save_selection(state_dir: str | os.PathLike[str], selection: Selection) -> Path:
    """Persist selection intent without touching the cache."""
    state = load_state(state_dir)
    state.selection = selection
    return save_state(state_dir, state)


def save_config(state_dir: str | os.PathLike[str], config: EndpointConfig) -> Path:
    """Persist endpoint configuration (opt-in flags and env-var *names* only)."""
    state = load_state(state_dir)
    state.config = config
    return save_state(state_dir, state)
