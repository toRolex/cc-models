"""Settings snapshot, preview and safe apply/restore for the managed keys.

Managed keys (contract):
- ``modelPicker``: owned as a whole object. ``options`` and
  ``replaceBuiltInOptions`` (default true) are written from the selection;
  other picker-level fields imported from the file are preserved verbatim.
- ``model``: the top-level default model. Only written when the
  DefaultEdit is an explicit ``set``; a preserved default is left untouched
  (an existing default outside the selection is a preview warning, never a
  silent repoint or deletion).

Strict JSON only: malformed JSON, nonobject roots, duplicate object keys and
nonstandard NaN/Infinity constants are all rejected. JSONC comments are
refused rather than silently stripped (documented limitation). Unrelated
settings values are preserved semantically, not verbatim.

Safety mechanics:
- symlink settings/backup/lock targets are always rejected;
- a content hash is taken at snapshot time, re-checked under the tool-local
  writer lock, and checked a second time immediately before atomic replace;
- backups are private (0600), created with O_EXCL in the settings directory;
  a backup failure aborts the apply with the original file untouched;
- the temp file is same-directory, 0600, flushed and fsynced, then
  ``os.replace``d atomically;
- the writer lock is cooperating (this tool only); no cross-tool CAS is
  claimed.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from .domain import (
    BackupError,
    Catalog,
    ConfigError,
    DriftError,
    RestoreRefusedError,
    Selection,
    SelectionError,
    parse_strict_json,
)

MANAGED_PICKER_KEY = "modelPicker"
MANAGED_MODEL_KEY = "model"
LOCK_FILENAME = "cc-models.lock"
TEMP_PREFIX = ".cc-models-"
TEMP_SUFFIX = ".tmp"

# Documented picker-level keys; any other picker-level field is preserved on
# apply but its VALUE is redacted in previews (it may carry secrets).
DOCUMENTED_PICKER_KEYS = ("options", "replaceBuiltInOptions")
REDACTED_VALUE = "<unknown field redacted>"


# --------------------------------------------------------------------------
# Strict parsing and snapshots
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingsSnapshot:
    path: str
    content: str
    data: dict
    sha256: str

    @property
    def managed(self) -> dict:
        """Only the managed keys that are present (never the full settings)."""
        out = {}
        if MANAGED_PICKER_KEY in self.data:
            out[MANAGED_PICKER_KEY] = self.data[MANAGED_PICKER_KEY]
        if MANAGED_MODEL_KEY in self.data:
            out[MANAGED_MODEL_KEY] = self.data[MANAGED_MODEL_KEY]
        return out


def _sha256_hex(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_snapshot(path: str | os.PathLike[str]) -> SettingsSnapshot:
    """Read and validate a settings file; symlinks and bad JSON are rejected."""
    target = Path(path)
    if target.is_symlink():
        raise ConfigError(f"refusing symlink settings path: {target}")
    if not target.exists():
        raise ConfigError(f"settings file not found: {target}")
    content = target.read_bytes().decode("utf-8")
    data = parse_strict_json(content)
    if not isinstance(data, dict):
        raise ConfigError("settings root must be a JSON object")
    return SettingsSnapshot(
        path=str(target), content=content, data=data, sha256=_sha256_hex(content)
    )


# --------------------------------------------------------------------------
# Selection import / picker construction
# --------------------------------------------------------------------------


def existing_picker(snapshot: SettingsSnapshot) -> dict | None:
    picker = snapshot.data.get(MANAGED_PICKER_KEY)
    if picker is None:
        return None
    if not isinstance(picker, dict):
        raise ConfigError(f"{MANAGED_PICKER_KEY} must be a JSON object")
    return picker


def import_selection_from_settings(snapshot: SettingsSnapshot) -> tuple[Selection, list[str]]:
    """Import the initial selection from modelPicker if present.

    Returns the selection (ordered, extras preserved) plus warnings such as
    imported ``ccs-`` IDs. The default edit is always an explicit preserve;
    applying it never deletes an existing default.
    """
    picker = existing_picker(snapshot)
    warnings: list[str] = []
    if picker is None:
        return Selection(), warnings
    options_obj = picker.get("options")
    if options_obj is None:
        return Selection(), warnings
    if not isinstance(options_obj, list):
        raise ConfigError(f"{MANAGED_PICKER_KEY}.options must be a list")
    options = []
    for item in options_obj:
        option = _option_from_settings(item)
        options.append(option)
        if option.model.startswith("ccs-"):
            warnings.append(f"imported ccs- id in selection: {option.model}")
    selection = Selection(options=options)
    selection.validate()
    return selection, warnings


def _option_from_settings(item: object):
    from .domain import PickerOption

    return PickerOption.from_settings(item)


def build_model_picker(selection: Selection, picker: dict | None = None) -> dict:
    """Build the new modelPicker object from the selection.

    ``replaceBuiltInOptions`` comes from the selection's explicit override
    when set, otherwise from the imported picker (default true); imported
    picker-level fields other than ``options``/``replaceBuiltInOptions`` are
    preserved verbatim. ``behavesAs`` is never generated here; per-option
    extras only survive if they were imported.
    """
    out: dict = {}
    if picker is not None:
        for key, value in picker.items():
            if key not in ("options", "replaceBuiltInOptions"):
                out[key] = value
    if selection.replace_built_in is not None:
        out["replaceBuiltInOptions"] = selection.replace_built_in
    else:
        replace_built_in = picker.get("replaceBuiltInOptions") if picker else None
        out["replaceBuiltInOptions"] = (
            replace_built_in if isinstance(replace_built_in, bool) else True
        )
    out["options"] = [
        {**option.to_settings(), "model": selection.output_model_id(option.model)}
        for option in selection.options
    ]
    return out


def _merge_unknown_option_fields(
    new_picker: dict, original_picker: object, selection: Selection
) -> list[str]:
    """Preserve unknown imported option fields at apply time.

    Unknown option fields are never persisted in the selection; they are
    merged back from the original settings snapshot. Matching is by raw ID
    (the selection order pairs raw options with built output options): exact
    raw ID first, then the ``raw + '[1m]'`` form this tool may have written
    on a previous apply (exact raw prefix, case-insensitive ``[1m]``
    suffix), so re-apply and a later opt-out do not lose the fields. A raw
    ID that already carries a ``[1m]`` suffix never uses the alias fallback
    (this tool never writes a double suffix). Each source key is assigned to
    at most one option; a source owned by another selected literal raw ID is
    never reused. Ambiguous candidates raise SelectionError before write
    instead of silently losing data. Returns warnings.
    """
    warnings: list[str] = []
    if not isinstance(original_picker, dict):
        return warnings
    original_options = original_picker.get("options")
    if not isinstance(original_options, list):
        return warnings
    by_id: dict[str, list] = {}
    for item in original_options:
        if isinstance(item, dict) and isinstance(item.get("model"), str):
            by_id.setdefault(item["model"], []).append(item)
    from .domain import DOCUMENTED_OPTION_KEYS, SelectionError

    selected_raw = set(selection.ids)
    used_sources: set[str] = set()
    for raw_option, opt in zip(selection.options, new_picker["options"]):
        raw_id = raw_option.model
        candidates = by_id.get(raw_id, [])
        source_key = raw_id
        if not candidates and not raw_id.casefold().endswith("[1m]"):
            alias_keys = [
                k
                for k in by_id
                if k not in used_sources and k[-4:].casefold() == "[1m]" and k[:-4] == raw_id
            ]
            if len(alias_keys) > 1:
                raise SelectionError(
                    f"ambiguous unknown-field source candidates for {raw_id}: {alias_keys}"
                )
            if alias_keys and alias_keys[0] in selected_raw:
                # That source belongs to the selected literal suffixed option.
                warnings.append(
                    f"unknown fields for {raw_id} skipped: source {alias_keys[0]} "
                    "belongs to another selected option"
                )
                continue
            if alias_keys:
                source_key = alias_keys[0]
                candidates = by_id[source_key]
        if not candidates:
            continue
        if len(candidates) > 1:
            raise SelectionError(
                f"ambiguous unknown-field source for {raw_id}: "
                f"{len(candidates)} original options share the model ID"
            )
        used_sources.add(source_key)
        source = candidates[0]
        for key, value in source.items():
            if key not in DOCUMENTED_OPTION_KEYS and key not in opt:
                opt[key] = value
    return warnings


def compute_new_state(
    snapshot: SettingsSnapshot, selection: Selection
) -> tuple[dict, list[str], dict]:
    """Compute the new settings data, preview warnings and managed-key changes.

    Raises SelectionError for an empty selection (apply is blocked to avoid an
    accidental empty picker) or a dangling explicit default.
    """
    selection.validate()
    if not selection.options:
        raise SelectionError("refusing empty selection: apply would produce an empty picker")
    picker = existing_picker(snapshot)
    new_picker = build_model_picker(selection, picker)
    merge_warnings = _merge_unknown_option_fields(new_picker, picker, selection)
    new_data = dict(snapshot.data)
    new_data[MANAGED_PICKER_KEY] = new_picker
    if selection.default_edit.kind == "set":
        default_raw = selection.default_edit.value
        if isinstance(default_raw, str):
            new_data[MANAGED_MODEL_KEY] = selection.output_model_id(default_raw)

    warnings: list[str] = list(merge_warnings)
    outputs = [opt["model"] for opt in new_picker["options"]]
    if len(set(outputs)) != len(outputs):
        raw_ids = sorted(selection.ids)
        raise SelectionError(
            "transformed output model ID collision between selected options "
            f"{raw_ids}; exclude the un-suffixed raw ID via one_min_exclusions"
        )
    selected_ids = selection.ids
    for model_id in selected_ids:
        if model_id.startswith("ccs-") and not any(w.endswith(model_id) for w in warnings):
            warnings.append(f"imported ccs- id in selection: {model_id}")
    if any(opt["model"].casefold().endswith("[1m]") for opt in new_picker["options"]):
        warnings.append(
            "the [1m] output suffix is a client window declaration only: it does not "
            "extend backend capability, and CPA/upstream compatibility is not assured"
        )
    current_model = snapshot.data.get(MANAGED_MODEL_KEY)
    # Membership is judged against emitted output IDs: a preserved default is
    # compared with what the picker will actually contain after suffixing.
    output_ids = {opt["model"] for opt in new_picker["options"]}
    if (
        selection.default_edit.kind == "preserve"
        and isinstance(current_model, str)
        and current_model not in output_ids
    ):
        warnings.append(f"preserved default model not in selection: {current_model}")

    changes: dict[str, dict] = {}
    if snapshot.data.get(MANAGED_PICKER_KEY) != new_picker:
        changes[MANAGED_PICKER_KEY] = {
            "before": snapshot.data.get(MANAGED_PICKER_KEY),
            "after": new_picker,
        }
    if new_data.get(MANAGED_MODEL_KEY) != snapshot.data.get(MANAGED_MODEL_KEY):
        changes[MANAGED_MODEL_KEY] = {
            "before": snapshot.data.get(MANAGED_MODEL_KEY),
            "after": new_data[MANAGED_MODEL_KEY],
        }
    return new_data, warnings, changes


def add_missing_warnings(
    snapshot: SettingsSnapshot, selection: Selection, catalog: Catalog | None, warnings: list[str]
) -> list[str]:
    """Flag selected IDs missing from the catalog (derived, retained)."""
    if catalog is None:
        return warnings
    for model_id in catalog.missing_from(selection):
        warnings.append(f"missing from catalog (kept in selection): {model_id}")
    return warnings


def _redact_option_for_preview(option: object) -> object:
    from .domain import DOCUMENTED_OPTION_KEYS

    if not isinstance(option, dict):
        return option
    return {
        key: (value if key in DOCUMENTED_OPTION_KEYS else REDACTED_VALUE)
        for key, value in option.items()
    }


def _redact_picker_for_preview(picker: object) -> object:
    if not isinstance(picker, dict):
        return picker
    out: dict = {}
    for key, value in picker.items():
        if key == "options" and isinstance(value, list):
            out[key] = [_redact_option_for_preview(item) for item in value]
        elif key in DOCUMENTED_PICKER_KEYS:
            out[key] = value
        else:
            out[key] = REDACTED_VALUE
    return out


def redact_managed_changes(changes: dict[str, dict]) -> dict[str, dict]:
    """Redact the values of unknown fields in managed preview changes.

    Documented picker/option fields keep their values; any other field (a
    potential secret carrier imported from settings) keeps its key but its
    value is replaced so no unknown value can leak through a preview.
    """
    out: dict[str, dict] = {}
    for key, diff in changes.items():
        out[key] = {
            side: (_redact_picker_for_preview(value) if key == MANAGED_PICKER_KEY else value)
            for side, value in diff.items()
        }
    return out


@dataclass(frozen=True)
class Preview:
    """Managed-key-only preview. Never contains unrelated settings or secrets."""

    would_write: bool
    managed_changes: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def preview_apply(
    snapshot: SettingsSnapshot,
    selection: Selection,
    catalog: Catalog | None = None,
) -> Preview:
    _, warnings, changes = compute_new_state(snapshot, selection)
    add_missing_warnings(snapshot, selection, catalog, warnings)
    return Preview(
        would_write=bool(changes),
        managed_changes=redact_managed_changes(changes),
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Writer lock, backups, atomic replace
# --------------------------------------------------------------------------


def _reject_symlink(path: Path, what: str) -> None:
    if path.is_symlink():
        raise ConfigError(f"refusing symlink {what}: {path}")


def settings_lock_path(target: str | os.PathLike[str]) -> Path:
    """The writer lock file path for a settings target (next to the file)."""
    target_path = Path(os.path.realpath(target))
    return target_path.with_name(f".{target_path.name}.{LOCK_FILENAME}")


@contextmanager
def writer_lock_for_settings(target: str | os.PathLike[str]):
    """Cooperating writer lock keyed by the canonical settings target.

    The lock file lives next to the settings file, so two instances using the
    same settings file serialize even with different state directories. This
    is a this-tool-only cooperating lock; no cross-tool CAS is claimed.
    """
    lock_path = settings_lock_path(target)
    _reject_symlink(lock_path, "writer lock path")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield lock_path
    finally:
        os.close(fd)


def write_private_backup(target: Path, content: str) -> Path:
    """Create a 0600 backup next to the target, exclusively.

    Any failure raises BackupError; the caller must guarantee the original is
    untouched (this runs before any mutation).
    """
    stamp = time.strftime("%Y%m%dT%H%M%S")
    try:
        fd, name = tempfile.mkstemp(
            dir=target.parent, prefix=f"{target.name}.cc-models-backup-{stamp}-"
        )
    except OSError as exc:
        raise BackupError(f"could not create private backup: {exc}") from exc
    backup_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(backup_path, 0o600)
    except OSError as exc:
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
        with suppress(OSError):
            backup_path.unlink(missing_ok=True)
        raise BackupError(f"could not write private backup: {exc}") from exc
    return backup_path


def _atomic_write(target: Path, content: str, expected_sha: str) -> None:
    """Same-directory 0600 temp file, fsync, final drift recheck, atomic replace."""
    fd, temp_name = None, None
    try:
        fd, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        _reject_symlink(target, "settings target")
        recheck = read_snapshot(target)
        if recheck.sha256 != expected_sha:
            raise DriftError("settings changed between checks; refusing to replace")
        os.replace(temp_name, target)
        temp_name = None
        try:
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Apply and restore
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyResult:
    applied: bool
    no_op: bool
    backup_path: str | None
    warnings: list[str] = field(default_factory=list)
    managed_changes: dict[str, dict] = field(default_factory=dict)


def apply_selection(
    snapshot: SettingsSnapshot,
    selection: Selection,
    state_dir: str | os.PathLike[str],
    catalog: Catalog | None = None,
) -> ApplyResult:
    """Apply the selection under the writer lock with full drift safety.

    A semantically identical apply is a no-op: it still re-reads the file
    under the lock and refuses drift, but writes nothing and creates no
    backup.
    """
    selection.validate()
    if not selection.options:
        raise SelectionError("refusing empty selection: apply would produce an empty picker")
    with writer_lock_for_settings(snapshot.path):
        try:
            current = read_snapshot(snapshot.path)
        except ConfigError as exc:
            raise DriftError(f"settings target missing or unreadable: {exc}") from exc
        if current.sha256 != snapshot.sha256:
            raise DriftError("settings changed since snapshot; refusing to apply")
        new_data, warnings, changes = compute_new_state(current, selection)
        add_missing_warnings(current, selection, catalog, warnings)
        if not changes:
            return ApplyResult(applied=False, no_op=True, backup_path=None, warnings=warnings)
        new_content = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
        target = Path(current.path)
        backup_path = write_private_backup(target, current.content)
        _atomic_write(target, new_content, expected_sha=current.sha256)
        return ApplyResult(
            applied=True,
            no_op=False,
            backup_path=str(backup_path),
            warnings=warnings,
            managed_changes=redact_managed_changes(changes),
        )


@dataclass(frozen=True)
class RestoreResult:
    applied: bool
    no_op: bool
    backup_of_current: str | None
    warnings: list[str] = field(default_factory=list)
    managed_changes: dict[str, dict] = field(default_factory=dict)


def restore_backup(
    snapshot: SettingsSnapshot,
    backup_path: str | os.PathLike[str],
    state_dir: str | os.PathLike[str],
    confirm: bool = False,
) -> RestoreResult:
    """Restore a whole settings snapshot from a backup.

    Refuses without explicit ``confirm`` because a whole-snapshot restore
    also replaces unrelated settings keys. Validates the backup JSON, takes a
    fresh private backup of the current content, then applies the same safe
    writer checks as apply.
    """
    source = Path(backup_path)
    _reject_symlink(source, "backup path")
    if not source.exists():
        raise ConfigError(f"backup file not found: {source}")
    backup_content = source.read_bytes().decode("utf-8")
    backup_data = parse_strict_json(backup_content)
    if not isinstance(backup_data, dict):
        raise ConfigError("backup root must be a JSON object")
    if not confirm:
        raise RestoreRefusedError(
            "restore replaces the entire settings file, including unrelated keys; "
            "pass confirm=True (CLI: --yes) after reviewing"
        )
    warnings = ["whole-snapshot restore also replaces unrelated settings keys"]
    with writer_lock_for_settings(snapshot.path):
        try:
            current = read_snapshot(snapshot.path)
        except ConfigError as exc:
            raise DriftError(f"settings target missing or unreadable: {exc}") from exc
        if current.sha256 != snapshot.sha256:
            raise DriftError("settings changed since snapshot; refusing to restore")
        if current.content == backup_content:
            return RestoreResult(
                applied=False, no_op=True, backup_of_current=None, warnings=warnings
            )
        managed_changes: dict[str, dict] = {}
        for key in (MANAGED_PICKER_KEY, MANAGED_MODEL_KEY):
            if current.data.get(key) != backup_data.get(key):
                managed_changes[key] = {
                    "before": current.data.get(key),
                    "after": backup_data.get(key),
                }
        backup_of_current = write_private_backup(Path(current.path), current.content)
        _atomic_write(Path(current.path), backup_content, expected_sha=current.sha256)
        return RestoreResult(
            applied=True,
            no_op=False,
            backup_of_current=str(backup_of_current),
            warnings=warnings,
            managed_changes=redact_managed_changes(managed_changes),
        )
