"""Textual TUI for cc-models (Chinese primary UI; raw model IDs untouched).

Keys: j/k 移动焦点 · space 选择 · e 编辑标签和描述 · ctrl+↑/↓ 排序 · d 设默认 · / 过滤 · r 刷新 ·
p 预览 · a 确认应用 · c 端点配置 · Ctrl+P 命令/主题面板 · q 退出。

- Snapshot is taken at session start (in :mod:`session`); apply re-checks and
  drift is rejected, with an explicit reload offered afterwards.
- ``c`` modal configures the endpoint, the token environment-variable NAME and
  the explicit remote plain-HTTP opt-in (with a credential-transport warning);
  secrets are never shown, entered or persisted.
- ``--demo`` sessions (ctx.demo) never touch the network, not even refresh.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING

from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style as RichStyle
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.scrollbar import ScrollBarRender
from textual.theme import Theme
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, Static

from .catalog import fetch_catalog
from .domain import (
    PRESERVE_DEFAULT,
    Catalog,
    CoreError,
    DefaultEdit,
    DriftError,
    EndpointError,
    PickerOption,
    Selection,
    SortOrder,
    normalize_endpoint,
)
from .settings import preview_apply
from .store import EndpointConfig, load_state, record_fetch, save_selection, save_state

if TYPE_CHECKING:
    from .session import SessionContext

COLUMNS = ("选", "默认", "状态", "模型 ID", "标签", "描述")
INSECURE_WARNING = (
    "警告：远程明文 HTTP 不受加密保护，令牌将以明文传输，可能被中间人截获。\n"
    "仅在可信内网环境且明确知悉风险时开启。"
)
SECRETS_NOTE = "本工具只保存环境变量名称，从不保存令牌值。"
MOCHA_ANSI_THEME = Theme(
    name="cc-models-mocha-ansi",
    primary="#cba6f7",
    secondary="#89b4fa",
    warning="#f9e2af",
    error="#f38ba8",
    success="#a6e3a1",
    accent="#b4befe",
    foreground="#cdd6f4",
    dark=True,
    ansi=True,
    variables={
        "text": "#cdd6f4",
        "text-muted": "#a6adc8",
        "text-disabled": "#7f849c",
        "foreground-muted": "#a6adc8",
        "block-cursor-foreground": "#cba6f7",
        "block-cursor-background": "ansi_default",
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-foreground": "#a6adc8",
        "block-cursor-blurred-background": "ansi_default",
        "block-cursor-blurred-text-style": "bold",
        "block-hover-background": "ansi_default",
        "border": "#cba6f7",
        "border-blurred": "#585b70",
        "footer-foreground": "#a6adc8",
        "footer-background": "ansi_default",
        "footer-key-foreground": "#cba6f7",
        "footer-key-background": "ansi_default",
        "footer-description-foreground": "#a6adc8",
        "footer-description-background": "ansi_default",
        "footer-item-background": "ansi_default",
        "scrollbar": "#7f849c",
        "scrollbar-hover": "#cba6f7",
        "scrollbar-active": "#b4befe",
        "scrollbar-background": "ansi_default",
        "scrollbar-background-hover": "ansi_default",
        "scrollbar-background-active": "ansi_default",
        "scrollbar-corner-color": "ansi_default",
        "input-cursor-background": "#cba6f7",
        "input-cursor-foreground": "#1e1e2e",
        "input-cursor-text-style": "bold",
        "input-selection-background": "#cba6f7 45%",
        "input-selection-foreground": "#1e1e2e",
        "button-foreground": "#cdd6f4",
        "button-color-foreground": "#1e1e2e",
        "button-focus-text-style": "bold underline",
        "ansi-background": "#1e1e2e",
        "ansi-foreground": "#cdd6f4",
        "ansi": "#cdd6f4",
        "screen-selection-background": "#45475a",
        "screen-selection-foreground": "#cdd6f4",
    },
)


_SCROLLBAR_DEFAULT_BACK = Color.parse("#555555")
_SCROLLBAR_DEFAULT_BAR = Color.parse("bright_magenta")


# Scrollbar thumbs render with reverse video in a real terminal: the bar
# color becomes the effective cell background. Every grab segment (thumb body
# and both partial head/tail cells, reverse or not) is quantized to a full
# foreground block glyph so the thumb stays visible while every cell keeps
# the terminal default background. Position is whole-cell; fractional fill
# within the edge cells is not represented.
_THUMB_GLYPH_V = "▐"
_THUMB_GLYPH_H = "▄"


def _default_bg_thumb(segments: Segments, vertical: bool) -> Segments:
    """Quantize grab segments to full-cell foreground block glyphs.

    Drops reverse and explicit backgrounds on thumb cells, keeps the color
    and mouse metadata so grabbing and hovering still work. Track segments
    pass through unchanged.
    """
    glyph = _THUMB_GLYPH_V if vertical else _THUMB_GLYPH_H
    out: list[Segment] = []
    for seg in segments.segments:
        style = seg.style
        if style is None or (style.meta or {}).get("@mouse.down") != "grab":
            out.append(seg)
            continue
        out.append(
            Segment(
                glyph * len(seg.text),
                RichStyle(color=style.color, meta=style.meta),
            )
        )
    return Segments(out, new_lines=vertical)


class _DefaultBgScrollBarRender(ScrollBarRender):
    """Renderer that keeps scrollbar cells on the terminal default background."""

    @classmethod
    def render_bar(
        cls,
        size: int = 25,
        virtual_size: float = 50,
        window_size: float = 20,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        back_color: Color = _SCROLLBAR_DEFAULT_BACK,
        bar_color: Color = _SCROLLBAR_DEFAULT_BAR,
    ) -> Segments:
        segments = super().render_bar(
            size=size,
            virtual_size=virtual_size,
            window_size=window_size,
            position=position,
            thickness=thickness,
            vertical=vertical,
            back_color=back_color,
            bar_color=bar_color,
        )
        return _default_bg_thumb(segments, vertical)


class FilterInput(Input):
    BINDINGS = [Binding("escape", "app.focus_table", "返回表格", show=False)]


class ModelTable(DataTable):
    BINDINGS = [
        Binding("j", "cursor_down", "向下", show=False),
        Binding("k", "cursor_up", "向上", show=False),
        Binding("space", "app.toggle_selection", "选择", key_display="空格"),
        Binding("c", "app.config", "端点配置"),
        Binding("e", "app.rename", "重命名"),
        Binding("ctrl+up", "app.move_up", "上移", key_display="ctrl+↑"),
        Binding("ctrl+down", "app.move_down", "下移", key_display="ctrl+↓"),
        Binding("s", "app.toggle_sort", "排序 A–Z/Z–A"),
        Binding("d", "app.default", "设默认"),
    ]

    # Instance-level renderer: only scrollbars owned by this app's widgets get
    # the default-background transform; other Apps in the same process keep
    # Textual's stock rendering. Assigned on every access so lazily created
    # scrollbars are covered without polling the widget tree.
    @property
    def vertical_scrollbar(self):  # type: ignore[override]
        scrollbar = super().vertical_scrollbar
        scrollbar.renderer = _DefaultBgScrollBarRender  # type: ignore[misc]
        return scrollbar

    @property
    def horizontal_scrollbar(self):  # type: ignore[override]
        scrollbar = super().horizontal_scrollbar
        scrollbar.renderer = _DefaultBgScrollBarRender  # type: ignore[misc]
        return scrollbar


@dataclass(frozen=True)
class RenameResult:
    label: str
    description: str | None


class RenameModal(ModalScreen[RenameResult | None]):
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, model_id: str, label: str, description: str | None) -> None:
        super().__init__()
        self._model_id = model_id
        self._label = label
        self._description = description or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-dialog"):
            yield Label(f"编辑标签和描述（模型 ID 不变）: {self._model_id}")
            yield Label("显示标签：")
            yield Input(value=self._label, id="rename-input")
            yield Label("描述（留空清除）：")
            yield Input(value=self._description, id="description-input")
            with Horizontal():
                yield Button("保存", id="rename-save", variant="primary")
                yield Button("取消", id="rename-cancel")
            yield Label("标签回车进入描述 · 描述回车保存 · Esc 取消")

    def on_mount(self) -> None:
        self.query_one("#rename-input", Input).focus()

    @on(Input.Submitted, "#rename-input")
    def label_submitted(self) -> None:
        self.query_one("#description-input", Input).focus()

    @on(Input.Submitted, "#description-input")
    def description_submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#rename-save")
    def save(self) -> None:
        self._submit()

    @on(Button.Pressed, "#rename-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        label = self.query_one("#rename-input", Input).value.strip() or self._label
        description = self.query_one("#description-input", Input).value.strip() or None
        self.dismiss(RenameResult(label, description))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfigModal(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(
        self,
        endpoint: str,
        token_env: str,
        allow_insecure: bool,
        replace: bool | None,
        replace_effective: bool,
    ) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._token_env = token_env
        self._allow_insecure = allow_insecure
        self._replace = replace
        self._replace_effective = replace_effective

    def compose(self) -> ComposeResult:
        with Vertical(id="config-dialog"):
            yield Label("端点配置")
            yield Label("端点 URL（例如 http://127.0.0.1:2317）：")
            yield Input(value=self._endpoint, id="endpoint-input")
            yield Label("令牌环境变量名称（不输入令牌值）：")
            yield Input(value=self._token_env, id="token-env-input")
            yield Label(
                "替换内置选项 replaceBuiltInOptions（当前生效: "
                f"{'开启' if self._replace_effective else '关闭'}；回车切换 → {self._replace if self._replace is not None else '跟随设置文件'}）："
            )
            yield Input(
                value="" if self._replace is None else ("t" if self._replace else "f"),
                placeholder="输入 t 显式开 / f 显式关 / 留空跟随文件",
                id="replace-input",
            )
            yield Label(
                (
                    "已启用远程明文 HTTP opt-in。"
                    if self._allow_insecure
                    else "远程明文 HTTP：关闭。"
                )
                + " 如需启用，在端点中保留 http:// 远程地址并勾选确认："
            )
            yield Label(INSECURE_WARNING)
            yield Checkbox(
                "我允许向远程明文 HTTP 发送凭据", value=self._allow_insecure, id="insecure-http"
            )
            yield Label(SECRETS_NOTE)
            with Horizontal():
                yield Button("保存", id="config-save", variant="primary")
                yield Button("取消", id="config-cancel")

    @on(Button.Pressed, "#config-save")
    def save(self) -> None:
        replace_text = self.query_one("#replace-input", Input).value.strip().lower()
        replace_value: bool | None
        if not replace_text:
            replace_value = None
        elif replace_text.startswith(("t", "1", "y", "开")):
            replace_value = True
        elif replace_text.startswith(("f", "0", "n", "关")):
            replace_value = False
        else:
            self.app.notify("replaceBuiltInOptions 需为 t/f 或留空", severity="error")
            return
        self.dismiss(
            {
                "endpoint": self.query_one("#endpoint-input", Input).value.strip(),
                "token_env": self.query_one("#token-env-input", Input).value.strip(),
                "allow_insecure_http": self.query_one("#insecure-http", Checkbox).value,
                "confirm_insecure_http": self.query_one("#insecure-http", Checkbox).value,
                "replace_built_in": replace_value,
            }
        )

    @staticmethod
    def _is_remote_http(url: str) -> bool:
        try:
            normalized = normalize_endpoint(url, allow_insecure_http=True)
        except EndpointError:
            return False
        return (
            normalized.startswith("http://")
            and "127.0.0.1" not in normalized
            and "localhost" not in normalized
        )

    @on(Button.Pressed, "#config-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PreviewModal(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "关闭"), Binding("q", "close", "关闭")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="preview-dialog"):
            yield Label("应用预览（仅受管键，绝不含其他设置或凭据）")
            yield Static(self._text, id="preview-text")
            yield Label("Esc/q 关闭 · a 在主界面确认应用")

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmApplyModal(ModalScreen[bool]):
    def __init__(self, summary: str) -> None:
        super().__init__()
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label("确认应用以下变更？")
            yield Static(self._summary, id="confirm-text")
            yield Label("将先创建 0600 私有备份，再原子替换。")
            with Horizontal():
                yield Button("应用", id="apply-yes", variant="warning")
                yield Button("取消", id="apply-no")

    @on(Button.Pressed, "#apply-yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#apply-no")
    def no(self) -> None:
        self.dismiss(False)


class DriftModal(ModalScreen[str]):
    """Drift was rejected; offer an explicit reload."""

    def __init__(self, detail: str) -> None:
        super().__init__()
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="drift-dialog"):
            yield Label("设置文件已在外部被修改，应用已拒绝。")
            yield Static(self._detail)
            yield Label("可显式重新加载最新设置（保留当前选择意图），或取消。")
            with Horizontal():
                yield Button("重新加载", id="drift-reload", variant="primary")
                yield Button("取消", id="drift-cancel")

    @on(Button.Pressed, "#drift-reload")
    def reload(self) -> None:
        self.dismiss("reload")

    @on(Button.Pressed, "#drift-cancel")
    def action_cancel(self) -> None:
        self.dismiss("cancel")


RowMeta = tuple[str, bool, bool]  # (model_id, selected, missing)


class EmptyReason(StrEnum):
    """Why the model table has no rows to show."""

    NO_CATALOG = "no_catalog"
    FETCHING = "fetching"
    FETCH_FAILED = "fetch_failed"
    CATALOG_EMPTY = "catalog_empty"
    FILTERED = "filtered"


EMPTY_TEXT: Mapping[EmptyReason, str] = MappingProxyType(
    {
        EmptyReason.NO_CATALOG: "尚无目录缓存 · 按 r 拉取模型目录",
        EmptyReason.FETCHING: "正在拉取模型目录…",
        EmptyReason.FETCH_FAILED: "目录拉取失败 · 按 r 重试",
        EmptyReason.CATALOG_EMPTY: "上游未返回任何模型 · 按 r 重试",
        EmptyReason.FILTERED: "没有匹配的模型 · 按 / 修改过滤",
    }
)


class FetchState(StrEnum):
    """Lifecycle of the current endpoint's refresh worker."""

    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"


@dataclass(frozen=True)
class TableView:
    """The rendered rows plus, only when there are none, the empty-state reason."""

    rows: list[RowMeta]
    reason: EmptyReason | None

    def __post_init__(self) -> None:
        if self.rows and self.reason is not None:
            raise ValueError("rows imply reason is None")
        if not self.rows and self.reason is None:
            raise ValueError("no rows implies a reason")


def project_table(
    selection: Selection,
    catalog: Catalog | None,
    filter_text: str,
    fetch_state: FetchState,
    *,
    sort: SortOrder = SortOrder.AZ,
) -> TableView:
    """Project globally casefold-sorted rows without changing selection order.

    Empty-state precedence is filtered, refreshing, failed, no catalog, empty
    catalog. Equal sort keys retain their input order in both directions.
    """
    selected = {opt.model: opt for opt in selection.options}
    ordered: list[RowMeta] = []
    for opt in selection.options:
        missing = catalog is not None and not catalog.contains(opt.model)
        ordered.append((opt.model, True, missing))
    if catalog is not None:
        for model_id in catalog.model_ids:
            if model_id not in selected:
                ordered.append((model_id, False, False))
    ordered.sort(
        key=lambda row: row[0].casefold(),
        reverse=sort is SortOrder.ZA,
    )

    needle = filter_text.casefold()
    visible = [
        (mid, sel, miss)
        for mid, sel, miss in ordered
        if not needle
        or needle in mid.casefold()
        or (sel and needle in selected[mid].label.casefold())
        or (sel and needle in (selected[mid].description or "").casefold())
    ]

    if visible:
        return TableView(visible, None)
    if ordered:
        return TableView([], EmptyReason.FILTERED)
    if fetch_state is FetchState.RUNNING:
        return TableView([], EmptyReason.FETCHING)
    if fetch_state is FetchState.FAILED:
        return TableView([], EmptyReason.FETCH_FAILED)
    if catalog is None:
        return TableView([], EmptyReason.NO_CATALOG)
    return TableView([], EmptyReason.CATALOG_EMPTY)


class CCModelsApp(App):
    TITLE = "cc-models — CPA 模型选择器"
    CSS = """
    Vertical { align: center middle; }
    App:ansi ToggleButton > .toggle--button {
        background: #1e1e2e; color: #a6adc8;
    }
    App:ansi ToggleButton.-on > .toggle--button {
        background: #1e1e2e; color: $success;
    }
    App:ansi Toast {
        background: #313244; color: $text;
        border: round $border-blurred;
    }
    App:ansi Toast.-information { border-left: outer $success; }
    App:ansi Toast.-warning { border-left: outer $warning; }
    App:ansi Toast.-error { border-left: outer $error; }
    App:ansi Toast .toast--title { color: $text; text-style: bold; }
    #rename-dialog, #confirm-dialog, #drift-dialog {
        width: 70; height: auto; max-height: 80%; border: round $accent;
        background: $surface; padding: 1 2;
    }
    #config-dialog {
        width: 90; height: auto; max-height: 90%; border: round $accent;
        background: $surface; padding: 1 2;
    }
    #preview-dialog {
        width: 95%; height: 80%; border: round $accent; background: $surface;
        padding: 1 2;
    }
    #preview-text { overflow-y: auto; height: 1fr; margin: 1 0; }
    #confirm-text, #drift-dialog Static { margin: 1 0; }
    Horizontal { height: auto; align-horizontal: center; }
    Horizontal Button { margin: 0 1; }
    App:ansi Screen { background: ansi_default; color: $text; }
    App:ansi * {
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
    }
    App:ansi Footer {
        background: ansi_default; color: $text-muted;
    }
    App:ansi #title {
        background: ansi_default; color: $primary;
    }
    #title {
        width: 100%; height: 1; padding: 0 1; text-style: bold;
        color: $primary; background: $panel;
    }
    App:ansi FooterKey, App:ansi FooterKey:hover { background: ansi_default; }
    App:ansi #empty {
        height: 1; padding: 0 1; color: $text-muted; background: ansi_default;
    }
    App:ansi #filter {
        height: 1; margin: 0 1; padding: 0 1; border: none;
        background: ansi_default; color: $text;
    }
    App:ansi #filter:focus {
        border: none; background: ansi_default; background-tint: transparent;
    }
    App:ansi #filter > .input--placeholder { color: $text-muted; }
    App:ansi #table {
        height: 1fr; background: ansi_default; color: $text;
        background-tint: transparent;
    }
    App:ansi #table:focus { background-tint: transparent; }
    App:ansi #table > .datatable--header {
        background: ansi_default; color: $text-muted; text-style: bold;
    }
    App:ansi #table > .datatable--cursor,
    App:ansi #table:focus > .datatable--cursor,
    App:ansi #table:focus > .datatable--fixed-cursor {
        background: ansi_default; color: $primary; text-style: bold;
    }
    App:ansi #table > .datatable--even-row,
    App:ansi #table > .datatable--odd-row { background: ansi_default; }
    App:ansi #table > .datatable--fixed { background: ansi_default; color: $text; }
    App:ansi #status {
        dock: bottom; height: 1; padding: 0 1; color: $text-muted;
        background: ansi_default;
    }
    App:ansi #rename-dialog, App:ansi #confirm-dialog,
    App:ansi #drift-dialog, App:ansi #config-dialog,
    App:ansi #preview-dialog { background: #1e1e2e; color: $text; }
    App:ansi #rename-dialog Input, App:ansi #config-dialog Input {
        background: #181825; color: $text; border: tall $border-blurred;
    }
    App:ansi #rename-dialog Input:focus, App:ansi #config-dialog Input:focus {
        background: #181825; background-tint: transparent; border: tall $accent;
    }
    App:ansi Button {
        min-width: 12; height: 3; background: #313244; color: $text;
        border: round $border-blurred;
    }
    App:ansi Button:hover { background: #45475a; border: round $accent; }
    App:ansi Button:focus { text-style: bold underline; }
    App:ansi Button.-primary { color: $primary; border: round $primary; }
    App:ansi Button.-warning { color: $warning; border: round $warning; }
    App:ansi #config-dialog Checkbox {
        background: #1e1e2e; color: $text; border: tall $border-blurred;
    }
    App:ansi #config-dialog Checkbox:focus {
        border: tall $accent; background: #1e1e2e;
    }
    App:ansi #config-dialog Checkbox > .toggle--button {
        background: #313244; color: $text-muted;
    }
    App:ansi #config-dialog Checkbox.-on > .toggle--button {
        background: #313244; color: $success;
    }
    App:ansi CommandPalette { background: ansi_default; color: $text; }
    App:ansi CommandPalette > Vertical { background: #1e1e2e; }
    App:ansi CommandPalette #--container { background: #1e1e2e; border: round $accent; }
    App:ansi CommandPalette #--input { background: #181825; }
    App:ansi CommandPalette #--input Input { background: #181825; color: $text; }
    App:ansi CommandPalette #--results,
    App:ansi CommandPalette #--results > * { background: #1e1e2e; }
    Horizontal { height: auto; align-horizontal: center; }
    Horizontal Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("/", "focus_filter", "过滤"),
        Binding("r", "refresh", "刷新"),
        Binding("p", "preview", "预览"),
        Binding("a", "apply", "应用"),
        Binding("q", "quit", "退出"),
        Binding("ctrl+p", "command_palette", "命令/主题", show=False, key_display="^p"),
    ]

    def __init__(self, ctx: SessionContext, *, fetch_on_start: bool = False) -> None:
        super().__init__()
        self.ctx = ctx
        self._fetch_on_start = fetch_on_start
        self._filter_text = ""
        self._refresh_generation = 0
        self._fetch_state = FetchState.IDLE
        self.register_theme(MOCHA_ANSI_THEME)
        self.theme = MOCHA_ANSI_THEME.name
        self._row_metas: list[RowMeta] = []  # (model_id, selected, missing)

    @property
    def available_themes(self) -> dict[str, Theme]:
        """Only ansi=True themes keep the terminal default background after a switch."""
        return {name: theme for name, theme in super().available_themes.items() if theme.ansi}

    @property
    def current_theme(self) -> Theme:
        # Textual's fallback asserts on the unfiltered "textual-dark" default,
        # which our ansi-only registry no longer contains during App.__init__.
        return self.get_theme(self.theme) or MOCHA_ANSI_THEME

    # ------------------------------------------------------------------ UI

    def compose(self) -> ComposeResult:
        yield Static("cc-models — CPA 模型选择器", id="title")
        yield FilterInput(placeholder="过滤（/ 聚焦，Esc 返回表格）…", id="filter")
        yield Static("", id="empty")
        yield ModelTable(id="table")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", ModelTable)
        table.cursor_type = "row"
        table.add_columns(*COLUMNS)
        if not self.ctx.demo:
            from .session import resolve_endpoint

            resolution = resolve_endpoint(self.ctx)
            self.ctx.catalog = self.ctx.catalog_for_current_endpoint(
                resolution.raw, resolution.allow_insecure_http
            )
        self._rebuild_table()
        table.focus()
        if self._fetch_on_start and not self.ctx.demo and self.ctx.catalog is None:
            self.action_refresh()
        if self.ctx.imported_from_settings:
            self.notify("已从设置文件导入初始选择（本地状态此前为空）")

    def _focused_meta(self) -> RowMeta | None:
        table = self.query_one("#table", ModelTable)
        if self._row_metas and 0 <= table.cursor_row < len(self._row_metas):
            return self._row_metas[table.cursor_row]
        return None

    def _update_empty(self, reason: EmptyReason | None) -> None:
        empty = self.query_one("#empty", Static)
        if reason is None:
            empty.display = False
        else:
            empty.update(EMPTY_TEXT[reason])
            empty.display = True

    def _rebuild_table(self) -> None:
        table = self.query_one("#table", ModelTable)
        focused = self._focused_meta()
        focused_id = focused[0] if focused is not None else None
        old_cursor_row = table.cursor_row
        selected = {opt.model: opt for opt in self.ctx.selection.options}
        view = project_table(
            self.ctx.selection,
            self.ctx.catalog,
            self._filter_text,
            self._fetch_state,
            sort=self.ctx.state.sort,
        )
        visible = view.rows
        self._row_metas = visible
        visible_indices = {
            model_id: index for index, (model_id, _selected, _missing) in enumerate(visible)
        }

        default_id: str | None = None
        preserved_id: str | None = None
        if self.ctx.selection.default_edit.kind == "set":
            default_id = self.ctx.selection.default_edit.value
        else:
            model_value = self.ctx.snapshot.data.get("model")
            if isinstance(model_value, str):
                preserved_id = model_value

        table.clear()
        for model_id, is_selected, missing in visible:
            option = selected.get(model_id)
            label = option.label if option else ""
            description = (option.description if option else "") or ""
            chosen = Text("✓", style="#a6e3a1 bold") if is_selected else ""
            if model_id == default_id:
                default_mark = Text("★", style="#f9e2af bold")
            elif model_id == preserved_id:
                default_mark = Text("☆", style="#f9e2af")
            else:
                default_mark = ""
            status = Text("缺失", style="#f38ba8") if missing else ""
            table.add_row(chosen, default_mark, status, model_id, label, description)

        if visible:
            if focused_id is not None and focused_id in visible_indices:
                row = visible_indices[focused_id]
            else:
                row = max(0, min(old_cursor_row, len(visible) - 1))
            table.move_cursor(row=row)

        self._update_empty(view.reason)

        direction = "Z–A" if self.ctx.state.sort is SortOrder.ZA else "A–Z"
        self.query_one("#title", Static).update(f"cc-models — CPA 模型选择器 · 名称 {direction}")
        status = self.query_one("#status", Static)
        resolution_hint = (
            "演示模式（无网络）"
            if self.ctx.demo
            else "本地缓存可用"
            if self.ctx.catalog
            else "无目录缓存"
        )
        replace_state = (
            str(self.ctx.selection.replace_built_in)
            if self.ctx.selection.replace_built_in is not None
            else "跟随文件"
        )
        status.update(
            f"已选 {len(self.ctx.selection.options)} · 目录 "
            f"{len(self.ctx.catalog.model_ids) if self.ctx.catalog else 0} · "
            f"排序 {'Z–A' if self.ctx.state.sort is SortOrder.ZA else 'A–Z'} · "
            f"replaceBuiltInOptions: {replace_state} · {resolution_hint}"
        )

    def _save_selection(self) -> bool:
        try:
            save_selection(self.ctx.state_dir, self.ctx.selection)
        except (CoreError, OSError) as exc:
            self.notify(f"保存选择失败: {exc}", severity="error")
            return False
        return True

    def _selected_option(self, model_id: str) -> PickerOption | None:
        for option in self.ctx.selection.options:
            if option.model == model_id:
                return option
        return None

    # -------------------------------------------------------------- actions

    def action_focus_filter(self) -> None:
        self.query_one("#filter", FilterInput).focus()

    def action_focus_table(self) -> None:
        self.query_one("#table", ModelTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._filter_text = event.value
            self._rebuild_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self.action_focus_table()

    def action_toggle_selection(self) -> None:
        focused = self._focused_meta()
        if focused is None:
            return
        model_id, is_selected, _missing = focused
        if is_selected:
            self.ctx.selection.options = [
                o for o in self.ctx.selection.options if o.model != model_id
            ]
            if (
                self.ctx.selection.default_edit.kind == "set"
                and self.ctx.selection.default_edit.value == model_id
            ):
                self.ctx.selection.default_edit = PRESERVE_DEFAULT
                self.notify("已取消选择并还原为保留原默认", severity="warning")
        else:
            self.ctx.selection.options.append(PickerOption(model_id, model_id))
        self._save_selection()
        self._rebuild_table()

    def action_rename(self) -> None:
        focused = self._focused_meta()
        if focused is None:
            return
        option = self._selected_option(focused[0])
        if option is None:
            self.notify("未选中的行不能重命名；先按空格选择", severity="warning")
            return
        self.push_screen(
            RenameModal(option.model, option.label, option.description), self._apply_rename
        )

    def _apply_rename(self, result: RenameResult | None) -> None:
        focused = self._focused_meta()
        if result is None or focused is None:
            return
        model_id = focused[0]
        option = self._selected_option(model_id)
        if option is None:
            return
        self.ctx.selection.options = [
            PickerOption(o.model, result.label, result.description, dict(o.extra))
            if o.model == model_id
            else o
            for o in self.ctx.selection.options
        ]
        self._save_selection()
        self._rebuild_table()

    def _move(self, delta: int) -> None:
        focused = self._focused_meta()
        if focused is None or not focused[1]:
            return
        options = self.ctx.selection.options
        index = next(i for i, o in enumerate(options) if o.model == focused[0])
        target = index + delta
        if not 0 <= target < len(options):
            return
        options[index], options[target] = options[target], options[index]
        if self._save_selection():
            direction = "Z–A" if self.ctx.state.sort is SortOrder.ZA else "A–Z"
            self.notify(
                f"应用顺序已调整：第 {target + 1}/{len(options)} 项；表格仍按 {direction} 显示"
            )
        self._rebuild_table()

    def action_move_up(self) -> None:
        self._move(-1)

    def action_move_down(self) -> None:
        self._move(1)

    def action_toggle_sort(self) -> None:
        """Persist the direction and imported selection before updating the view."""
        target = SortOrder.ZA if self.ctx.state.sort is SortOrder.AZ else SortOrder.AZ
        try:
            candidate = load_state(self.ctx.state_dir)
            candidate.sort = target
            candidate.selection = self.ctx.selection
            save_state(self.ctx.state_dir, candidate)
        except (CoreError, OSError) as exc:
            self.notify(f"切换排序失败: {exc}", severity="error")
            return
        self.ctx.state = candidate
        self._rebuild_table()
        self.notify(f"已切换为 {'Z–A' if target is SortOrder.ZA else 'A–Z'} 排序")

    def action_default(self) -> None:
        focused = self._focused_meta()
        if focused is None or not focused[1]:
            self.notify("默认模型必须是已选条目", severity="warning")
            return
        model_id = focused[0]
        if (
            self.ctx.selection.default_edit.kind == "set"
            and self.ctx.selection.default_edit.value == model_id
        ):
            self.ctx.selection.default_edit = PRESERVE_DEFAULT
            self.notify("默认编辑已还原为『保留原默认』")
        else:
            self.ctx.selection.default_edit = DefaultEdit("set", model_id)
            self.notify(f"应用时将默认模型设为: {model_id}")
        self._save_selection()
        self._rebuild_table()

    # refresh -----------------------------------------------------------

    def action_refresh(self) -> None:
        self._refresh_generation += 1
        if self.ctx.demo:
            from .session import demo_catalog

            self.ctx.catalog = demo_catalog(datetime.now(UTC).isoformat(timespec="seconds"))
            self._rebuild_table()
            self.notify("演示模式：使用确定性演示目录，未访问网络")
            return
        from .session import resolve_api_key, resolve_endpoint

        resolution = resolve_endpoint(self.ctx)
        try:
            normalize_endpoint(resolution.raw, resolution.allow_insecure_http)
        except (EndpointError, ValueError):
            self.notify("端点无效，请重新配置", severity="error")
            return
        api_key, _source = resolve_api_key(self.ctx, resolution)
        self._fetch_state = FetchState.RUNNING
        self._rebuild_table()
        self.notify(f"正在从 {resolution.raw} 刷新…")
        self._refresh_worker(resolution.raw, resolution.allow_insecure_http, api_key)

    def _refresh_worker(self, raw: str, allow_insecure: bool, api_key: str | None) -> None:
        self.run_worker(
            partial(
                self._fetch_catalog_blocking, self._refresh_generation, raw, allow_insecure, api_key
            ),
            thread=True,
            group="refresh",
            exclusive=True,
        )

    def _fetch_catalog_blocking(
        self, generation: int, raw: str, allow_insecure: bool, api_key: str | None
    ) -> None:
        try:
            catalog = fetch_catalog(raw, api_key=api_key, allow_insecure_http=allow_insecure)
        except CoreError as exc:
            self.call_from_thread(self._refresh_done, generation, raw, None, str(exc))
        else:
            self.call_from_thread(self._refresh_done, generation, raw, catalog, None)

    def _refresh_done(self, generation: int, endpoint: str, catalog, error: str | None) -> None:
        from .session import resolve_endpoint

        resolution = resolve_endpoint(self.ctx)
        if generation != self._refresh_generation or resolution.raw != endpoint:
            return
        if error is not None:
            self._fetch_state = FetchState.FAILED
            self._rebuild_table()
            self.notify(f"刷新失败: {error}", severity="error")
            self.notify("本地选择保持不变", severity="warning")
            return
        if catalog.endpoint != endpoint:
            return
        try:
            record_fetch(self.ctx.state_dir, catalog, self.ctx.selection)
        except CoreError as exc:
            self._fetch_state = FetchState.FAILED
            self._rebuild_table()
            self.notify(f"保存目录失败: {exc}", severity="error")
            return
        self._fetch_state = FetchState.IDLE
        self.ctx.refresh_state()
        self.ctx.catalog = catalog
        self._rebuild_table()
        self.notify(f"目录已刷新: {len(catalog.model_ids)} 个模型")

    # preview / apply ----------------------------------------------------

    def _preview_text(self) -> tuple[str, object]:
        preview = preview_apply(self.ctx.snapshot, self.ctx.selection, self.ctx.catalog)
        import json as _json

        text = _json.dumps(preview.managed_changes, ensure_ascii=False, indent=2)
        if preview.warnings:
            text += "\n\n警告:\n" + "\n".join(f"- {w}" for w in preview.warnings)
        if not preview.would_write:
            text = "（无变更 no-op）\n" + text
        return text, preview

    def action_preview(self) -> None:
        try:
            text, _preview = self._preview_text()
        except CoreError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(PreviewModal(text))

    def action_apply(self) -> None:
        try:
            text, _preview = self._preview_text()
        except CoreError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(ConfirmApplyModal(text), self._do_apply)

    def _do_apply(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            from .settings import apply_selection

            result = apply_selection(
                self.ctx.snapshot, self.ctx.selection, self.ctx.state_dir, self.ctx.catalog
            )
        except DriftError as exc:
            self.push_screen(DriftModal(str(exc)), self._after_drift)
            return
        except CoreError as exc:
            self.notify(f"应用失败: {exc}", severity="error")
            return
        self._after_apply(result)

    def _after_apply(self, result) -> None:
        self.ctx.reload_snapshot()
        self._rebuild_table()
        if result.no_op:
            self.notify("无变更（no-op），未创建备份")
        else:
            self.notify(f"已应用；备份: {result.backup_path}")

    def _after_drift(self, choice: str | None) -> None:
        if choice != "reload":
            return
        try:
            self.ctx.reload_snapshot()
        except CoreError as exc:
            self.notify(f"重新加载失败: {exc}", severity="error")
            return
        from .session import resolve_endpoint

        resolution = resolve_endpoint(self.ctx)
        self._refresh_generation += 1
        self._fetch_state = FetchState.IDLE
        self.ctx.catalog = self.ctx.catalog_for_current_endpoint(
            resolution.raw, resolution.allow_insecure_http
        )
        self._rebuild_table()
        self.notify("已重新加载设置快照（选择意图保留）")

    # config --------------------------------------------------------------

    def action_config(self) -> None:
        from .session import resolve_endpoint

        resolution = resolve_endpoint(self.ctx)
        replace_effective = (
            self.ctx.selection.replace_built_in
            if self.ctx.selection.replace_built_in is not None
            else bool(
                self.ctx.snapshot.data.get("modelPicker", {}).get("replaceBuiltInOptions", True)
            )
        )
        self.push_screen(
            ConfigModal(
                endpoint=resolution.raw,
                token_env=resolution.token_env,
                allow_insecure=resolution.allow_insecure_http,
                replace=self.ctx.selection.replace_built_in,
                replace_effective=bool(replace_effective),
            ),
            self._save_config,
        )

    def _save_config(self, values: dict | None) -> None:
        if not values:
            return
        try:
            normalized = normalize_endpoint(
                values["endpoint"], allow_insecure_http=values["allow_insecure_http"]
            )
        except (EndpointError, ValueError):
            self.notify("端点无效，请使用不含凭据、查询或片段的 URL", severity="error")
            return
        token_env = values["token_env"] or "CPA_API_KEY"
        try:
            state = load_state(self.ctx.state_dir)
            state.config = EndpointConfig(
                endpoint=normalized,
                allow_insecure_http=values["allow_insecure_http"],
                token_env=token_env,
            )
            from dataclasses import replace

            state.selection = replace(
                self.ctx.selection, replace_built_in=values["replace_built_in"]
            )
            save_state(self.ctx.state_dir, state)
        except CoreError as exc:
            self.notify(f"保存配置失败: {exc}", severity="error")
            return
        self._refresh_generation += 1
        self._fetch_state = FetchState.IDLE
        self.ctx.refresh_state()
        self.ctx.selection = self.ctx.state.selection
        self.ctx.catalog = self.ctx.catalog_for_current_endpoint(
            normalized, values["allow_insecure_http"]
        )
        self._rebuild_table()
        self.notify(f"端点已保存: {normalized}（令牌仅保存环境变量名 {token_env}）")
        if values["allow_insecure_http"]:
            self.notify(INSECURE_WARNING, severity="warning")


def run_app(ctx: SessionContext) -> None:
    CCModelsApp(ctx, fetch_on_start=True).run()
