"""First-run and empty-state behavior for CCModelsApp."""

from __future__ import annotations

import asyncio
import json
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
    run_app,
)
from cc_models.domain import Catalog, NetworkError, PickerOption, Selection
from cc_models.session import SessionContext, build_demo_context, load_session
from cc_models.store import load_state

SIZE = (120, 40)
Scenario = Callable[[CCModelsApp, object], Awaitable[None]]

ENDPOINT = "http://localhost:2317/v1"


def run_scenario(ctx: SessionContext, scenario: Scenario, *, fetch_on_start: bool = False) -> None:
    app = CCModelsApp(ctx, fetch_on_start=fetch_on_start)

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


def test_empty_registry_covers_every_reason() -> None:
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


def test_inert_by_default_shows_no_catalog_hint(tmp_path, monkeypatch) -> None:
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


def test_filter_mismatch_shows_filtered_hint() -> None:
    from test_app import nondefault_background_cells

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
        assert nondefault_background_cells(app) == []
        await pilot.press("/")
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert app.query_one("#filter", Input).value == ""
        assert empty_widget(app).display is False
        assert table_row_count(app) == 4

    run_scenario(ctx, scenario)


def test_startup_fetch_fills_fresh_session(tmp_path, monkeypatch) -> None:
    target = tmp_path / "user.json"
    target.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:2317",
                    "ANTHROPIC_AUTH_TOKEN": "fixture-token",
                }
            }
        )
    )
    ctx = load_session(target, tmp_path / "state")
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    seen: list[str] = []

    def fetch(endpoint: str, **_kwargs: object) -> Catalog:
        seen.append(endpoint)
        return Catalog(endpoint=endpoint, fetched_at="2026-01-01", model_ids=["a", "b", "c"])

    monkeypatch.setattr("cc_models.app.fetch_catalog", fetch)

    async def scenario(app: CCModelsApp, pilot) -> None:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert seen == ["http://127.0.0.1:2317/v1"]
        assert table_row_count(app) == 3
        assert app.ctx.catalog is not None
        assert empty_widget(app).display is False
        cache = load_state(ctx.state_dir).cache
        assert cache is not None
        assert cache.model_ids == ["a", "b", "c"]
        assert cache.endpoint == "http://127.0.0.1:2317/v1"

    run_scenario(ctx, scenario, fetch_on_start=True)


def test_startup_fetch_failure_explains_and_recovers(tmp_path, monkeypatch) -> None:
    target = tmp_path / "user.json"
    target.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:2317"}}))
    ctx = load_session(target, tmp_path / "state")
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    calls: list[str] = []

    def fetch(endpoint: str, **_kwargs: object) -> Catalog:
        calls.append(endpoint)
        if len(calls) == 1:
            raise NetworkError("connection refused")
        return Catalog(endpoint=endpoint, fetched_at="2026-01-01", model_ids=["recovered"])

    monkeypatch.setattr("cc_models.app.fetch_catalog", fetch)

    async def scenario(app: CCModelsApp, pilot) -> None:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(calls) == 1
        assert table_row_count(app) == 0
        assert empty_widget(app).display is True
        assert empty_widget(app).content == "目录拉取失败 · 按 r 重试"
        assert load_state(ctx.state_dir).cache is None

        await pilot.press("r")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(calls) == 2
        assert table_row_count(app) == 1
        assert empty_widget(app).display is False
        assert load_state(ctx.state_dir).cache is not None

    run_scenario(ctx, scenario, fetch_on_start=True)


def test_demo_never_fetches_even_when_armed(monkeypatch) -> None:
    ctx = build_demo_context()

    def tripwire(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("demo session must never fetch")

    monkeypatch.setattr("cc_models.app.fetch_catalog", tripwire)

    async def scenario(app: CCModelsApp, pilot) -> None:
        await pilot.pause()
        assert table_row_count(app) == 4
        assert empty_widget(app).display is False

    run_scenario(ctx, scenario, fetch_on_start=True)


def test_config_rebind_clears_failed_hint(tmp_path, monkeypatch) -> None:
    target = tmp_path / "user.json"
    target.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:2317"}}))
    ctx = load_session(target, tmp_path / "state")
    monkeypatch.delenv("CPA_BASE_URL", raising=False)

    def fetch(_endpoint: str, **_kwargs: object) -> Catalog:
        raise NetworkError("connection refused")

    monkeypatch.setattr("cc_models.app.fetch_catalog", fetch)

    async def scenario(app: CCModelsApp, pilot) -> None:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert empty_widget(app).content == "目录拉取失败 · 按 r 重试"

        await pilot.press("c")
        await pilot.pause()
        modal = app.screen
        endpoint_input = modal.query_one("#endpoint-input", Input)
        await pilot.click("#endpoint-input")
        await pilot.pause()
        await pilot.press(*(["end"] + ["backspace"] * len(endpoint_input.value)))
        await pilot.press(*"https://b.example/v1")
        await pilot.click("#config-save")
        await pilot.pause()

        assert app._fetch_state is FetchState.IDLE
        assert empty_widget(app).display is True
        assert empty_widget(app).content == "尚无目录缓存 · 按 r 拉取模型目录"
        assert load_state(ctx.state_dir).config.endpoint == "https://b.example/v1"

    run_scenario(ctx, scenario, fetch_on_start=True)


def test_run_app_arms_fetch_on_start(monkeypatch) -> None:
    recorded: list[bool] = []

    class _Recorder:
        def __init__(self, ctx: SessionContext, *, fetch_on_start: bool = False) -> None:
            recorded.append(fetch_on_start)

        def run(self) -> None:
            return None

    monkeypatch.setattr("cc_models.app.CCModelsApp", _Recorder)
    ctx = build_demo_context()
    run_app(ctx)
    assert recorded == [True]


def test_warm_cache_with_empty_catalog_shows_upstream_empty(tmp_path, monkeypatch) -> None:
    from cc_models.session import resolve_endpoint
    from cc_models.store import record_fetch

    target = tmp_path / "user.json"
    target.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:2317"}}))
    ctx = load_session(target, tmp_path / "state")
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    endpoint = resolve_endpoint(ctx).raw
    record_fetch(
        ctx.state_dir,
        Catalog(endpoint=endpoint, fetched_at="2026-01-01", model_ids=[]),
        ctx.selection,
    )
    # Restart like a real second run so the app sees the seeded state dir.
    ctx = load_session(target, ctx.state_dir)

    def tripwire(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("warm cache must satisfy the startup fetch")

    monkeypatch.setattr("cc_models.app.fetch_catalog", tripwire)

    async def scenario(app: CCModelsApp, pilot) -> None:
        await pilot.pause()
        assert table_row_count(app) == 0
        assert app._row_metas == []
        assert empty_widget(app).display is True
        assert empty_widget(app).content == "上游未返回任何模型 · 按 r 重试"

    run_scenario(ctx, scenario, fetch_on_start=True)
