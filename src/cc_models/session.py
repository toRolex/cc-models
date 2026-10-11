"""Session glue shared by the TUI and CLI (not part of the core contract).

Responsibilities:
- locate/validate the settings file (friendly refusal when missing — first-MVP
  limitation: cc-models only manages an *existing* settings file);
- startup selection semantics: import ``modelPicker`` from settings only when
  the local state file is absent. A saved selection — even an explicitly empty
  one — is never reimported;
- resolve the endpoint (local state config → ``CPA_BASE_URL`` env → settings
  ``ANTHROPIC_BASE_URL`` → default) and the API key. A settings-derived token
  is only used when the configured endpoint matches the settings-derived base
  URL after normalization; it is never sent to a different endpoint. Token
  values are resolved in memory only and never printed or persisted;
- build the deterministic ``--demo`` fixture (temporary settings/state, no
  live HTTP, even refresh).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .domain import (
    DEFAULT_BASE_URL,
    Catalog,
    CoreError,
    EndpointError,
    Selection,
    normalize_endpoint,
)
from .settings import SettingsSnapshot, import_selection_from_settings, read_snapshot
from .store import (
    EndpointConfig,
    LocalState,
    cache_for_endpoint,
    load_state,
    state_path,
)

DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
DEFAULT_STATE_DIR = Path.home() / ".cc-models"

DEMO_TOKEN = "sk-ant-demo-not-a-real-key"  # fixture value; never a real credential
DEMO_ENDPOINT = "http://localhost:2317/v1"
DEMO_MODEL_IDS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "custom-proxy-model",
]


class SessionError(CoreError):
    """Friendly session-level failure (missing settings, bad environment)."""


@dataclass
class SessionContext:
    """Everything one cc-models session needs; UI layers read/update this."""

    settings_path: Path
    state_dir: Path
    snapshot: SettingsSnapshot
    state: LocalState
    selection: Selection
    catalog: Catalog | None
    imported_from_settings: bool
    demo: bool = False
    demo_catalog: Catalog | None = None
    warnings: list[str] = field(default_factory=list)

    def reload_snapshot(self) -> None:
        """Explicitly re-read the settings snapshot (after drift rejection)."""
        self.snapshot = read_snapshot(self.settings_path)

    def refresh_state(self) -> None:
        self.state = load_state(self.state_dir)

    def catalog_for_current_endpoint(
        self, endpoint_raw: str, allow_insecure_http: bool
    ) -> Catalog | None:
        try:
            return cache_for_endpoint(self.state, endpoint_raw, allow_insecure_http)
        except EndpointError:
            return None


def demo_settings_data() -> dict:
    return {
        "ANTHROPIC_AUTH_TOKEN": DEMO_TOKEN,
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:2317",
        "numParallelServers": 2,
        "theme": "dark",
        "model": "claude-sonnet-4-6",
        "modelPicker": {
            "replaceBuiltInOptions": True,
            "options": [
                {"model": "claude-opus-4-6", "label": "Opus 演示"},
                {"model": "custom-proxy-model", "label": "自定义代理", "description": "演示缺失项"},
            ],
        },
    }


def demo_catalog(fetched_at: str = "2026-01-01T00:00:00+00:00") -> Catalog:
    return Catalog(
        endpoint=DEMO_ENDPOINT,
        fetched_at=fetched_at,
        model_ids=list(DEMO_MODEL_IDS),
    )


def build_demo_context() -> SessionContext:
    """A deterministic demo fixture in a fresh temp dir. No live HTTP ever."""
    demo_dir = Path(tempfile.mkdtemp(prefix="cc-models-demo-"))
    settings_path = demo_dir / "settings.json"
    settings_path.write_text(
        __import__("json").dumps(demo_settings_data(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    state_dir = demo_dir / "state"
    ctx = load_session(settings_path, state_dir, demo=True)
    ctx.catalog = demo_catalog()
    return ctx


def load_session(
    settings_path: str | os.PathLike[str],
    state_dir: str | os.PathLike[str],
    demo: bool = False,
) -> SessionContext:
    """Load one session; friendly refusals instead of raw tracebacks."""
    settings = Path(settings_path)
    if settings.is_symlink():
        raise SessionError(f"设置路径不能是符号链接: {settings}")
    if not settings.exists():
        raise SessionError(
            f"未找到设置文件: {settings}\n"
            "cc-models 管理一个已存在的 settings.json（首次 MVP 不负责创建）。\n"
            "可用 --settings PATH 指定路径，或用 --demo 在临时目录中试用。"
        )
    snapshot = read_snapshot(settings)
    state = load_state(state_dir)
    state_file_exists = state_path(state_dir).exists()

    warnings: list[str] = []
    imported = False
    if state_file_exists:
        # A saved selection is authoritative — even when it is explicitly empty.
        selection = state.selection
    else:
        selection, import_warnings = import_selection_from_settings(snapshot)
        warnings.extend(import_warnings)
        imported = True

    return SessionContext(
        settings_path=settings,
        state_dir=Path(state_dir),
        snapshot=snapshot,
        state=state,
        selection=selection,
        catalog=None,
        imported_from_settings=imported,
        demo=demo,
        warnings=warnings,
    )


@dataclass(frozen=True)
class EndpointResolution:
    raw: str
    source: str  # state | env | settings | default
    allow_insecure_http: bool
    token_env: str


def _normalized_or_raw(raw: str, allow_insecure_http: bool) -> str:
    """Never expose an invalid external URL, which may contain secrets."""
    try:
        return normalize_endpoint(raw, allow_insecure_http=allow_insecure_http)
    except (EndpointError, ValueError):
        return "（端点无效，请重新配置）"


def resolve_endpoint(
    ctx: SessionContext,
    env: Mapping[str, str] | None = None,
    *,
    allow_insecure_http: bool = False,
) -> EndpointResolution:
    """Endpoint precedence: state config → CPA_BASE_URL → settings → default.

    ``raw`` carries the normalized endpoint identity (the same form used for
    cache binding), so the TUI/CLI display and the cache agree.
    """
    env = os.environ if env is None else env
    config: EndpointConfig = ctx.state.config
    allow_insecure_http = allow_insecure_http or config.allow_insecure_http
    if config.endpoint:
        return EndpointResolution(
            _normalized_or_raw(config.endpoint, allow_insecure_http),
            "本地状态",
            allow_insecure_http,
            config.token_env,
        )
    if env.get("CPA_BASE_URL"):
        return EndpointResolution(
            _normalized_or_raw(env["CPA_BASE_URL"], allow_insecure_http),
            "环境变量 CPA_BASE_URL",
            allow_insecure_http,
            config.token_env,
        )
    settings_env = ctx.snapshot.data.get("env", {})
    if not isinstance(settings_env, dict):
        settings_env = {}
    settings_base = settings_env.get("ANTHROPIC_BASE_URL") or ctx.snapshot.data.get(
        "ANTHROPIC_BASE_URL"
    )
    if isinstance(settings_base, str) and settings_base:
        return EndpointResolution(
            _normalized_or_raw(settings_base, allow_insecure_http),
            "设置文件 ANTHROPIC_BASE_URL",
            allow_insecure_http,
            config.token_env,
        )
    return EndpointResolution(
        _normalized_or_raw(DEFAULT_BASE_URL, allow_insecure_http),
        "默认值",
        allow_insecure_http,
        config.token_env,
    )


def resolve_api_key(
    ctx: SessionContext,
    resolution: EndpointResolution,
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str]:
    """Resolve a token in memory; never print or persist the value.

    Precedence: ``token_env`` environment variable; otherwise a
    settings-derived ANTHROPIC token — only when the configured endpoint is
    the same normalized endpoint as the settings' own ANTHROPIC_BASE_URL.
    """
    env = os.environ if env is None else env
    value = env.get(resolution.token_env)
    if value:
        return value, f"环境变量 {resolution.token_env}"

    settings_env = ctx.snapshot.data.get("env", {})
    if not isinstance(settings_env, dict):
        settings_env = {}
    settings_base = settings_env.get("ANTHROPIC_BASE_URL") or ctx.snapshot.data.get(
        "ANTHROPIC_BASE_URL"
    )
    if isinstance(settings_base, str) and settings_base:
        try:
            normalized = normalize_endpoint(settings_base, resolution.allow_insecure_http)
        except EndpointError:
            normalized = None
        if normalized is not None:
            try:
                current = normalize_endpoint(resolution.raw, resolution.allow_insecure_http)
            except EndpointError:
                current = None
            if current == normalized:
                for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
                    token = settings_env.get(key) or ctx.snapshot.data.get(key)
                    if isinstance(token, str) and token:
                        return token, f"设置文件 {key}（仅同一端点回退）"
    return None, "无"
