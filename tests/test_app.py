"""TUI Pilot tests for CCModelsApp on the deterministic --demo fixture.

Sync tests wrap ``asyncio.run`` around ``App.run_test(size=(120, 40))`` with a
Pilot. The demo context never touches the network; the refresh test installs a
tripwire so any accidental fetch fails loudly. Assertions inspect the real
temp settings/state files and actual widget state — no snapshot-only checks.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import pytest
from rich.console import Console
from textual.app import InvalidThemeError
from textual.renderables.blank import Blank
from textual.widgets import Input

from cc_models.app import (
    MOCHA_ANSI_THEME,
    CCModelsApp,
    ConfigModal,
    ConfirmApplyModal,
    ModelTable,
    PreviewModal,
    RenameModal,
)
from cc_models.domain import DefaultEdit
from cc_models.session import (
    DEMO_MODEL_IDS,
    DEMO_TOKEN,
    SessionContext,
    build_demo_context,
    load_session,
)
from cc_models.store import load_state, state_path

SIZE = (120, 40)
Scenario = Callable[[CCModelsApp, object], Awaitable[None]]


def run_pilot(scenario: Scenario) -> SessionContext:
    """Run one Pilot scenario against a fresh demo session; return its ctx."""
    return run_pilot_at(SIZE, scenario)


def run_pilot_at(size: tuple[int, int], scenario: Scenario) -> SessionContext:
    """Run one Pilot scenario at an explicit terminal size."""
    ctx = build_demo_context()
    app = CCModelsApp(ctx)

    async def _main() -> None:
        async with app.run_test(size=size) as pilot:
            await scenario(app, pilot)

    asyncio.run(_main())
    return ctx


def _bad_cells_in_strips(strips) -> list[tuple[int, int]]:
    """Locate non-default effective backgrounds in rendered strips.

    Violations: an explicit bgcolor of any color kind (standard ANSI, 256,
    RGB — not just RGB triplets), or reverse video with any foreground
    (a terminal paints the fg as bg, default fg included).
    """
    from rich.color import ColorType

    bad: list[tuple[int, int]] = []
    for y, strip in enumerate(strips):
        x = 0
        for segment in strip:
            style = segment.style
            if style is None:
                x += len(segment.text)
                continue
            bgcolor = style.bgcolor
            explicit_bg = bgcolor is not None and bgcolor.type != ColorType.DEFAULT
            if explicit_bg or style.reverse:
                bad.append((y, x))
            x += len(segment.text)
    return bad


def nondefault_background_cells(app: CCModelsApp) -> list[tuple[int, int]]:
    """Cells whose effective background is not the terminal default."""
    return _bad_cells_in_strips(app.screen._compositor.render_strips())


@pytest.mark.parametrize(
    "seg_style",
    [
        "explicit standard bg",
        "explicit 256 bg",
        "explicit rgb bg",
        "reverse rgb fg",
        "reverse 256 fg",
        "reverse default fg",
        "reverse no color",
    ],
)
def test_background_audit_detects_every_color_kind_and_reverse(seg_style: str) -> None:
    """Negative controls: each violating style makes the audit report a cell."""
    from rich.color import Color as RichColor
    from rich.segment import Segment as RichSegment
    from rich.style import Style as RichStyle

    rgb = RichColor.from_rgb(127, 132, 156)
    by_name = {
        "explicit standard bg": RichStyle(bgcolor=RichColor.from_ansi(1)),
        "explicit 256 bg": RichStyle(bgcolor=RichColor.from_ansi(196)),
        "explicit rgb bg": RichStyle(bgcolor=rgb),
        "reverse rgb fg": RichStyle(reverse=True, color=rgb),
        "reverse 256 fg": RichStyle(reverse=True, color=RichColor.from_ansi(196)),
        "reverse default fg": RichStyle(reverse=True, color=RichColor.default()),
        "reverse no color": RichStyle(reverse=True),
    }
    style = by_name[seg_style]
    strips = [[RichSegment("text ", style)]]
    assert _bad_cells_in_strips(strips) == [(0, 0)]


def rows(app: CCModelsApp) -> list[tuple[str, bool]]:
    """(model_id, selected) for every visible table row, in display order."""
    return [(mid, sel) for mid, sel, _miss in app._row_metas]


# ---------------------------------------------------------------- test 1


def test_demo_toggle_default_and_rename(tmp_path) -> None:
    """Selection preserves focus; e edits label/description without changing ID."""

    async def scenario(app: CCModelsApp, pilot) -> None:
        ctx = app.ctx
        await pilot.pause()
        # Demo settings import two options; catalog adds the other two rows.
        assert rows(app) == [
            ("claude-opus-4-6", True),
            ("custom-proxy-model", True),
            ("claude-sonnet-4-6", False),
            ("claude-haiku-4-5", False),
        ]
        table = app.query_one("#table", ModelTable)
        # Raw model IDs are shown untouched in the 模型 ID column.
        assert table.get_row_at(0)[3] == "claude-opus-4-6"
        assert table.get_row_at(3)[3] == "claude-haiku-4-5"

        assert isinstance(app.focused, ModelTable)
        await pilot.press("j")
        await pilot.pause()
        assert app._focused_meta() == ("custom-proxy-model", True, False)
        await pilot.press("k")
        await pilot.pause()
        assert app._focused_meta() == ("claude-opus-4-6", True, False)

        # space on row 0 (already selected) deselects it. Unselected catalog
        # rows keep catalog order, so opus moves into the unselected block.
        await pilot.press("space")
        await pilot.pause()
        assert rows(app) == [
            ("custom-proxy-model", True),
            ("claude-opus-4-6", False),
            ("claude-sonnet-4-6", False),
            ("claude-haiku-4-5", False),
        ]
        assert [o.model for o in ctx.selection.options] == ["custom-proxy-model"]

        assert app._focused_meta() == ("claude-opus-4-6", False, False)
        # Focus follows the model ID even though the removed selected item moves rows.
        await pilot.press("j", "j", "space")
        await pilot.pause()
        # Selection order first, then the remaining catalog rows.
        assert rows(app) == [
            ("custom-proxy-model", True),
            ("claude-haiku-4-5", True),
            ("claude-opus-4-6", False),
            ("claude-sonnet-4-6", False),
        ]
        assert [o.model for o in ctx.selection.options] == [
            "custom-proxy-model",
            "claude-haiku-4-5",
        ]

        assert app._focused_meta() == ("claude-haiku-4-5", True, False)
        await pilot.press("ctrl+up")
        await pilot.pause()
        assert app._focused_meta() == ("claude-haiku-4-5", True, False)
        assert [option.model for option in ctx.selection.options] == [
            "claude-haiku-4-5",
            "custom-proxy-model",
        ]
        await pilot.press("ctrl+down")
        await pilot.pause()
        assert app._focused_meta() == ("claude-haiku-4-5", True, False)
        assert [option.model for option in ctx.selection.options] == [
            "custom-proxy-model",
            "claude-haiku-4-5",
        ]
        await pilot.press("d")
        await pilot.pause()
        assert ctx.selection.default_edit == DefaultEdit("set", "claude-haiku-4-5")

        # e opens the rename modal for the focused selected row.
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, RenameModal)
        await pilot.press("j", "k")
        label_input = app.screen.query_one("#rename-input", Input)
        assert label_input.value.endswith("jk")
        await pilot.press("ctrl+u")
        await pilot.press("O", "p", "u", "s", " ", "H", "a", "i", "k", "u")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.focused, Input) and app.focused.id == "description-input"
        await pilot.press("N", "e", "w", " ", "d", "e", "s", "c")
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, RenameModal)
        haiku = next(o for o in ctx.selection.options if o.model == "claude-haiku-4-5")
        assert haiku.label == "Opus Haiku"
        assert haiku.description == "New desc"
        # Raw IDs never change on rename.
        assert [o.model for o in ctx.selection.options] == [
            "custom-proxy-model",
            "claude-haiku-4-5",
        ]

        # Everything above persisted to the real local state file.
        state = load_state(ctx.state_dir)
        assert [o.model for o in state.selection.options] == [
            "custom-proxy-model",
            "claude-haiku-4-5",
        ]
        assert state.selection.options[-1].label == "Opus Haiku"
        assert state.selection.options[-1].description == "New desc"
        assert state.selection.default_edit == DefaultEdit("set", "claude-haiku-4-5")

        # Reopen the editor and clear the description with an empty value.
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, RenameModal)
        await pilot.click("#description-input")
        description_input = app.screen.query_one("#description-input", Input)
        await pilot.press("end", *(["backspace"] * len(description_input.value)))
        await pilot.press("enter")
        await pilot.pause()
        haiku = next(o for o in ctx.selection.options if o.model == "claude-haiku-4-5")
        assert haiku.label == "Opus Haiku"
        assert haiku.description is None
        assert load_state(ctx.state_dir).selection.options[-1].description is None
        # The temp settings file is untouched by selection edits.
        assert "claude-haiku-4-5" not in ctx.settings_path.read_text(encoding="utf-8")

    run_pilot(scenario)


# ---------------------------------------------------------------- test 2


def test_demo_filter_via_input_narrows_rows(tmp_path) -> None:
    """Filter typing keeps j/k literal, and selection preserves focused identity."""

    async def scenario(app: CCModelsApp, pilot) -> None:
        await pilot.pause()
        assert len(rows(app)) == 4
        await pilot.press("/")
        assert isinstance(app.focused, Input) and app.focused.id == "filter"

        await pilot.press("h", "a", "i", "k", "u")
        await pilot.pause()
        assert rows(app) == [("claude-haiku-4-5", False)]
        assert isinstance(app.focused, Input) and app.focused.id == "filter"
        await pilot.press("ctrl+u")
        await pilot.press("j", "k")
        await pilot.pause()
        assert app.query_one("#filter", Input).value == "jk"
        assert rows(app) == []
        await pilot.press("ctrl+u")
        await pilot.press("s", "o", "n", "n", "e", "t")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.focused, ModelTable)
        assert rows(app) == [("claude-sonnet-4-6", False)]
        await pilot.press("space")
        await pilot.pause()
        assert rows(app) == [("claude-sonnet-4-6", True)]
        assert app._focused_meta() == ("claude-sonnet-4-6", True, False)
        await pilot.press("space")
        await pilot.pause()
        assert rows(app) == [("claude-sonnet-4-6", False)]
        assert app._focused_meta() == ("claude-sonnet-4-6", False, False)

        # A selected row also matches by label (CJK label text).
        await pilot.press("/")
        await pilot.press("ctrl+u")
        await pilot.press("演", "示")
        await pilot.pause()
        assert [mid for mid, _sel in rows(app)] == ["claude-opus-4-6", "custom-proxy-model"]
        assert app._focused_meta() == ("custom-proxy-model", True, False)

        # The old focused row remains hidden while the filter is active. Clear
        # the search and its model identity returns to the cursor.
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert len(rows(app)) == 4
        # Esc returns focus to the table.
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.focused, ModelTable)

    run_pilot(scenario)


# ---------------------------------------------------------------- test 3


def test_demo_preview_cancel_then_apply_confirm_writes_settings(tmp_path) -> None:
    """p previews and cancels without writing; a + confirm writes real settings."""

    async def scenario(app: CCModelsApp, pilot) -> None:
        ctx = app.ctx
        await pilot.pause()
        original = ctx.settings_path.read_text(encoding="utf-8")

        # Edit a selected option's description through the UI before applying.
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, RenameModal)
        await pilot.press("enter")
        await pilot.press(*"Isolated description")
        await pilot.press("enter")
        await pilot.pause()
        assert ctx.selection.options[0].description == "Isolated description"

        # d sets default row 0 (claude-opus-4-6), so apply has real work.
        # This also creates the local state file; capture the file listing
        # afterwards so only the apply backup shows up as a new entry.
        await pilot.press("d")
        await pilot.pause()
        demo_dir_files = sorted(p.name for p in ctx.settings_path.parent.iterdir())
        assert "state" in demo_dir_files

        # p opens the preview; escape cancels and nothing is written.
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, PreviewModal)
        preview_body = app.screen._text
        assert '"after": "claude-opus-4-6"' in preview_body
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, (PreviewModal, ConfirmApplyModal))
        assert ctx.settings_path.read_text(encoding="utf-8") == original
        assert sorted(p.name for p in ctx.settings_path.parent.iterdir()) == demo_dir_files

        # a opens the confirm modal; clicking 应用 writes the real settings file.
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmApplyModal)
        await pilot.press("enter")
        await pilot.pause()

        data = json.loads(ctx.settings_path.read_text(encoding="utf-8"))
        picker = data["modelPicker"]
        # Raw IDs, not labels, land in the settings file.
        assert [opt["model"] for opt in picker["options"]] == [
            "claude-opus-4-6",
            "custom-proxy-model",
        ]
        assert picker["options"][0]["label"] == "Opus 演示"
        assert picker["options"][0]["description"] == "Isolated description"
        assert picker["replaceBuiltInOptions"] is True
        # Default edit lands in the managed top-level model key.
        assert data["model"] == "claude-opus-4-6"
        # Unmanaged settings keys are preserved verbatim.
        assert data["theme"] == "dark"
        assert data["numParallelServers"] == 2
        assert data["ANTHROPIC_AUTH_TOKEN"] == DEMO_TOKEN
        reloaded = load_session(ctx.settings_path, ctx.state_dir)
        assert reloaded.selection.options[0].description == "Isolated description"
        # Exactly the writer lock and one 0600 backup appeared next to the
        # settings file; the backup preserves the pre-apply content verbatim.
        new_files = sorted(p.name for p in ctx.settings_path.parent.iterdir())
        added = set(new_files) - set(demo_dir_files)
        backups = [n for n in added if n.startswith("settings.json.cc-models-backup-")]
        assert added == {".settings.json.cc-models.lock", backups[0]}
        assert len(backups) == 1
        backup = ctx.settings_path.parent / backups[0]
        assert backup.read_text(encoding="utf-8") == original
        assert (backup.stat().st_mode & 0o777) == 0o600

    run_pilot(scenario)


# ---------------------------------------------------------------- test 4


def test_demo_config_save_and_refresh_never_fetches(tmp_path, monkeypatch) -> None:
    """c saves endpoint + token-env NAME only; demo refresh must not fetch."""

    async def scenario(app: CCModelsApp, pilot) -> None:
        from textual.widgets import Input

        ctx = app.ctx
        await pilot.pause()

        # Network tripwire: any fetch attempt in this session fails the test.
        def _tripwire(*_a: object, **_kw: object) -> object:
            raise AssertionError("demo session attempted a network fetch")

        monkeypatch.setattr("cc_models.app.fetch_catalog", _tripwire)
        monkeypatch.setattr("cc_models.catalog.fetch_catalog", _tripwire)

        # c opens the config modal prefilled from settings resolution.
        await pilot.press("c")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ConfigModal)
        endpoint_input = modal.query_one("#endpoint-input", Input)
        token_env_input = modal.query_one("#token-env-input", Input)
        assert endpoint_input.value == "http://127.0.0.1:2317/v1"

        # Replace endpoint and the token-env NAME via the real Input widgets.
        # Click moves the cursor to the click point, so jump to the end first
        # before deleting the prefilled value.
        await pilot.click("#endpoint-input")
        await pilot.pause()
        await pilot.press(*(["end"] + ["backspace"] * len(endpoint_input.value)))
        await pilot.press(*"http://127.0.0.1:9999")
        await pilot.click("#token-env-input")
        await pilot.pause()
        await pilot.press(*(["end"] + ["backspace"] * len(token_env_input.value)))
        await pilot.press(*"MY_TOKEN_VAR")
        await pilot.click("#config-save")
        await pilot.pause()
        assert not isinstance(app.screen, ConfigModal)

        # Local state holds the normalized endpoint and the env NAME — never
        # a token value.
        config = load_state(ctx.state_dir).config
        assert config.endpoint == "http://127.0.0.1:9999/v1"
        assert config.token_env == "MY_TOKEN_VAR"
        assert config.allow_insecure_http is False
        state_raw = (ctx.state_dir / "state.json").read_text(encoding="utf-8")
        assert "MY_TOKEN_VAR" in state_raw
        assert "sk-ant" not in state_raw

        # r in demo mode reloads the deterministic demo catalog offline.
        await pilot.press("r")
        await pilot.pause()
        assert ctx.catalog is not None
        assert list(ctx.catalog.model_ids) == list(DEMO_MODEL_IDS)
        assert ctx.demo is True

    run_pilot(scenario)


def test_theme_defaults_switches_and_resets_without_settings_writes() -> None:
    """Real theme panel only offers ansi themes and switching keeps default background."""

    async def scenario(app: CCModelsApp, pilot) -> None:
        ctx = app.ctx
        original_settings = ctx.settings_path.read_bytes()
        await pilot.pause()
        assert app.theme == MOCHA_ANSI_THEME.name

        # The real panel path: ctrl+p command palette, then the Theme search
        # palette backed by ThemeProvider over app.available_themes.
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"Theme")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # Unsafe built-ins are filtered out of the registry, so typing their
        # names matches nothing and the theme stays mocha.
        await pilot.press(*"catppuccin-latte")
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == MOCHA_ANSI_THEME.name
        assert ctx.settings_path.read_bytes() == original_settings

        # Switch to a safe built-in via the same real panel path.
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"Theme")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"ansi-dark")
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == "ansi-dark"
        assert app.current_theme.ansi is True
        assert app.screen.styles.background.ansi == -1

        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"Theme")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*MOCHA_ANSI_THEME.name)
        await pilot.press("enter")
        await pilot.pause()
        assert app.theme == MOCHA_ANSI_THEME.name
        assert app.current_theme.ansi is True

    ctx = run_pilot(scenario)
    assert CCModelsApp(ctx).theme == MOCHA_ANSI_THEME.name


def test_theme_assignment_boundary_rejects_non_ansi_themes() -> None:
    """Direct assignment bypassing the panel is rejected by the framework boundary."""

    async def scenario(app: CCModelsApp, pilot) -> None:
        await pilot.pause()
        with pytest.raises(InvalidThemeError):
            app.theme = "dracula"
        await pilot.pause()
        assert app.theme == MOCHA_ANSI_THEME.name
        assert app.screen.styles.background.ansi == -1

    run_pilot(scenario)


def test_theme_switch_keeps_terminal_default_strips() -> None:
    """After switching to a safe theme, rendered strips keep DEFAULT backgrounds."""

    async def scenario(app: CCModelsApp, pilot) -> None:
        app.theme = "ansi-dark"
        await pilot.pause()
        assert app.current_theme.ansi is True
        root_blank = app.screen.render()
        assert isinstance(root_blank, Blank)
        assert root_blank._rich_style.bgcolor is not None
        assert root_blank._rich_style.bgcolor.type.name == "DEFAULT"
        for selector in ("#table", "#filter", "#title", "Footer", "#status"):
            assert app.query_one(selector).styles.background.ansi == -1
        composed = app.screen._compositor.render_strips(app.size)
        for strip in composed:
            for segment in strip._segments:
                assert segment.style is not None
                if segment.style.bgcolor is not None:
                    assert segment.style.bgcolor.type.name == "DEFAULT"

    run_pilot(scenario)


def test_mocha_ansi_theme_renders_terminal_default_surfaces() -> None:
    async def scenario(app: CCModelsApp, pilot) -> None:
        await pilot.pause()
        assert app.current_theme.ansi is True
        assert app.screen.styles.background.ansi == -1
        root_blank = app.screen.render()
        assert isinstance(root_blank, Blank)
        assert root_blank._rich_style.bgcolor is not None
        assert root_blank._rich_style.bgcolor.type.name == "DEFAULT"

        for selector in ("#table", "#filter", "#title", "Footer", "#status"):
            assert app.query_one(selector).styles.background.ansi == -1

        composed = app.screen._compositor.render_strips(app.size)
        backgrounds = []
        for strip in composed:
            for segment in strip._segments:
                assert segment.style is not None
                if segment.style.bgcolor is not None:
                    backgrounds.append(segment.style.bgcolor)
        assert backgrounds
        assert all(background.type.name == "DEFAULT" for background in backgrounds)

        table = app.query_one("#table", ModelTable)
        assert table.get_component_styles("datatable--cursor").background.ansi == -1
        checked = table.get_row_at(0)[0]
        checked_style = checked.get_style_at_offset(Console(), 0)
        assert checked_style.color.get_truecolor().hex == "#a6e3a1"
        assert checked_style.bold is True

        await pilot.press("/")
        await pilot.pause()
        assert app.query_one("#filter").styles.background.ansi == -1
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is app.query_one("#table")

        await pilot.press("c")
        await pilot.pause()
        dialog = app.screen.query_one("#config-dialog")
        assert dialog.styles.background.ansi is None
        assert (
            dialog.styles.background.r,
            dialog.styles.background.g,
            dialog.styles.background.b,
        ) == (30, 30, 46)

    run_pilot(scenario)


def test_build_demo_context_shape() -> None:
    """build_demo_context returns a ready SessionContext in a fresh temp dir."""
    ctx = build_demo_context()
    assert ctx.demo is True
    assert ctx.settings_path.exists()
    # The local state file is only created on the first save.
    assert not state_path(ctx.state_dir).exists()
    assert ctx.catalog is not None and list(ctx.catalog.model_ids) == list(DEMO_MODEL_IDS)
    assert ctx.imported_from_settings is True
    assert [o.model for o in ctx.selection.options] == [
        "claude-opus-4-6",
        "custom-proxy-model",
    ]


# ---------------------------------------------------------------- test 6
async def _title_footer_scenario(app: CCModelsApp, pilot) -> None:
    await pilot.pause()
    title = app.query_one("#title")
    assert title.styles.background.ansi == -1
    strips = app.screen._compositor.render_strips(app.size)
    if isinstance(strips, tuple):
        strips = strips[0]
    top = "".join(s.text for s in strips[0])
    bottom = "".join(s.text for s in strips[-1])
    assert "cc-models — CPA 模型选择器" in top
    assert "端点配置" in bottom
    assert "命令/主题" in bottom
    assert "^p" in bottom

    await pilot.press("ctrl+p")
    await pilot.pause()
    from textual.command import CommandPalette

    assert any(isinstance(s, CommandPalette) for s in app.screen_stack)
    await pilot.press("escape")
    await pilot.pause()

    await pilot.press("c")
    await pilot.pause()
    app.screen.query_one("#config-dialog")


def test_title_line_and_footer_key_copy(tmp_path) -> None:
    """Title renders on line one; footer distinguishes endpoint config from palette."""
    run_pilot(_title_footer_scenario)


async def _narrow_footer_scenario(app: CCModelsApp, pilot) -> None:
    await pilot.pause()
    strips = app.screen._compositor.render_strips()
    bottom = "".join(s.text for s in strips[-1])
    assert "端点配置" in bottom, bottom
    assert "^p 命令/主题" in bottom, bottom
    assert bottom.count("^p") == 1, f"palette entry duplicated: {bottom!r}"

    await pilot.press("ctrl+p")
    await pilot.pause()
    from textual.command import CommandPalette

    assert any(isinstance(s, CommandPalette) for s in app.screen_stack)


def test_narrow_footer_shows_endpoint_and_single_palette_entry() -> None:
    """At 60x24 the footer still shows c 端点配置 and exactly one ^p entry."""
    run_pilot_at((60, 24), _narrow_footer_scenario)


def _in_allowed(x: int, y: int, allowed) -> bool:
    return any(
        region.x <= x < region.x + region.width and region.y <= y < region.y + region.height
        for region in allowed
    )


def _outside_allowed(bad_cells, allowed) -> list[tuple[int, int]]:
    """Audit cells that fall outside every allowed overlay rectangle."""
    return [(y, x) for y, x in bad_cells if not _in_allowed(x, y, allowed)]


def _strip_line_with_violations(width: int, xs, style):
    """One strip row of terminal-default cells with violating cells at xs."""
    from rich.segment import Segment as RichSegment

    segments = []
    cursor = 0
    for x in sorted(xs):
        segments.append(RichSegment(" " * (x - cursor)))
        segments.append(RichSegment("V", style))
        cursor = x + 1
    segments.append(RichSegment(" " * (width - cursor)))
    return segments


async def _narrow_scroll_background_scenario(app: CCModelsApp, pilot) -> None:
    table = app.query_one("#table")
    for theme_name in ("cc-models-mocha-ansi", "ansi-dark", "ansi-light"):
        app.theme = theme_name
        await pilot.pause()
        table.scroll_end(animate=False)
        await pilot.pause()
        assert nondefault_background_cells(app) == [], theme_name

    await pilot.press("ctrl+p")
    await pilot.pause()
    from textual.widgets import OptionList

    palette = app.screen_stack[-1]
    option_list = palette.query_one(OptionList)
    # Real overflow: pad the palette list so it actually scrolls, then scroll.
    strips_before = app.screen._compositor.render_strips()
    for index in range(80):
        option_list.add_option(f"filler-{index}")
    await pilot.pause()
    option_list.scroll_end(animate=False)
    await pilot.pause()
    assert option_list.show_vertical_scrollbar, "palette list should overflow and scroll"
    strips_after = app.screen._compositor.render_strips()
    assert strips_before != strips_after, "scrolling should change the rendered screen"

    # Allowed region is the palette's real visible chrome, never the root
    # Screen or its full-screen Vertical columns: union the actual visible
    # rects of the container, results overlay, input, list and scrollbars.
    screen_w, screen_h = app.size
    candidates = [
        palette.query_one("#--container").region,
        palette.query_one("#--results").region,
        palette.query_one("#--input").region,
        option_list.region,
        option_list.vertical_scrollbar.region,
        option_list.horizontal_scrollbar.region,
    ]
    # Headless visibility:hidden maps #--container/#--results to 0x0 regions;
    # they join the union only when they measure a real visible rectangle.
    allowed = [region for region in candidates if region.width > 0 and region.height > 0]
    assert allowed, "palette visible chrome must exist"
    assert all(region.width < screen_w or region.height < screen_h for region in allowed), allowed
    bbox_left = min(region.x for region in allowed)
    bbox_top = min(region.y for region in allowed)
    bbox_right = max(region.x + region.width for region in allowed)
    bbox_bottom = max(region.y + region.height for region in allowed)
    assert (bbox_left, bbox_top, bbox_right, bbox_bottom) != (0, 0, screen_w, screen_h)
    outside_grid = [
        (y, x) for y in range(screen_h) for x in range(screen_w) if not _in_allowed(x, y, allowed)
    ]
    # The allowed union must not cover the screen; real cells remain audited.
    assert outside_grid, allowed

    # Negative control: a violating cell just past the union boundary on each
    # side must be reported by the audit, proving the filter can actually fail.
    from rich.color import Color as RichColor
    from rich.style import Style as RichStyle

    bad_style = RichStyle(bgcolor=RichColor.from_rgb(127, 132, 156))
    mid_x = (bbox_left + bbox_right) // 2
    mid_y = (bbox_top + bbox_bottom) // 2
    directional = {
        "top": (bbox_top - 1, mid_x) if bbox_top > 0 else None,
        "bottom": (bbox_bottom, mid_x) if bbox_bottom < screen_h else None,
        "left": (mid_y, bbox_left - 1) if bbox_left > 0 else None,
        "right": (mid_y, bbox_right) if bbox_right < screen_w else None,
    }
    # The full-height results column leaves no on-screen space below the
    # palette; every other boundary direction must yield a real probe cell.
    if directional["bottom"] is None:
        assert bbox_bottom == screen_h, directional
    edge_points = [
        point
        for point in directional.values()
        if point is not None and not _in_allowed(point[1], point[0], allowed)
    ]
    assert edge_points, directional
    input_rect = palette.query_one("#--input").region
    inner_point = (input_rect.y + input_rect.height // 2, input_rect.x + input_rect.width // 2)
    for point in edge_points + [inner_point]:
        y, x = point
        assert 0 <= y < screen_h and 0 <= x < screen_w, point
    for point in edge_points:
        assert not _in_allowed(point[1], point[0], allowed), point
    assert _in_allowed(inner_point[1], inner_point[0], allowed), inner_point
    rows: dict[int, list[int]] = {}
    for y, x in edge_points + [inner_point]:
        rows.setdefault(y, []).append(x)
    strips_probe = [
        _strip_line_with_violations(screen_w, rows[y], bad_style) if y in rows else []
        for y in range(screen_h)
    ]
    reported = _outside_allowed(_bad_cells_in_strips(strips_probe), allowed)
    assert reported, "audit must fail for violations outside the panel"
    for point in edge_points:
        assert point in reported, (point, reported)
    assert inner_point not in reported

    outside = _outside_allowed(_bad_cells_in_strips(strips_after), allowed)
    assert outside == [], (allowed, outside)
    await pilot.press("escape")
    await pilot.pause()
    assert nondefault_background_cells(app) == []


def test_narrow_scrollbar_and_palette_surroundings_keep_default_background() -> None:
    """Scrolled tables and the palette overlay keep the default background at 60x24."""
    run_pilot_at((60, 24), _narrow_scroll_background_scenario)


async def _large_table_scroll_scenario(app: CCModelsApp, pilot) -> None:
    """A table big enough for both scrollbars, audited at partial positions."""
    table = app.query_one("#table", ModelTable)
    # Pad the data table with fake rows so both scroll directions overflow.
    original_row_count = len(app._row_metas)
    for index in range(200 - original_row_count):
        table.add_row("", "", f"filler-{index}", f"{index:04d}-" + "x" * 300, "")
    await pilot.pause()
    assert table.show_vertical_scrollbar and table.show_horizontal_scrollbar

    for theme_name in ("cc-models-mocha-ansi", "ansi-dark", "ansi-light"):
        app.theme = theme_name
        await pilot.pause()
        for scroll in (
            lambda: table.scroll_end(animate=False),
            lambda: table.scroll_home(animate=False),
            lambda: table.scroll_right(animate=False),
            lambda: table.scroll_left(animate=False),
            lambda: table.scroll_to(
                x=10,
                y=len(app._row_metas) // 2,
                animate=False,
                immediate=True,
            ),
        ):
            scroll()
            await pilot.pause()
            assert nondefault_background_cells(app) == [], theme_name


def test_large_table_both_scrollbars_partial_positions_keep_default_background() -> None:
    """Vertical+horizontal thumbs at head/tail/middle stay on default background."""
    run_pilot(_large_table_scroll_scenario)


@pytest.mark.parametrize("vertical", [True, False])
def test_scrollbar_render_thumb_is_whole_cell_quantized_foreground(vertical: bool) -> None:
    """Every grab cell becomes the fixed block glyph; no reverse, no bgcolor."""
    from rich.color import Color as RichColor
    from rich.color import ColorType
    from textual.color import Color

    from cc_models.app import _DefaultBgScrollBarRender

    bar_colors = (
        Color.parse("ansi_blue").rich_color,
        Color.parse("ansi_bright_blue").rich_color,
        Color.parse("#7f849c").rich_color,
        RichColor.from_ansi(196),
        RichColor.default(),
    )
    for bar_color in bar_colors:
        segments = _DefaultBgScrollBarRender.render_bar(
            size=8,
            virtual_size=40,
            window_size=20,
            position=2,
            vertical=vertical,
            back_color=Color.parse("ansi_default").rich_color,
            bar_color=bar_color,
        )
        glyph = "▐" if vertical else "▄"
        for seg in segments.segments:
            style = seg.style
            if style is None:
                continue
            is_grab = (style.meta or {}).get("@mouse.down") == "grab"
            if not is_grab:
                assert not style.reverse
                continue
            assert set(seg.text) == {glyph}, seg
            assert not style.reverse, seg
            assert style.bgcolor is None or style.bgcolor.type == ColorType.DEFAULT, seg
            assert style.color == bar_color, seg
        meta = [s.style.meta for s in segments.segments if s.style and s.style.meta]
        assert any("@mouse.down" in m for m in meta)


def test_scrollbar_renderer_is_instance_scoped() -> None:
    """Importing and running CCModelsApp never touches the global renderer."""
    import textual.scrollbar as sb

    assert sb.ScrollBar.renderer is sb.ScrollBarRender

    run_pilot(lambda app, pilot: _noop_scenario(app, pilot))

    assert sb.ScrollBar.renderer is sb.ScrollBarRender


async def _noop_scenario(app: CCModelsApp, pilot) -> None:
    await pilot.pause()


def test_other_app_scrollbar_keeps_reverse_thumb() -> None:
    """A stock Textual app in the same process renders reverse thumbs as before."""
    import asyncio

    from rich.color import ColorType
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable

    class OtherApp(App[None]):
        def compose(self) -> ComposeResult:
            table = DataTable()
            table.add_columns("a", "b")
            for index in range(60):
                table.add_row(str(index), str(index))
            yield table

    async def _run() -> list[tuple[int, int]]:
        app = OtherApp()
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause()
            table = app.query_one(DataTable)
            table.scroll_end(animate=False)
            await pilot.pause()
            assert table.show_vertical_scrollbar
            bad = []
            for y, strip in enumerate(app.screen._compositor.render_strips()):
                x = 0
                for seg in strip:
                    style = seg.style
                    if (
                        style is not None
                        and style.reverse
                        and style.color is not None
                        and style.color.type != ColorType.DEFAULT
                    ):
                        bad.append((y, x))
                    x += len(seg.text)
            return bad

    bad = asyncio.run(_run())
    assert bad, "stock app should still use reverse-video thumbs"
