"""Export real demo TUI screenshots without touching user settings or the network.

Run from the repository root with `uv run --locked python scripts/export_readme_screenshots.py`.
Convert the resulting SVGs with rsvg-convert (see docs/assets/README.md).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from textual.widgets import Input

from cc_models.app import CCModelsApp, ModelTable, PreviewModal
from cc_models.session import build_demo_context

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"


async def export() -> None:
    ctx = build_demo_context()
    app = CCModelsApp(ctx)
    try:
        async with app.run_test(size=(110, 14)) as pilot:
            await pilot.pause()
            table = app.query_one("#table", ModelTable)
            # A–Z order: Haiku, Opus, Sonnet, custom model.
            await pilot.press("j")
            assert table.cursor_row == 1
            await pilot.press("d", "e")
            await pilot.pause()
            app.screen.query_one("#rename-input", Input).value = "Opus · Coding"
            await pilot.press("enter")
            await pilot.pause()
            app.screen.query_one("#description-input", Input).value = "日常编码与复杂任务"
            await pilot.press("enter")
            await pilot.pause()
            assert ctx.selection.options[0].label == "Opus · Coding"
            await pilot.press("j", "space")
            await pilot.pause()
            assert "claude-sonnet-4-6" in ctx.selection.ids
            assert ctx.selection.default_edit.value == "claude-opus-4-6"
            # Let startup/action notifications expire before exporting.
            await pilot.pause(4)
            svg = app.export_screenshot(title="cc-models · Model selection (demo)")
            assert "Coding" in svg and "claude-sonnet-4-6" in svg
            (ASSETS / "screenshot-model-list.svg").write_text(svg, encoding="utf-8")
            await pilot.resize_terminal(110, 46)
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, PreviewModal)
            svg = app.export_screenshot(title="cc-models · Apply preview (demo)")
            assert "应用预览" in svg and "Coding" in svg
            (ASSETS / "screenshot-preview.svg").write_text(svg, encoding="utf-8")
            print("Exported demo model list and apply preview SVGs (no apply performed).")
    finally:
        shutil.rmtree(ctx.settings_path.parent)


if __name__ == "__main__":
    asyncio.run(export())
