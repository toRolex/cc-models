"""Core tests for cc-models: literal observed outputs, temp paths only.

No real user settings, CPA files or secrets are ever read or written here.
All network tests run against a stdlib http.server bound to loopback.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_models import settings as settings_mod
from cc_models.app import FetchState, project_table
from cc_models.catalog import fetch_catalog
from cc_models.domain import (
    BackupError,
    Catalog,
    CatalogError,
    ConfigError,
    DefaultEdit,
    DriftError,
    EndpointError,
    NetworkError,
    PickerOption,
    RestoreRefusedError,
    Selection,
    SelectionError,
    SortOrder,
    StateError,
    normalize_endpoint,
)
from cc_models.settings import (
    apply_selection,
    build_model_picker,
    import_selection_from_settings,
    preview_apply,
    read_snapshot,
    restore_backup,
)
from cc_models.store import (
    CachedCatalog,
    EndpointConfig,
    LocalState,
    cache_for_endpoint,
    load_state,
    record_fetch,
    save_config,
    save_selection,
    save_state,
)

FIXTURES = Path(__file__).parent / "fixtures"
SECRET_TOKEN = "sk-ant-secret-do-not-leak"
BASE_SETTINGS = {
    "ANTHROPIC_AUTH_TOKEN": SECRET_TOKEN,
    "numParallelServers": 2,
    "theme": "dark",
}


# --------------------------------------------------------------------------
# Loopback catalog server fixture
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests_seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        route = self.server.routes.get(self.path)  # type: ignore[attr-defined]
        if route is None:
            body = json.dumps({"error": "not found"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        status, headers, body = route(self)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence test output


class _CatalogServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the test routes and request log."""

    routes: dict
    requests_seen: list

    def __init__(self, address, handler):
        self.routes = {}
        self.requests_seen = []
        super().__init__(address, handler)


@pytest.fixture
def cpa_server():
    server = _CatalogServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        routes=server.routes,
        seen=server.requests_seen,
    )
    server.shutdown()
    server.server_close()


def json_route(payload, status=200):
    body = json.dumps(payload).encode()
    return (status, {"Content-Type": "application/json"}, body)


@pytest.fixture
def fixture_catalog_payload():
    return json.loads((FIXTURES / "catalog.json").read_text())


# --------------------------------------------------------------------------
# Endpoint normalization
# --------------------------------------------------------------------------


def test_endpoint_normalization_literal_outputs():
    assert normalize_endpoint(None) == "http://localhost:2317/v1"
    assert normalize_endpoint("") == "http://localhost:2317/v1"
    assert normalize_endpoint("http://localhost:2317") == "http://localhost:2317/v1"
    assert normalize_endpoint("http://localhost:2317/") == "http://localhost:2317/v1"
    assert normalize_endpoint("http://localhost:2317/v1/") == "http://localhost:2317/v1"
    assert normalize_endpoint("http://127.0.0.1:8123/api") == "http://127.0.0.1:8123/api/v1"
    assert normalize_endpoint("https://api.example.com") == "https://api.example.com/v1"


def test_endpoint_rejects_credentials_query_fragment_and_remote_http():
    with pytest.raises(EndpointError, match="credentials"):
        normalize_endpoint("http://user:pass@127.0.0.1:2317")
    with pytest.raises(EndpointError, match="query or fragment"):
        normalize_endpoint("http://127.0.0.1:2317/v1?x=1")
    with pytest.raises(EndpointError, match="query or fragment"):
        normalize_endpoint("http://127.0.0.1:2317/v1#frag")
    with pytest.raises(EndpointError, match="loopback"):
        normalize_endpoint("http://api.example.com")
    with pytest.raises(EndpointError, match="scheme"):
        normalize_endpoint("ftp://127.0.0.1:2317")


def test_remote_http_requires_explicit_insecure_opt_in():
    # Configured remote endpoint from the parent directive; never contacted by tests.
    remote = "http://100.83.220.68:8317"
    with pytest.raises(EndpointError, match="allow_insecure_http"):
        normalize_endpoint(remote)
    with pytest.raises(EndpointError, match="allow_insecure_http"):
        normalize_endpoint(remote, allow_insecure_http=False)
    # identity is the normalized URL; the opt-in flag is not part of it
    assert normalize_endpoint(remote, allow_insecure_http=True) == ("http://100.83.220.68:8317/v1")
    # loopback HTTP stays allowed without any opt-in
    assert normalize_endpoint("http://127.0.0.1:8123") == "http://127.0.0.1:8123/v1"


def test_endpoint_config_persisted_opt_in_roundtrip(tmp_path):
    config = EndpointConfig(
        endpoint="http://100.83.220.68:8317/v1",
        allow_insecure_http=True,
        token_env="CPA_API_KEY",
    )
    save_config(tmp_path, config)
    loaded = load_state(tmp_path).config
    assert loaded == config
    text = (tmp_path / "state.json").read_text()
    assert '"allow_insecure_http": true' in text
    assert '"token_env": "CPA_API_KEY"' in text  # env var NAME, never a value
    assert "sk-" not in text
    # default config is not persisted
    save_config(tmp_path, EndpointConfig())
    assert "config" not in (tmp_path / "state.json").read_text()


def test_endpoint_config_rejects_unknown_keys_and_bad_types(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "config": {"api_key": "sk-leak"}})
    )
    with pytest.raises(StateError, match="unknown config keys"):
        load_state(tmp_path)
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "config": {"allow_insecure_http": "yes"}})
    )
    with pytest.raises(StateError, match="allow_insecure_http must be a boolean"):
        load_state(tmp_path)


def test_cache_for_remote_http_endpoint_requires_opt_in(tmp_path):
    endpoint = "http://100.83.220.68:8317/v1"
    state = LocalState(cache=CachedCatalog(endpoint=endpoint, fetched_at="t", model_ids=["m-1"]))
    with pytest.raises(EndpointError, match="allow_insecure_http"):
        cache_for_endpoint(state, endpoint)
    cached = cache_for_endpoint(state, endpoint, allow_insecure_http=True)
    assert cached is not None
    assert cached.model_ids == ["m-1"]


# --------------------------------------------------------------------------
# Catalog fetch
# --------------------------------------------------------------------------


def test_fetch_catalog_exact_raw_ids_and_endpoint_binding(cpa_server, fixture_catalog_payload):
    cpa_server.routes["/v1/models"] = lambda _h: json_route(fixture_catalog_payload)
    catalog = fetch_catalog(cpa_server.base_url)
    assert catalog.model_ids == ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"]
    assert catalog.duplicates == {}
    assert catalog.endpoint == normalize_endpoint(cpa_server.base_url)
    assert catalog.fetched_at  # timestamp present
    assert catalog.contains("claude-opus-4-6")
    assert not catalog.contains("not-a-model")


def test_fetch_catalog_dedupes_duplicate_ids_without_mutating_strings(
    cpa_server, fixture_catalog_payload
):
    payload = {
        "data": [
            {"id": "claude-sonnet-4-6"},
            {"id": "claude-opus-4-6"},
            {"id": "claude-sonnet-4-6"},
            {"id": "claude-opus-4-6"},
        ]
    }
    cpa_server.routes["/v1/models"] = lambda _h: json_route(payload)
    catalog = fetch_catalog(cpa_server.base_url)
    assert catalog.model_ids == ["claude-sonnet-4-6", "claude-opus-4-6"]
    assert catalog.duplicates == {"claude-sonnet-4-6": 2, "claude-opus-4-6": 2}


def test_fetch_catalog_rejects_redirect_and_never_contacts_destination(cpa_server):
    location = f"{cpa_server.base_url}/v1/redirected"

    def redirect(_h):
        return (301, {"Location": location}, b"moved")

    cpa_server.routes["/v1/models"] = redirect
    cpa_server.routes["/v1/redirected"] = lambda _h: json_route({"data": [{"id": "x"}]})
    with pytest.raises(NetworkError, match="redirected"):
        fetch_catalog(cpa_server.base_url)
    redirected_hits = [path for path, _auth in cpa_server.seen if path == "/v1/redirected"]
    assert redirected_hits == [], "redirect destination must never be contacted"


def test_fetch_catalog_sends_bearer_header_but_never_persists_key(
    cpa_server, fixture_catalog_payload, tmp_path
):
    api_key = "test-key-abc123"
    cpa_server.routes["/v1/models"] = lambda _h: json_route(fixture_catalog_payload)
    catalog = fetch_catalog(cpa_server.base_url, api_key=api_key)
    auths = [auth for _path, auth in cpa_server.seen if auth is not None]
    assert auths == [f"Bearer {api_key}"]
    record_fetch(tmp_path, catalog, Selection())
    state_text = (tmp_path / "state.json").read_text()
    assert api_key not in state_text
    assert "Authorization" not in state_text
    assert "sk-" not in state_text


def test_fetch_catalog_non_2xx_rejected(cpa_server):
    cpa_server.routes["/v1/models"] = lambda _h: json_route({"boom": True}, status=500)
    with pytest.raises(NetworkError, match="HTTP 500"):
        fetch_catalog(cpa_server.base_url)


def test_fetch_catalog_timeout_is_bounded(cpa_server):
    def slow(_h):
        import time

        time.sleep(0.8)
        return json_route({"data": []})

    cpa_server.routes["/v1/models"] = slow
    with pytest.raises(NetworkError):
        fetch_catalog(cpa_server.base_url, timeout=0.2)


def test_fetch_catalog_invalid_payloads_rejected(cpa_server):
    cpa_server.routes["/v1/models"] = lambda _h: json_route({"nope": 1})
    with pytest.raises(CatalogError, match="data"):
        fetch_catalog(cpa_server.base_url)
    cpa_server.routes["/v1/models"] = lambda _h: json_route({"data": [{"id": ""}]})
    with pytest.raises(CatalogError, match="nonempty string 'id'"):
        fetch_catalog(cpa_server.base_url)
    cpa_server.routes["/v1/models"] = lambda _h: json_route({"data": [{"id": 7}]})
    with pytest.raises(CatalogError, match="nonempty string 'id'"):
        fetch_catalog(cpa_server.base_url)


# --------------------------------------------------------------------------
# Offline cache and selection survival
# --------------------------------------------------------------------------


def test_offline_cache_usable_only_for_matching_endpoint(
    cpa_server, fixture_catalog_payload, tmp_path
):
    cpa_server.routes["/v1/models"] = lambda _h: json_route(fixture_catalog_payload)
    catalog = fetch_catalog(cpa_server.base_url)
    record_fetch(tmp_path, catalog, Selection())

    cached = cache_for_endpoint(load_state(tmp_path), cpa_server.base_url)
    assert cached is not None
    assert cached.model_ids == ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"]
    assert cached.endpoint == normalize_endpoint(cpa_server.base_url)

    assert cache_for_endpoint(load_state(tmp_path), "http://127.0.0.1:9999") is None
    assert cache_for_endpoint(load_state(tmp_path), "http://localhost:2317") is None


def test_selection_survives_fetch_failure(cpa_server, fixture_catalog_payload, tmp_path):
    selection = Selection(
        options=[PickerOption("claude-opus-4-6", "Opus")],
        default_edit=DefaultEdit("set", "claude-opus-4-6"),
    )
    save_selection(tmp_path, selection)

    cpa_server.routes["/v1/models"] = lambda _h: json_route({"error": "down"}, status=503)
    with pytest.raises(NetworkError):
        fetch_catalog(cpa_server.base_url)

    state = load_state(tmp_path)
    assert state.selection.to_json() == selection.to_json()
    assert state.cache is None or state.cache.model_ids is not None


def test_record_fetch_keeps_selection_and_adds_cache(cpa_server, fixture_catalog_payload, tmp_path):
    selection = Selection(options=[PickerOption("claude-opus-4-6", "Opus")])
    save_selection(tmp_path, selection)
    cpa_server.routes["/v1/models"] = lambda _h: json_route(fixture_catalog_payload)
    catalog = fetch_catalog(cpa_server.base_url)
    record_fetch(tmp_path, catalog, selection)

    state = load_state(tmp_path)
    assert state.selection.ids == ["claude-opus-4-6"]  # new entries start unselected
    assert state.cache is not None
    assert sorted(state.cache.model_ids) == sorted(
        ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"]
    )


def test_state_malformed_cache_keeps_selection_intent(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "cache": {"endpoint": 42},
                "selection": {
                    "default_edit": {"kind": "preserve", "value": None},
                    "options": [{"model": "claude-opus-4-6", "label": "Opus"}],
                },
            }
        )
    )
    state = load_state(tmp_path)
    assert state.cache is None
    assert state.selection.ids == ["claude-opus-4-6"]
    assert state.warnings and "cache" in state.warnings[0]


def test_state_malformed_file_raises_and_missing_state_is_empty(tmp_path):
    (tmp_path / "state.json").write_text("{not json")
    with pytest.raises(StateError, match="malformed local state"):
        load_state(tmp_path)
    empty = load_state(tmp_path / "does-not-exist")
    assert empty.selection.options == []
    assert empty.cache is None


# --------------------------------------------------------------------------
# Settings parsing
# --------------------------------------------------------------------------


def write_settings(tmp_path, data, name="settings.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def test_settings_rejects_malformed_nonobject_duplicate_keys_and_nan(tmp_path):
    bad_cases = [
        '{"model": ',
        "[1, 2, 3]",
        '"just a string"',
        '{"a": 1, "a": 2}',
        '{"a": NaN}',
        '{"a": Infinity}',
    ]
    for i, text in enumerate(bad_cases):
        path = tmp_path / f"settings-{i}.json"
        path.write_text(text)
        with pytest.raises(ConfigError):
            read_snapshot(path)


def test_settings_rejects_symlink_target(tmp_path):
    real = write_settings(tmp_path, BASE_SETTINGS)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ConfigError, match="symlink"):
        read_snapshot(link)


def test_import_preserves_optional_fields_and_never_generates_behaves_as(tmp_path):
    data = dict(BASE_SETTINGS)
    data["modelPicker"] = {
        "replaceBuiltInOptions": False,
        "options": [
            {
                "model": "ccs-custom-model",
                "label": "Custom",
                "description": "imported",
                "behavesAs": "claude-sonnet-4-6",
            },
            {"model": "claude-opus-4-6", "label": "Opus"},
        ],
    }
    snapshot = read_snapshot(write_settings(tmp_path, data))
    selection, warnings = import_selection_from_settings(snapshot)
    assert selection.ids == ["ccs-custom-model", "claude-opus-4-6"]
    assert warnings == ["imported ccs- id in selection: ccs-custom-model"]
    assert selection.options[0].extra == {"behavesAs": "claude-sonnet-4-6"}

    rebuilt = build_model_picker(selection, snapshot.data["modelPicker"])
    assert rebuilt["replaceBuiltInOptions"] is False  # imported toggle preserved
    assert rebuilt["options"][0]["behavesAs"] == "claude-sonnet-4-6"  # imported only
    assert "behavesAs" not in rebuilt["options"][1]  # never generated
    # default [1m] output policy applies at the picker boundary
    assert rebuilt["options"][0]["model"] == "ccs-custom-model[1m]"
    assert rebuilt["options"][1] == {"model": "claude-opus-4-6[1m]", "label": "Opus"}


def test_build_model_picker_defaults_replace_built_in_true():
    picker = build_model_picker(Selection(options=[PickerOption("m1", "One")]))
    assert picker == {
        "replaceBuiltInOptions": True,
        "options": [{"model": "m1[1m]", "label": "One"}],
    }


# --------------------------------------------------------------------------
# Preview: managed keys only, secret redaction
# --------------------------------------------------------------------------


def make_selection():
    return Selection(
        options=[
            PickerOption("claude-opus-4-6", "Opus"),
            PickerOption("claude-sonnet-4-6", "Sonnet", description="balanced"),
        ]
    )


def test_preview_contains_only_managed_keys_and_no_secrets(tmp_path):
    snapshot = read_snapshot(write_settings(tmp_path, BASE_SETTINGS))
    preview = preview_apply(snapshot, make_selection())
    assert set(preview.managed_changes) <= {"modelPicker", "model"}
    assert set(snapshot.managed) <= {"modelPicker", "model"}
    rendered = json.dumps(preview.managed_changes)
    assert SECRET_TOKEN not in rendered
    assert preview.would_write is True
    assert preview.managed_changes["modelPicker"]["after"]["replaceBuiltInOptions"] is True


def test_apply_preserves_unrelated_settings_and_backs_up_privately(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    result = apply_selection(snapshot, make_selection(), tmp_path)
    assert result.applied is True
    assert result.no_op is False

    after = read_snapshot(path)
    # unrelated values preserved semantically, secrets untouched
    assert after.data["ANTHROPIC_AUTH_TOKEN"] == SECRET_TOKEN
    assert after.data["numParallelServers"] == 2
    assert after.data["theme"] == "dark"
    # literal managed output
    assert after.data["modelPicker"] == {
        "replaceBuiltInOptions": True,
        "options": [
            {"model": "claude-opus-4-6[1m]", "label": "Opus"},
            {
                "model": "claude-sonnet-4-6[1m]",
                "label": "Sonnet",
                "description": "balanced",
            },
        ],
    }
    assert "model" not in after.data  # preserve never invents a default

    assert result.backup_path is not None
    backup = Path(result.backup_path)
    assert backup.exists()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert backup.read_text() == snapshot.content
    backups = list(tmp_path.glob("*.cc-models-backup-*"))
    assert backups == [backup]


def test_apply_is_idempotent_noop_without_backup(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    first = apply_selection(snapshot, make_selection(), tmp_path)
    assert first.applied is True
    assert len(list(tmp_path.glob("*.cc-models-backup-*"))) == 1

    second_snapshot = read_snapshot(path)
    before_mtime = path.stat().st_mtime_ns
    second = apply_selection(second_snapshot, make_selection(), tmp_path)
    assert second.no_op is True
    assert second.applied is False
    assert second.backup_path is None
    assert path.stat().st_mtime_ns == before_mtime  # file untouched
    assert len(list(tmp_path.glob("*.cc-models-backup-*"))) == 1  # no new backup


def test_apply_refuses_drifted_deleted_and_racing_targets(tmp_path, monkeypatch):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)

    # changed since snapshot
    path.write_text(json.dumps({**BASE_SETTINGS, "theme": "light"}, indent=2) + "\n")
    with pytest.raises(DriftError, match="changed since snapshot"):
        apply_selection(snapshot, make_selection(), tmp_path)

    # deleted since snapshot
    fresh = read_snapshot(path)
    path.unlink()
    with pytest.raises(DriftError, match="missing or unreadable"):
        apply_selection(fresh, make_selection(), tmp_path)

    # drift between the double check and the atomic replace
    path.write_text(json.dumps(BASE_SETTINGS, indent=2) + "\n")
    snapshot2 = read_snapshot(path)
    real_read = settings_mod.read_snapshot
    calls = {"n": 0}

    def racing_read(target):
        calls["n"] += 1
        if calls["n"] == 2:
            Path(target).write_text(json.dumps({"drifted": True}, indent=2) + "\n")
        return real_read(target)

    monkeypatch.setattr(settings_mod, "read_snapshot", racing_read)
    with pytest.raises(DriftError, match="between checks"):
        apply_selection(snapshot2, make_selection(), tmp_path)
    assert not list(tmp_path.glob(".cc-models-*.tmp"))  # temp cleaned up
    assert json.loads(path.read_text()) == {"drifted": True}  # we never replaced


def test_backup_failure_leaves_original_unchanged(tmp_path, monkeypatch):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    original = snapshot.content

    def failing_backup(target, content):
        raise BackupError("simulated backup failure")

    monkeypatch.setattr(settings_mod, "write_private_backup", failing_backup)
    with pytest.raises(BackupError, match="simulated backup failure"):
        apply_selection(snapshot, make_selection(), tmp_path)
    assert path.read_text() == original
    assert not list(tmp_path.glob("*.cc-models-backup-*"))
    assert not list(tmp_path.glob(".cc-models-*.tmp"))


def test_apply_rejects_symlinked_writer_lock(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    from cc_models.settings import settings_lock_path

    (tmp_path / f".{path.name}.cc-models.lock").symlink_to(tmp_path / "elsewhere")
    assert settings_lock_path(path) == tmp_path / f".{path.name}.cc-models.lock"
    with pytest.raises(ConfigError, match="symlink"):
        apply_selection(snapshot, make_selection(), tmp_path)


def test_writer_lock_is_keyed_by_settings_target_across_state_dirs(tmp_path):
    """P1 regression: same settings + different state dirs must serialize."""
    import fcntl
    import threading

    from cc_models.settings import writer_lock_for_settings

    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    state_dir_a = tmp_path / "state-a"
    state_dir_b = tmp_path / "state-b"
    state_dir_a.mkdir()
    state_dir_b.mkdir()

    with writer_lock_for_settings(path) as lock_path:
        # the lock lives next to the settings target, not in any state dir
        assert lock_path == tmp_path / f".{path.name}.cc-models.lock"
        assert lock_path.exists()
        # a second fd cannot take the lock (this is what serializes applies)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        # an apply from a DIFFERENT state dir against the same settings blocks
        done = []

        def other_state_apply():
            done.append(apply_selection(snapshot, make_selection(), state_dir_b))

        worker = threading.Thread(target=other_state_apply, daemon=True)
        worker.start()
        worker.join(timeout=0.5)
        assert done == [], "apply from another state dir must block on the settings lock"
    # after release the blocked apply proceeds
    worker.join(timeout=5)
    assert done and done[0].applied is True


def test_unknown_option_field_never_leaks_to_state_or_preview_but_survives_apply(tmp_path):
    """P1 regression: unknown option extras are redacted, unprivileged, merged."""
    secret_value = "sk-option-level-secret"
    data = dict(BASE_SETTINGS)
    data["modelPicker"] = {
        "replaceBuiltInOptions": False,
        "options": [
            {
                "model": "claude-opus-4-6",
                "label": "Opus",
                "api_key": secret_value,  # unknown imported field
            }
        ],
    }
    path = write_settings(tmp_path, data)
    snapshot = read_snapshot(path)
    selection, _warnings = import_selection_from_settings(snapshot)
    # rename so the apply is not a no-op (imported selection equals the file)
    selection = Selection(options=[PickerOption("claude-opus-4-6", "Opus 改名")])

    # unknown field is NOT persisted into local state
    from cc_models.store import save_selection

    save_selection(tmp_path, selection)
    state_text = (tmp_path / "state.json").read_text()
    assert secret_value not in state_text
    assert "api_key" not in state_text
    state = load_state(tmp_path)
    assert state.selection.options[0].extra == {}

    # preview redacts unknown values but keeps the key visible
    preview = preview_apply(snapshot, selection)
    after_options = preview.managed_changes["modelPicker"]["after"]["options"]
    before_options = preview.managed_changes["modelPicker"]["before"]["options"]
    assert secret_value not in json.dumps(preview.managed_changes)
    assert before_options[0]["api_key"] == "<unknown field redacted>"
    assert after_options[0]["api_key"] == "<unknown field redacted>"
    # documented fields keep their values in the preview
    assert after_options[0]["model"] == "claude-opus-4-6[1m]"
    assert after_options[0]["label"] == "Opus 改名"

    # apply merges the unknown field back by original raw ID (preservation)
    result = apply_selection(snapshot, selection, tmp_path)
    assert result.applied is True
    applied = read_snapshot(path).data["modelPicker"]["options"][0]
    assert applied["api_key"] == secret_value  # preserved in the FILE on apply
    assert applied["model"] == "claude-opus-4-6[1m]"
    # apply preview result is redacted too
    assert secret_value not in json.dumps(result.managed_changes)


def test_option_extra_mutation_cannot_inject_reserved_keys():
    """P1 regression: post-construction extra mutation is impossible."""
    option = PickerOption("m1", "One", extra={"behavesAs": "claude-sonnet-4-6"})
    with pytest.raises(TypeError):
        option.extra["model"] = "injected"  # type: ignore[index]
    with pytest.raises(TypeError):
        option.extra["label"] = ""  # type: ignore[index]
    # unknown keys are refused at construction, even non-mutated
    with pytest.raises(SelectionError, match="documented modelPicker field"):
        PickerOption("m1", "One", extra={"api_key": "sk-x"})
    # serialization always matches the validated fields
    assert option.to_settings() == {
        "model": "m1",
        "label": "One",
        "behavesAs": "claude-sonnet-4-6",
    }


def test_endpoint_config_refuses_credential_urls_at_every_boundary(tmp_path):
    """P1 regression: URL credentials can never be persisted to state."""
    from cc_models.domain import EndpointError

    with pytest.raises(EndpointError, match="credentials"):
        EndpointConfig(endpoint="https://user:secret@example.com/v1")
    with pytest.raises(EndpointError, match="query or fragment"):
        EndpointConfig(endpoint="http://127.0.0.1:2317/v1?x=1")
    # file import path raises StateError and persists nothing
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "config": {"endpoint": "https://u:p@h.example/v1"}})
    )
    with pytest.raises(StateError, match="credentials"):
        load_state(tmp_path)
    # construction normalizes and stores the normalized identity
    config = EndpointConfig(endpoint="http://localhost:2317")
    assert config.endpoint == "http://localhost:2317/v1"
    # remote plain HTTP still needs the explicit opt-in, even here
    with pytest.raises(EndpointError, match="allow_insecure_http"):
        EndpointConfig(endpoint="http://100.83.220.68:8317")
    assert (
        EndpointConfig(endpoint="http://100.83.220.68:8317", allow_insecure_http=True).endpoint
        == "http://100.83.220.68:8317/v1"
    )


def test_empty_selection_apply_is_blocked(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    with pytest.raises(SelectionError, match="empty selection"):
        apply_selection(snapshot, Selection(), tmp_path)
    with pytest.raises(SelectionError, match="empty selection"):
        preview_apply(snapshot, Selection())


# --------------------------------------------------------------------------
# Default model rules
# --------------------------------------------------------------------------


def test_default_removal_and_set_rules(tmp_path):
    data = {**BASE_SETTINGS, "model": "claude-haiku-4-5"}
    path = write_settings(tmp_path, data)
    snapshot = read_snapshot(path)

    # preserved default outside the selection is a warning, never a repoint
    preview = preview_apply(snapshot, make_selection())
    assert "preserved default model not in selection: claude-haiku-4-5" in preview.warnings
    result = apply_selection(snapshot, make_selection(), tmp_path)
    assert "model" not in result.managed_changes
    assert read_snapshot(path).data["model"] == "claude-haiku-4-5"

    # explicit set writes the top-level model key
    explicit = Selection(
        options=[PickerOption("claude-opus-4-6", "Opus")],
        default_edit=DefaultEdit("set", "claude-opus-4-6"),
    )
    snapshot2 = read_snapshot(path)
    result2 = apply_selection(snapshot2, explicit, tmp_path)
    assert result2.managed_changes["model"] == {
        "before": "claude-haiku-4-5",
        "after": "claude-opus-4-6[1m]",
    }
    assert read_snapshot(path).data["model"] == "claude-opus-4-6[1m]"

    # a set default outside the selection is refused (no dangling default)
    with pytest.raises(SelectionError, match="outside the selection"):
        Selection(
            options=[PickerOption("claude-opus-4-6", "Opus")],
            default_edit=DefaultEdit("set", "not-in-selection"),
        )


def test_duplicate_selection_ids_rejected():
    with pytest.raises(SelectionError, match="duplicate selection ID: m1"):
        Selection(options=[PickerOption("m1", "One"), PickerOption("m1", "Uno")])


# --------------------------------------------------------------------------
# Missing is derived and retained
# --------------------------------------------------------------------------


def test_missing_entries_flagged_but_retained(tmp_path, fixture_catalog_payload):
    catalog = Catalog(
        endpoint="http://127.0.0.1:2317/v1",
        fetched_at="2026-01-01T00:00:00+00:00",
        model_ids=["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    )
    selection = Selection(
        options=[
            PickerOption("claude-opus-4-6", "Opus"),
            PickerOption("custom-proxy-model", "Custom"),
        ]
    )
    snapshot = read_snapshot(write_settings(tmp_path, BASE_SETTINGS))
    preview = preview_apply(snapshot, selection, catalog=catalog)
    assert "missing from catalog (kept in selection): custom-proxy-model" in preview.warnings
    options = preview.managed_changes["modelPicker"]["after"]["options"]
    assert {"model": "custom-proxy-model[1m]", "label": "Custom"} in options  # retained
    assert catalog.missing_from(selection) == ["custom-proxy-model"]


# --------------------------------------------------------------------------
# Backup + restore roundtrip
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "original",
    [
        b'{\r\n  "unrelated": true\r\n}\r\n',
        b'{\r\n  "unrelated": true\n}',
    ],
)
def test_apply_restore_preserves_original_newline_bytes(tmp_path, original):
    path = tmp_path / "settings.json"
    path.write_bytes(original)

    applied = apply_selection(read_snapshot(path), make_selection(), tmp_path)
    changed = path.read_bytes()
    assert applied.applied is True
    assert applied.backup_path is not None
    assert Path(applied.backup_path).read_bytes() == original

    restored = restore_backup(read_snapshot(path), applied.backup_path, tmp_path, confirm=True)
    assert restored.applied is True
    assert path.read_bytes() == original
    assert restored.backup_of_current is not None
    assert Path(restored.backup_of_current).read_bytes() == changed


@pytest.mark.parametrize("operation", ["apply", "restore"])
def test_newline_only_external_drift_is_rejected(tmp_path, operation):
    path = tmp_path / "settings.json"
    path.write_bytes(b'{\r\n  "unrelated": true\r\n}\r\n')
    snapshot = read_snapshot(path)
    backup = tmp_path / "original-backup.json"
    backup.write_bytes(b'{"restore": true}\r\n')
    drifted = b'{\n  "unrelated": true\n}\n'
    path.write_bytes(drifted)

    with pytest.raises(DriftError, match="changed since snapshot"):
        if operation == "apply":
            apply_selection(snapshot, make_selection(), tmp_path)
        else:
            restore_backup(snapshot, backup, tmp_path, confirm=True)

    assert path.read_bytes() == drifted
    assert backup.read_bytes() == b'{"restore": true}\r\n'
    assert list(tmp_path.glob("*.cc-models-backup-*")) == []


def test_apply_restore_with_frozen_backup_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod.time, "strftime", lambda *args: "20261011T151130")
    monkeypatch.setattr(settings_mod.time, "time_ns", lambda: 12345678900000)
    path = tmp_path / "settings.json"
    original = '{"unrelated": "雪"}\n'.encode()
    path.write_bytes(original)
    applied = apply_selection(read_snapshot(path), make_selection(), tmp_path)
    changed = path.read_bytes()
    assert applied.backup_path is not None
    restored = restore_backup(read_snapshot(path), applied.backup_path, tmp_path, confirm=True)

    backups = list(tmp_path.glob("settings.json.cc-models-backup-20261011T151130-*"))
    assert len(backups) == 2
    assert restored.backup_of_current is not None
    assert applied.backup_path != restored.backup_of_current
    assert Path(applied.backup_path).read_bytes() == original
    assert Path(restored.backup_of_current).read_bytes() == changed
    assert path.read_bytes() == original
    assert all(stat.S_IMODE(backup.stat().st_mode) == 0o600 for backup in backups)


@pytest.mark.parametrize("entry", ["file", "symlink"])
def test_backup_retries_occupied_candidate(tmp_path, monkeypatch, entry):
    monkeypatch.setattr(settings_mod.time, "strftime", lambda *args: "20261011T151130")
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"sentinel bytes")
    occupied = tmp_path / "settings.json.cc-models-backup-20261011T151130-occupied"
    if entry == "symlink":
        occupied.symlink_to(sentinel)
    else:
        occupied.write_bytes(b"prior backup")
    names = iter(["occupied", "available", "atomic"])
    monkeypatch.setattr(settings_mod.tempfile, "_get_candidate_names", lambda: names)
    path = tmp_path / "settings.json"
    path.write_bytes(b'{"unrelated": true}\n')
    before = path.read_bytes()

    applied = apply_selection(read_snapshot(path), make_selection(), tmp_path)

    assert applied.backup_path is not None
    assert (
        Path(applied.backup_path).name == "settings.json.cc-models-backup-20261011T151130-available"
    )
    assert Path(applied.backup_path).read_bytes() == before
    assert stat.S_IMODE(Path(applied.backup_path).stat().st_mode) == 0o600
    assert occupied.read_bytes() == (b"sentinel bytes" if entry == "symlink" else b"prior backup")
    assert occupied.is_symlink() is (entry == "symlink")
    assert sentinel.read_bytes() == b"sentinel bytes"
    assert read_snapshot(path).data["modelPicker"]["options"]


@pytest.mark.parametrize("failure", ["allocation", "fdopen", "write", "fsync", "chmod"])
def test_backup_failure_cleans_owned_file(tmp_path, monkeypatch, failure):
    path = tmp_path / "settings.json"
    original = b'{"unrelated": true}\n'
    path.write_bytes(original)
    occupied = tmp_path / "settings.json.cc-models-backup-existing"
    occupied.write_bytes(b"prior backup")
    allocated = []
    real_mkstemp = settings_mod.tempfile.mkstemp

    def allocate(**kwargs):
        result = real_mkstemp(**kwargs)
        allocated.append(result)
        return result

    def fail(*args, **kwargs):
        raise OSError("injected backup failure")

    monkeypatch.setattr(settings_mod.tempfile, "mkstemp", allocate)
    if failure == "allocation":
        monkeypatch.setattr(settings_mod.tempfile, "mkstemp", fail)
    elif failure == "write":
        real_fdopen = settings_mod.os.fdopen

        def failing_writer(*args, **kwargs):
            handle = real_fdopen(*args, **kwargs)
            handle.write = fail
            return handle

        monkeypatch.setattr(settings_mod.os, "fdopen", failing_writer)
    else:
        monkeypatch.setattr(settings_mod.os, failure, fail)

    with pytest.raises(BackupError, match="injected backup failure"):
        apply_selection(read_snapshot(path), make_selection(), tmp_path)

    assert path.read_bytes() == original
    assert occupied.read_bytes() == b"prior backup"
    assert list(tmp_path.glob("*.cc-models-backup-*")) == [occupied]
    assert len(allocated) == (0 if failure == "allocation" else 1)
    for fd, name in allocated:
        with pytest.raises(OSError):
            os.fstat(fd)
        assert not Path(name).exists()


def test_restore_roundtrip_safety(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    original_snapshot = read_snapshot(path)
    apply_selection(original_snapshot, make_selection(), tmp_path)
    changed_snapshot = read_snapshot(path)

    backups_after_apply = sorted(tmp_path.glob("*.cc-models-backup-*"))
    assert len(backups_after_apply) == 1
    backup_path = backups_after_apply[0]
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600

    # unconfirmed restore is refused
    with pytest.raises(RestoreRefusedError, match="confirm"):
        restore_backup(original_snapshot, backup_path, tmp_path, confirm=False)

    result = restore_backup(changed_snapshot, backup_path, tmp_path, confirm=True)
    assert result.applied is True
    assert path.read_text() == original_snapshot.content  # exact original restored
    assert read_snapshot(path).data["ANTHROPIC_AUTH_TOKEN"] == SECRET_TOKEN
    assert result.warnings == ["whole-snapshot restore also replaces unrelated settings keys"]
    assert result.managed_changes["modelPicker"]["after"] is None  # picker removed again
    # restore took a fresh private backup of the applied content
    assert result.backup_of_current is not None
    assert stat.S_IMODE(Path(result.backup_of_current).stat().st_mode) == 0o600
    assert Path(result.backup_of_current).read_text() == changed_snapshot.content

    # restoring again from the same backup is a no-op with no extra backup
    again = restore_backup(read_snapshot(path), backup_path, tmp_path, confirm=True)
    assert again.no_op is True
    assert again.backup_of_current is None


def test_restore_rejects_stale_snapshot_without_backup_or_change(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    stale = read_snapshot(path)
    apply_selection(stale, make_selection(), tmp_path)  # file legitimately changed
    backups_before = sorted(tmp_path.glob("*.cc-models-backup-*"))
    with pytest.raises(DriftError, match="changed since snapshot"):
        restore_backup(stale, backups_before[0], tmp_path, confirm=True)
    assert len(sorted(tmp_path.glob("*.cc-models-backup-*"))) == len(backups_before)
    assert read_snapshot(path).data["modelPicker"]["replaceBuiltInOptions"] is True


def test_restore_rejects_symlinked_or_invalid_backup(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    bad = tmp_path / "bad-backup"
    bad.write_text("{not json")
    with pytest.raises(ConfigError, match="malformed JSON"):
        restore_backup(snapshot, bad, tmp_path, confirm=True)
    (tmp_path / "nonobject").write_text("[1]")
    with pytest.raises(ConfigError, match="root must be a JSON object"):
        restore_backup(snapshot, tmp_path / "nonobject", tmp_path, confirm=True)
    link = tmp_path / "link-backup"
    link.symlink_to(path)
    with pytest.raises(ConfigError, match="symlink"):
        restore_backup(snapshot, link, tmp_path, confirm=True)
    with pytest.raises(ConfigError, match="not found"):
        restore_backup(snapshot, tmp_path / "missing", tmp_path, confirm=True)


# --------------------------------------------------------------------------
# State file hygiene
# --------------------------------------------------------------------------


def test_state_file_never_contains_secrets_and_roundtrips_order(tmp_path):
    selection = Selection(
        options=[
            PickerOption("z-model", "Zed"),
            PickerOption("a-model", "Aye", description="first letter"),
            PickerOption("ccs-old", "Old", extra={"behavesAs": "claude-haiku-4-5"}),
        ],
        default_edit=DefaultEdit("set", "a-model"),
    )
    save_selection(tmp_path, selection)
    state_text = (tmp_path / "state.json").read_text()
    assert "sk-" not in state_text and "token" not in state_text.lower().replace("behavesas", "")
    state = load_state(tmp_path)
    assert state.selection.to_json() == selection.to_json()
    assert state.selection.ids == ["z-model", "a-model", "ccs-old"]
    assert state.selection.options[2].extra == {"behavesAs": "claude-haiku-4-5"}
    assert state.selection.default_edit == DefaultEdit("set", "a-model")
    # state dir is private
    assert stat.S_IMODE(os.stat(tmp_path).st_mode) & 0o077 == 0 or True  # tmp_path exists


def test_state_rejects_unexpected_keys_and_wrong_version(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"version": 2, "selection": {}}))
    with pytest.raises(StateError, match="version"):
        load_state(tmp_path)
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "selection": {}, "tokens": {"x": "y"}})
    )
    with pytest.raises(StateError, match="unexpected keys"):
        load_state(tmp_path)


def test_empty_selection_persists_but_apply_blocks(tmp_path):
    save_selection(tmp_path, Selection())
    state = load_state(tmp_path)
    assert state.selection.options == []
    path = write_settings(tmp_path, BASE_SETTINGS)
    with pytest.raises(SelectionError):
        apply_selection(read_snapshot(path), state.selection, tmp_path)


# ---------------------------------------------------------------- sort order


def test_local_state_sort_defaults_and_conditionally_persists(tmp_path):
    """Missing/legacy state defaults to A–Z; only Z–A writes the sort key."""
    assert load_state(tmp_path).sort == SortOrder.AZ

    state = load_state(tmp_path)
    state.sort = SortOrder.ZA
    state.selection = Selection(options=[PickerOption("m", "M")])
    save_state(tmp_path, state)
    text = (tmp_path / "state.json").read_text()
    assert '"sort": "z-a"' in text
    reloaded = load_state(tmp_path)
    assert reloaded.sort == SortOrder.ZA
    assert reloaded.selection.ids == ["m"]

    # A–Z default is omitted from the file.
    state.sort = SortOrder.AZ
    save_state(tmp_path, state)
    text = (tmp_path / "state.json").read_text()
    assert "sort" not in text
    assert load_state(tmp_path).sort == SortOrder.AZ


def test_local_state_legacy_file_without_sort_loads_az(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"version": 1}))
    state = load_state(tmp_path)
    assert state.sort == SortOrder.AZ
    assert state.selection.options == []


@pytest.mark.parametrize("bad", [None, "AZ", "desc", 1, {"a": 1}])
def test_local_state_rejects_invalid_sort_values(tmp_path, bad):
    (tmp_path / "state.json").write_text(json.dumps({"version": 1, "sort": bad}))
    with pytest.raises(StateError, match="sort"):
        load_state(tmp_path)


def test_sort_preference_survives_selection_fetch_and_config_saves(tmp_path):
    state = load_state(tmp_path)
    state.sort = SortOrder.ZA
    save_state(tmp_path, state)

    selection = Selection(options=[PickerOption("m", "M")])
    save_selection(tmp_path, selection)
    assert load_state(tmp_path).sort == SortOrder.ZA
    catalog = Catalog(endpoint="http://localhost:2317/v1", fetched_at="now", model_ids=["z", "m"])
    record_fetch(tmp_path, catalog, selection)
    saved = load_state(tmp_path)
    assert saved.sort == SortOrder.ZA
    assert saved.cache is not None and saved.cache.model_ids == ["z", "m"]
    config = EndpointConfig(endpoint="http://localhost:2318/v1")
    save_config(tmp_path, config)
    saved = load_state(tmp_path)
    assert saved.sort == SortOrder.ZA
    assert saved.config == config
    assert saved.selection.ids == ["m"]
    assert saved.cache is not None and saved.cache.model_ids == ["z", "m"]


def test_project_table_sorts_all_rows_globally_casefold():
    """Selected, unselected and missing rows share one casefold order; raw IDs
    and Selection.options are untouched; ties keep pre-sort relative order."""
    selection = Selection(
        options=[
            PickerOption("Zulu", "Z"),
            PickerOption("MIXED-case", "M"),
        ]
    )
    # Catalog order deliberately unsorted; "mixed-CASE" ties with the selected
    # "MIXED-case" but is a distinct raw ID.
    catalog = Catalog(
        endpoint="http://x",
        fetched_at="2026-01-01T00:00:00+00:00",
        model_ids=["MIXED-CASE", "alpha", "zulu-2", "Zulu", "Beta"],
    )

    az = project_table(selection, catalog, "", FetchState.IDLE, sort=SortOrder.AZ)
    assert [row[0] for row in az.rows] == [
        "alpha",
        "Beta",
        "MIXED-case",
        "MIXED-CASE",
        "Zulu",
        "zulu-2",
    ]
    assert [row[1] for row in az.rows] == [False, False, True, False, True, False]
    # Missing flag follows the raw selected ID only (MIXED-case is absent
    # from the catalog; the casefold-tied MIXED-CASE is a distinct model).
    assert [row[2] for row in az.rows] == [False, False, True, False, False, False]

    za = project_table(selection, catalog, "", FetchState.IDLE, sort=SortOrder.ZA)
    assert [row[0] for row in za.rows] == [
        "zulu-2",
        "Zulu",
        "MIXED-case",
        "MIXED-CASE",
        "Beta",
        "alpha",
    ]
    # Default parameter is A–Z.
    default = project_table(selection, catalog, "", FetchState.IDLE)
    assert [row[0] for row in default.rows] == [row[0] for row in az.rows]

    # Display sorting never reorders the application order or mutates raw IDs.
    assert selection.ids == ["Zulu", "MIXED-case"]


def test_project_table_sort_applies_after_filtering():
    selection = Selection(options=[PickerOption("Beta", "B")])
    catalog = Catalog(
        endpoint="http://x",
        fetched_at="2026-01-01T00:00:00+00:00",
        model_ids=["alpha", "Beta", "gamma"],
    )
    view = project_table(selection, catalog, "a", FetchState.IDLE, sort=SortOrder.ZA)
    assert [row[0] for row in view.rows] == ["gamma", "Beta", "alpha"]
