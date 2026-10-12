"""cc-models core: CPA model catalog picker (bounded core unit, no UI/CLI).

Public modules:
- :mod:`cc_models.domain` — PickerOption / Selection / DefaultEdit / Catalog
  and endpoint normalization.
- :mod:`cc_models.catalog` — bounded GET of ``{endpoint}/models`` with
  redirect rejection and raw-ID validation.
- :mod:`cc_models.settings` — strict JSON snapshots, managed-key preview,
  safe apply/restore (private backups, drift double-check, atomic replace).
- :mod:`cc_models.store` — local state: selection intent plus endpoint-bound
  catalog cache. Never stores tokens.

The Textual UI and CLI entrypoints are future units; this package
deliberately exposes no console script yet.
"""

from .catalog import DEFAULT_TIMEOUT_SECONDS, fetch_catalog
from .domain import (
    PRESERVE_DEFAULT,
    BackupError,
    Catalog,
    CatalogError,
    ConfigError,
    CoreError,
    DefaultEdit,
    DriftError,
    EndpointError,
    NetworkError,
    PickerOption,
    RestoreRefusedError,
    Selection,
    SelectionError,
    StateError,
    normalize_endpoint,
)
from .settings import (
    ApplyResult,
    Preview,
    RestoreResult,
    SettingsSnapshot,
    apply_selection,
    build_model_picker,
    import_selection_from_settings,
    preview_apply,
    read_snapshot,
    restore_backup,
)
from .store import LocalState, cache_for_endpoint, load_state, record_fetch, save_selection

__version__ = "0.1.1"

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PRESERVE_DEFAULT",
    "ApplyResult",
    "BackupError",
    "Catalog",
    "CatalogError",
    "ConfigError",
    "CoreError",
    "DefaultEdit",
    "DriftError",
    "EndpointError",
    "LocalState",
    "NetworkError",
    "PickerOption",
    "Preview",
    "RestoreRefusedError",
    "RestoreResult",
    "Selection",
    "SelectionError",
    "SettingsSnapshot",
    "StateError",
    "__version__",
    "apply_selection",
    "build_model_picker",
    "cache_for_endpoint",
    "fetch_catalog",
    "import_selection_from_settings",
    "load_state",
    "normalize_endpoint",
    "preview_apply",
    "read_snapshot",
    "record_fetch",
    "restore_backup",
    "save_selection",
]
