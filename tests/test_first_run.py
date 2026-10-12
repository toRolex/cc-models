"""First-run and empty-state behavior for CCModelsApp.

These tests cover the empty-state model, the ``#empty`` explanatory widget and
the startup fetch on a fresh non-demo session. Demo contexts and tripwire
fetchers keep every non-network assertion strict.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from textual.widgets import Input, Static

from cc_models.app import (
    EMPTY_TEXT,
    CCModelsApp,
    EmptyReason,
    FetchState,
    ModelTable,
    TableView,
    project_table,
)
from cc_models.domain import Catalog, PickerOption, Selection
from cc_models.session import SessionContext, build_demo_context, load_session

SIZE = (120, 40)
Scenario = Callable[[CCModelsApp, object], Awaitable[None]]

ENDPOINT = "http://localhost:2317/v1"


# ---------------------------------------------------------------- helpers


def run_scenario(ctx: SessionContext, scenario: Scenario) -> None:
    app = CCModelsApp(ctx)

    async def _main() -> None:
        async with app.run_test(size=SIZE) as pilot:
            await scenario(app, pilot)

    asyncio.run(_main())


def empty_widget(app: CCModelsApp) -> Static:
    return app.query_one("#empty", Static)


def table_row_count(app: CCModelsApp) -> int:
    return app.query_one("#table", ModelTable).row_count


def _catalog(model_ids: list[str]) -> Catalog:
    return Catalog(endpoint=ENDPOINT, fetched_at="2026-01-01", model_ids=list(model_ids))


def _selection(model_ids: list[str]) -> Selection:
    return Selection(options=[PickerOption(model_id, model_id) for model_id in model_ids])


# ---------------------------------------------------------------- test 6


def test_empty_registry_covers_every_reason() -> None:
    """The text registry has exactly one entry per EmptyReason member."""
    assert set(EMPTY_TEXT) == set(EmptyReason)
    assert len(EMPTY_TEXT) == len(EmptyReason)


@pytest.mark.parametrize(
    ("selection", "catalog", "filter_text", "fetch_state", "expected"),
    [
        (_selection([]), None, "", FetchState.IDLE, EmptyReason.NO_CATALOG),
        (_selection([]), None, "", FetchState.RUNNING, EmptyReason.FETCHING),
        (_selection([]), None, "", FetchState.FAILED, EmptyReason.FETCH_FAILED),
        (_selection([]), _catalog([]), "", FetchState.IDLE, EmptyReason.CATALOG_EMPTY),
        (
            _selection(["alpha"]),
            _catalog(["alpha", "beta"]),
            "zzz",
            FetchState.IDLE,
            EmptyReason.FILTERED,
        ),
        # ordered non-empty wins over a running fetch (priority step 4 before 5)
        (_selection(["alpha"]), None, "zzz", FetchState.RUNNING, EmptyReason.FILTERED),
    ],
)
def test_project_table_reason_priority(
    selection: Selection,
    catalog: Catalog | None,
    filter_text: str,
    fetch_state: FetchState,
    expected: EmptyReason,
) -> None:
    view = project_table(selection, catalog, filter_text, fetch_state)
    assert view.reason is expected
    assert view.rows == []


def test_project_table_orders_selected_first_then_catalog() -> None:
    view = project_table(_selection(["sel"]), _catalog(["sel", "other"]), "", FetchState.IDLE)
    assert view.reason is None
    assert view.rows == [("sel", True, False), ("other", False, False)]


def test_project_table_flags_selected_ids_missing_from_catalog() -> None:
    view = project_table(_selection(["absent"]), _catalog(["present"]), "", FetchState.IDLE)
    assert view.reason is None
    assert view.rows == [("absent", True, True), ("present", False, False)]


def test_table_view_rejects_rows_with_a_reason() -> None:
    with pytest.raises(ValueError):
        TableView([("x", True, False)], EmptyReason.NO_CATALOG)


def test_table_view_rejects_no_rows_without_a_reason() -> None:
    with pytest.raises(ValueError):
        TableView([], None)


# ---------------------------------------------------------------- test 5


def test_inert_by_default_shows_no_catalog_hint(tmp_path, monkeypatch) -> None:
    """A non-demo session mounted without the flag never fetches and explains itself."""
    target = tmp_path / "user.json"
    target.write_text("{}")
    ctx = load_session(target, tmp_path / "state")
    monkeypatch.delenv("CPA_BASE_URL", raising=False)

    def tripwire(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("inert session must not fetch on mount")

    monkeypatch.setattr("cc_models.app.fetch_catalog", tripwire)

    async def scenario(app: CCModelsApp, pilot) -> None:
        await pilot.pause()
        assert app._row_metas == []
        assert table_row_count(app) == 0
        assert empty_widget(app).display is True
        assert empty_widget(app).content == "尚无目录缓存 · 按 r 拉取模型目录"

    run_scenario(ctx, scenario)


# ---------------------------------------------------------------- test 7


def test_filter_mismatch_shows_filtered_hint() -> None:
    """A filter matching nothing hides all rows and explains the filter."""
    ctx = build_demo_context()

    async def scenario(app: CCModelsApp, pilot) -> None:
        await pilot.pause()
        assert empty_widget(app).display is False
        await pilot.press("/")
        await pilot.press(*"zzzz")
        await pilot.pause()
        assert table_row_count(app) == 0
        assert app._row_metas == []
        assert empty_widget(app).display is True
        assert empty_widget(app).content == "没有匹配的模型 · 按 / 修改过滤"
        await pilot.press("escape")
        await pilot.pause()
        # Esc only returns focus to the table: the filter value is unchanged
        # and the rows stay hidden.
        assert app.query_one("#filter", Input).value == "zzzz"
        assert table_row_count(app) == 0
        assert app._row_metas == []
        await pilot.press("/")
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert app.query_one("#filter", Input).value == ""
        assert empty_widget(app).display is False
        assert table_row_count(app) == 4

    run_scenario(ctx, scenario)
