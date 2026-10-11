"""CLI tests: real subprocess runs (`python -m cc_models`), exact outputs.

No real user settings, secrets or network. Network tests use a loopback
http.server. Every command output is asserted free of token values.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cc_models.session import DEMO_TOKEN

FIXTURES = Path(__file__).parent / "fixtures"
SECRET_TOKEN = "sk-ant-secret-do-not-leak"

SETTINGS = {
    "ANTHROPIC_AUTH_TOKEN": SECRET_TOKEN,
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:2317",
    "numParallelServers": 2,
    "theme": "dark",
    "model": "claude-sonnet-4-6",
    "modelPicker": {
        "replaceBuiltInOptions": True,
        "options": [
            {"model": "claude-opus-4-6", "label": "Opus"},
            {"model": "custom-proxy-model", "label": "自定义"},
        ],
    },
}


def write_settings(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def run_cli(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # Isolate the child from any ambient CPA_/ANTHROPIC_ configuration so a
    # developer shell cannot change endpoint/token resolution under the test.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CPA_", "ANTHROPIC_"))
    }
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "cc_models", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = cast(Any, self.server).routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, body = route(self)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def catalog_server():
    server = cast(Any, ThreadingHTTPServer(("127.0.0.1", 0), _Handler))
    server.routes = {}
    server.requests_seen = []
    original = _Handler.do_GET

    def do_GET(self):
        server.requests_seen.append((self.path, self.headers.get("Authorization")))
        return original(self)

    _Handler.do_GET = do_GET
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
    return (status, {"Content-Type": "application/json"}, json.dumps(payload).encode())


def catalog_payload():
    return json.loads((FIXTURES / "catalog.json").read_text())


# --------------------------------------------------------------------- list


def test_list_shows_selection_and_no_secrets(tmp_path):
    settings = write_settings(tmp_path, SETTINGS)
    result = run_cli("--settings", str(settings), "--state-dir", str(tmp_path / "state"), "list")
    assert result.returncode == 0
    assert "claude-opus-4-6 — Opus" in result.stdout
    assert "custom-proxy-model — 自定义" in result.stdout
    assert "端点: http://127.0.0.1:2317/v1（来源: 设置文件 ANTHROPIC_BASE_URL）" in result.stdout
    assert "启动时从设置文件 modelPicker 导入" in result.stdout
    assert SECRET_TOKEN not in result.stdout
    assert "sk-" not in result.stdout


def test_missing_settings_friendly_refusal(tmp_path):
    result = run_cli(
        "--settings", str(tmp_path / "absent.json"), "--state-dir", str(tmp_path / "state"), "list"
    )
    assert result.returncode == 1
    assert "未找到设置文件" in result.stdout
    assert "--demo" in result.stdout
    assert "Traceback" not in result.stderr


def test_state_selection_authoritative_even_when_empty(tmp_path):
    settings = write_settings(tmp_path, SETTINGS)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "selection": {
                    "default_edit": {"kind": "preserve", "value": None},
                    "options": [],
                },
            }
        )
    )
    result = run_cli("--settings", str(settings), "--state-dir", str(state_dir), "list")
    assert result.returncode == 0
    assert "已选: （空）" in result.stdout
    assert "来自本地状态" in result.stdout  # empty saved choice is NOT reimported


# ------------------------------------------------------------------ refresh


def test_refresh_against_loopback_and_list_uses_cache(tmp_path, catalog_server):
    settings = write_settings(tmp_path, SETTINGS)
    catalog_server.routes["/v1/models"] = lambda _h: json_route(catalog_payload())
    state_dir = tmp_path / "state"
    result = run_cli(
        "--settings",
        str(settings),
        "--state-dir",
        str(state_dir),
        "--base-url",
        catalog_server.base_url,
        "refresh",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "claude-opus-4-6 ✓" in result.stdout
    assert "claude-haiku-4-5" in result.stdout

    # offline list with the SAME per-run endpoint uses the endpoint-bound cache
    endpoint = f"{catalog_server.base_url}/v1"
    result2 = run_cli(
        "--settings",
        str(settings),
        "--state-dir",
        str(state_dir),
        "--base-url",
        catalog_server.base_url,
        "list",
    )
    assert result2.returncode == 0
    assert f"目录: 缓存 {endpoint}" in result2.stdout
    assert "缺失" in result2.stdout  # custom-proxy-model missing from catalog

    # --base-url is per-run only: the settings endpoint must not see that cache
    result3 = run_cli("--settings", str(settings), "--state-dir", str(state_dir), "list")
    assert result3.returncode == 0
    assert "目录: 无缓存" in result3.stdout
    assert "端点: http://127.0.0.1:2317/v1（来源: 设置文件 ANTHROPIC_BASE_URL）" in result3.stdout

    # settings-derived token is NOT sent to a different endpoint
    auths = [auth for _path, auth in catalog_server.seen if auth is not None]
    assert auths == [], "settings token must not be sent to another endpoint"


def test_refresh_failure_keeps_selection_and_exits_nonzero(tmp_path):
    settings = write_settings(tmp_path, SETTINGS)
    state_dir = tmp_path / "state"
    run_cli(
        "--settings",
        str(settings),
        "--state-dir",
        str(state_dir),
        "--base-url",
        "http://127.0.0.1:9",
        "refresh",
    )
    result = run_cli("--settings", str(settings), "--state-dir", str(state_dir), "list")
    assert result.returncode == 0
    assert "已选（顺序）" in result.stdout  # selection survived


# -------------------------------------------------------------------- apply


def test_apply_refuses_without_yes_or_dry_run(tmp_path):
    settings = write_settings(tmp_path, SETTINGS)
    result = run_cli("--settings", str(settings), "--state-dir", str(tmp_path / "state"), "apply")
    assert result.returncode == 2
    assert "拒绝" in result.stdout
    assert json.loads(settings.read_text())["modelPicker"]["options"][0]["label"] == "Opus"


def test_apply_dry_run_exact_managed_output_no_secrets(tmp_path):
    settings = write_settings(tmp_path, SETTINGS)
    state_dir = tmp_path / "state"
    # Preserve the existing default (settings "model" = claude-sonnet-4-6) while
    # selecting only claude-opus-4-6: the preserved default is now out of the
    # selection and must surface as a preview warning (never a silent repoint).
    (state_dir.mkdir())
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "selection": {
                    "default_edit": {"kind": "preserve", "value": None},
                    "options": [{"model": "claude-opus-4-6", "label": "Opus 真实"}],
                },
            }
        )
    )
    result = run_cli(
        "--settings", str(settings), "--state-dir", str(state_dir), "apply", "--dry-run"
    )
    assert result.returncode == 0
    assert "claude-opus-4-6" in result.stdout
    assert '"after"' in result.stdout and '"before"' in result.stdout
    assert SECRET_TOKEN not in result.stdout  # unrelated settings never in preview
    assert "警告: preserved default model not in selection: claude-sonnet-4-6" in result.stdout
    # nothing written
    assert json.loads(settings.read_text())["modelPicker"]["options"][0]["label"] == "Opus"
    assert json.loads(settings.read_text())["model"] == "claude-sonnet-4-6"


def test_apply_yes_writes_backup_and_preserves_unrelated(tmp_path):
    settings = write_settings(tmp_path, SETTINGS)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "selection": {
                    "default_edit": {"kind": "preserve", "value": None},
                    "options": [{"model": "claude-opus-4-6", "label": "Opus 真实"}],
                },
            }
        )
    )
    before = settings.read_text()
    result = run_cli("--settings", str(settings), "--state-dir", str(state_dir), "apply", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "已应用" in result.stdout
    assert "备份: " in result.stdout
    after = json.loads(settings.read_text())
    assert after["ANTHROPIC_AUTH_TOKEN"] == SECRET_TOKEN  # preserved, untouched
    assert after["numParallelServers"] == 2
    assert after["modelPicker"] == {
        "replaceBuiltInOptions": True,
        "options": [{"model": "claude-opus-4-6", "label": "Opus 真实"}],
    }
    backups = list(tmp_path.glob("settings.json.cc-models-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == before
    # state.json contains no secrets
    assert SECRET_TOKEN not in (state_dir / "state.json").read_text()


# ------------------------------------------------------- backups and restore


def test_backups_and_restore_roundtrip(tmp_path):
    settings = write_settings(tmp_path, SETTINGS)
    state_dir = tmp_path / "state"
    original = settings.read_text()
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "selection": {
                    "default_edit": {"kind": "set", "value": "claude-opus-4-6"},
                    "options": [{"model": "claude-opus-4-6", "label": "Opus"}],
                },
            }
        )
    )
    apply_result = run_cli(
        "--settings", str(settings), "--state-dir", str(state_dir), "apply", "--yes"
    )
    assert apply_result.returncode == 0
    backup = next(iter(tmp_path.glob("settings.json.cc-models-backup-*")))

    listing = run_cli("--settings", str(settings), "--state-dir", str(state_dir), "backups")
    assert listing.returncode == 0
    assert backup.name in listing.stdout

    # restore without --yes is refused
    refused = run_cli(
        "--settings",
        str(settings),
        "--state-dir",
        str(state_dir),
        "restore",
        "--backup",
        str(backup),
    )
    assert refused.returncode == 2
    assert "--yes" in refused.stdout

    restored = run_cli(
        "--settings",
        str(settings),
        "--state-dir",
        str(state_dir),
        "restore",
        "--backup",
        str(backup),
        "--yes",
    )
    assert restored.returncode == 0, restored.stdout
    assert "已恢复" in restored.stdout
    assert settings.read_text() == original  # exact original restored
    # restore took a fresh backup of the applied content
    assert len(list(tmp_path.glob("settings.json.cc-models-backup-*"))) == 2


def test_endpoint_with_credentials_is_refused(tmp_path):
    settings = write_settings(tmp_path, SETTINGS)
    result = run_cli(
        "--settings",
        str(settings),
        "--state-dir",
        str(tmp_path / "state"),
        "--base-url",
        "https://user:secret@example.com",
        "list",
    )
    assert result.returncode == 1
    assert "credentials" in result.stdout
    assert "secret@" not in result.stdout


# --------------------------------------------------------------------- demo


def test_demo_fully_offline_even_refresh():
    # --demo must never do live HTTP: point refresh at a dead port anyway
    result = run_cli("--demo", "refresh")
    assert result.returncode == 0
    assert "claude-opus-4-6 ✓" in result.stdout
    assert "custom-proxy-model" in result.stdout
    listing = run_cli("--demo", "list")
    assert listing.returncode == 0
    assert "不访问网络" in listing.stdout
    assert DEMO_TOKEN not in listing.stdout  # fixture token never printed
    assert "sk-" not in listing.stdout


def test_console_script_installed():
    result = subprocess.run(
        [sys.executable, "-m", "cc_models", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "list" in result.stdout and "apply" in result.stdout


@pytest.mark.parametrize(
    "url",
    [
        "https://user:url-secret@example.com",
        "https://example.com?token=url-secret",
        "https://example.com#url-secret",
        "bad://user:url-secret@example.com?token=url-secret",
    ],
)
@pytest.mark.parametrize("source", ["env", "settings", "override"])
def test_secret_endpoint_never_in_cli_output(tmp_path, url, source):
    settings = write_settings(tmp_path, {"ANTHROPIC_BASE_URL": url} if source == "settings" else {})
    args = ["--settings", str(settings), "--state-dir", str(tmp_path / "state")]
    if source == "override":
        args += ["--base-url", url]
    result = run_cli(*args, "list", env_extra={"CPA_BASE_URL": url} if source == "env" else {})
    assert "url-secret" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert result.returncode == (1 if source == "override" else 0)


@pytest.mark.parametrize(
    "content", ["[]", "null", '"text"', '{"model":1,"model":2}', '{"model":NaN}']
)
@pytest.mark.parametrize("confirmed", [False, True])
def test_restore_strict_object_preview_friendly_refusal(tmp_path, content, confirmed):
    settings = write_settings(tmp_path, SETTINGS)
    before = settings.read_bytes()
    backup = tmp_path / "backup.json"
    backup.write_text(content)
    args = [
        "--settings",
        str(settings),
        "--state-dir",
        str(tmp_path / "state"),
        "restore",
        "--backup",
        str(backup),
    ]
    result = run_cli(*args, *(["--yes"] if confirmed else []))
    assert result.returncode == 1
    assert "错误" in result.stdout
    assert "Traceback" not in result.stderr
    assert settings.read_bytes() == before
