import asyncio
import json

from textual.widgets import Checkbox, Input

from cc_models.app import CCModelsApp, ConfigModal
from cc_models.domain import Catalog
from cc_models.session import load_session, resolve_api_key, resolve_endpoint
from cc_models.store import load_state


def test_real_settings_env_shape_and_token_host_boundary(tmp_path):
    target = tmp_path / "user.json"
    target.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:2317",
                    "ANTHROPIC_AUTH_TOKEN": "private-fixture",
                }
            }
        )
    )
    ctx = load_session(target, tmp_path / "state")
    endpoint = resolve_endpoint(ctx, {})
    assert endpoint.raw == "http://127.0.0.1:2317/v1"
    assert resolve_api_key(ctx, endpoint, {}) == (
        "private-fixture",
        "设置文件 ANTHROPIC_AUTH_TOKEN（仅同一端点回退）",
    )
    other = type(endpoint)("https://example.com/v1", "override", False, "CPA_API_KEY")
    assert resolve_api_key(ctx, other, {}) == (None, "无")


def test_remote_http_requires_checkbox(tmp_path):
    async def exercise():
        target = tmp_path / "user.json"
        target.write_text("{}")
        ctx = load_session(target, tmp_path / "state")
        app = CCModelsApp(ctx)
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.press("c")
            await pilot.pause()
            app.screen.query_one("#endpoint-input", Input).value = "http://100.64.0.1:8317/v1"
            await pilot.click("#config-save")
            await pilot.pause()
            assert ctx.state.config.endpoint is None
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigModal)
            app.screen.query_one("#endpoint-input", Input).value = "http://100.64.0.1:8317/v1"
            app.screen.query_one("#insecure-http", Checkbox).value = True
            await pilot.click("#config-save")
            await pilot.pause()
            assert load_state(ctx.state_dir).config.endpoint == "http://100.64.0.1:8317/v1"
            assert load_state(ctx.state_dir).config.allow_insecure_http is True

    asyncio.run(exercise())


def test_non_demo_refresh_worker(tmp_path, monkeypatch):
    async def exercise():
        target = tmp_path / "user.json"
        target.write_text("{}")
        ctx = load_session(target, tmp_path / "state")
        seen = []

        def fetch(endpoint, **kwargs):
            seen.append(endpoint)
            return Catalog(
                endpoint="http://localhost:2317/v1",
                fetched_at="2026-01-01",
                model_ids=["raw-model"],
            )

        monkeypatch.setattr("cc_models.app.fetch_catalog", fetch)
        app = CCModelsApp(ctx)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert seen == ["http://localhost:2317/v1"]
            assert ctx.catalog is not None
            assert ctx.catalog.model_ids == ["raw-model"]
            cache = load_state(ctx.state_dir).cache
            assert cache is not None
            assert cache.model_ids == ["raw-model"]

    asyncio.run(exercise())


def test_slow_refresh_cannot_rebind_after_config_change(tmp_path, monkeypatch):
    import threading

    async def exercise():
        target = tmp_path / "user.json"
        target.write_text("{}")
        ctx = load_session(target, tmp_path / "state")
        started, release = threading.Event(), threading.Event()

        def fetch(endpoint, **kwargs):
            started.set()
            assert release.wait(5), "fetch release timed out"
            return Catalog(endpoint=endpoint, fetched_at="2026-01-01", model_ids=["stale-A"])

        monkeypatch.setattr("cc_models.app.fetch_catalog", fetch)
        app = CCModelsApp(ctx)
        async with app.run_test(size=(120, 50)) as pilot:
            try:
                await pilot.press("r")
                assert await asyncio.to_thread(started.wait, 3)
                await pilot.press("c")
                app.screen.query_one("#endpoint-input", Input).value = "https://b.example/v1"
                await pilot.click("#config-save")
                await pilot.pause()
                assert ctx.state.config.endpoint == "https://b.example/v1"
            finally:
                release.set()
            await asyncio.wait_for(app.workers.wait_for_complete(), 5)
            await pilot.pause()
            assert ctx.catalog is None
            assert load_state(ctx.state_dir).cache is None
            assert all(row[0] != "stale-A" for row in app._row_metas)

    asyncio.run(exercise())


def test_offline_cached_startup_preserves_missing_selection(tmp_path, monkeypatch):
    from cc_models.store import record_fetch

    async def exercise():
        target = tmp_path / "user.json"
        target.write_text(
            json.dumps({"modelPicker": {"options": [{"model": "absent", "label": "Absent"}]}})
        )
        ctx = load_session(target, tmp_path / "state")
        record_fetch(
            ctx.state_dir,
            Catalog(
                endpoint="http://localhost:2317/v1", fetched_at="2026-01-01", model_ids=["cached"]
            ),
            ctx.selection,
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("offline startup must not fetch")

        monkeypatch.setattr("cc_models.app.fetch_catalog", forbidden)
        restarted = load_session(target, ctx.state_dir)
        app = CCModelsApp(restarted)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._row_metas == [("absent", True, True), ("cached", False, False)]
            assert restarted.selection.ids == ["absent"]

    asyncio.run(exercise())


def test_config_only_import_and_replace_blank_survive_restart(tmp_path, monkeypatch):
    from cc_models.store import save_state as real_save_state

    async def exercise():
        target = tmp_path / "user.json"
        target.write_text(
            json.dumps(
                {
                    "modelPicker": {
                        "replaceBuiltInOptions": False,
                        "options": [{"model": "imported", "label": "Imported"}],
                    }
                }
            )
        )
        ctx = load_session(target, tmp_path / "state")
        writes = []

        def save(directory, state):
            writes.append(state.selection.ids)
            return real_save_state(directory, state)

        monkeypatch.setattr("cc_models.app.save_state", save, raising=False)
        app = CCModelsApp(ctx)
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.press("c")
            assert app.screen.query_one("#replace-input", Input).value == ""
            app.screen.query_one("#endpoint-input", Input).value = "https://b.example"
            await pilot.click("#config-save")
            await pilot.pause()
            restarted = load_session(target, ctx.state_dir)
            assert restarted.selection.ids == ["imported"]
            assert restarted.selection.replace_built_in is None
            assert writes == [["imported"]]
            await pilot.press("c")
            app.screen.query_one("#replace-input", Input).value = "t"
            await pilot.click("#config-save")
            await pilot.pause()
            assert load_session(target, ctx.state_dir).selection.replace_built_in is True
            await pilot.press("c")
            assert app.screen.query_one("#replace-input", Input).value == "t"
            app.screen.query_one("#replace-input", Input).value = ""
            await pilot.click("#config-save")
            await pilot.pause()
            restarted = load_session(target, ctx.state_dir)
            assert restarted.selection.replace_built_in is None
            assert restarted.selection.ids == ["imported"]
            assert writes == [["imported"], ["imported"], ["imported"]]

    asyncio.run(exercise())


def test_invalid_environment_endpoint_never_displays_secret(tmp_path, monkeypatch):
    async def exercise():
        target = tmp_path / "user.json"
        target.write_text("{}")
        monkeypatch.setenv("CPA_BASE_URL", "https://user:private-url@example.com?key=query-secret")
        ctx = load_session(target, tmp_path / "state")
        notices = []
        app = CCModelsApp(ctx)
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notices.append(message))
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.press("r", "c")
            value = app.screen.query_one("#endpoint-input", Input).value
            assert "private-url" not in value and "query-secret" not in value
            assert "private-url" not in str(notices) and "query-secret" not in str(notices)

    asyncio.run(exercise())


def test_cli_http_opt_in_before_source_validation(tmp_path, monkeypatch):
    from cc_models.cli import _resolution, build_parser

    target = tmp_path / "user.json"
    target.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://remote.example:8317/v1"}}))
    ctx = load_session(target, tmp_path / "state")
    args = build_parser().parse_args(["--allow-insecure-http", "refresh"])
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    assert _resolution(ctx, args).raw == "http://remote.example:8317/v1"
    monkeypatch.setenv("CPA_BASE_URL", "http://other.example:8317")
    assert _resolution(ctx, args).raw == "http://other.example:8317/v1"
    monkeypatch.setenv("CPA_BASE_URL", "http://user:secret@other.example")
    assert _resolution(ctx, args).raw == "（端点无效，请重新配置）"
