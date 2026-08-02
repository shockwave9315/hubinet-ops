from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Iterator

import pytest
import yaml

from tests.test_executor_contract import _executor_namespace


ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "deploy" / "managed" / "profiles" / "ct110.json"
RENDERER = ROOT / "scripts" / "render_ct110_profile.py"
UPGRADE = ROOT / "deploy" / "upgrade-0.4.3-from-pve.sh"


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(404)
            return
        payload = b'{"status":"ok","version":"0.4.3"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: Any) -> None:
        return


@contextmanager
def _backend_on_production_port() -> Iterator[None]:
    server = ThreadingHTTPServer(("127.0.0.1", 8787), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _executor_health_globals(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    namespace = _executor_namespace(monkeypatch)
    globals_ = namespace["preflight"].__globals__
    globals_["system_metrics"] = lambda: {
        "hostname": "ct110-test",
        "os": "Debian test",
        "uptime_seconds": 60,
        "ip_addresses": ["127.0.0.1"],
        "disk": {
            "total_mb": 8192,
            "used_mb": 1024,
            "free_mb": 7168,
            "used_percent": 12.5,
        },
        "memory": {
            "total_mb": 4096,
            "used_mb": 512,
            "available_mb": 3584,
            "used_percent": 12.5,
        },
    }
    globals_["service_states"] = lambda _config: (
        {"hubinet-ops.service": "active"},
        [],
    )
    globals_["failed_units"] = lambda: []
    globals_["docker_state"] = lambda _config: ({"enabled": False}, [])
    globals_["dpkg_locked"] = lambda: False
    globals_["apt_scan"] = lambda: {
        "pending_count": 0,
        "packages": [],
        "fingerprint": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "scanned_at": 0,
        "security_updates_count": 0,
        "reboot_required": False,
    }
    globals_["run"] = lambda argv, **_kwargs: subprocess.CompletedProcess(
        argv,
        0,
        "",
        "",
    )
    return namespace


def _profile(port: int) -> dict[str, Any]:
    return {
        "services": ["hubinet-ops.service"],
        "health_urls": [f"http://127.0.0.1:{port}/health"],
        "min_free_mb": 2048,
        "ignore_failed_units": [],
        "repair_actions": [],
        "docker": {
            "enabled": False,
            "require_health": False,
            "required_containers": [],
        },
    }


def test_ct110_preflight_and_verify_reject_8740_and_accept_api_port_8787(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _executor_health_globals(monkeypatch)

    with _backend_on_production_port():
        with pytest.raises(RuntimeError, match=r"127\.0\.0\.1:8740"):
            namespace["preflight"](_profile(8740))
        _bad_data, bad_failures = namespace["verify"](_profile(8740))
        assert any("127.0.0.1:8740" in failure for failure in bad_failures)

        preflight = namespace["preflight"](_profile(8787))
        verified, failures = namespace["verify"](_profile(8787))

    assert preflight["urls"]["http://127.0.0.1:8787/health"]["ok"] is True
    assert failures == []
    assert verified["urls"]["http://127.0.0.1:8787/health"]["ok"] is True
    assert verified["services"]["hubinet-ops.service"] == "active"


@pytest.mark.parametrize("api_port", [8787, 8899])
def test_ct110_profile_renderer_uses_preserved_api_port(
    tmp_path: Path,
    api_port: int,
) -> None:
    config = yaml.safe_load(
        (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    )
    config["api"]["port"] = api_port
    config_path = tmp_path / "config.yaml"
    output_path = tmp_path / "hubinet-maint.json"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--config",
            str(config_path),
            "--profile-template",
            str(PROFILE),
            "--output",
            str(output_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    rendered = json.loads(output_path.read_text(encoding="utf-8"))
    assert rendered["health_urls"] == [
        f"http://127.0.0.1:{api_port}/health"
    ]
    assert "{api_port}" not in output_path.read_text(encoding="utf-8")


def test_upgrade_renders_ct110_profile_then_validates_real_managed_health() -> None:
    text = UPGRADE.read_text(encoding="utf-8")
    migration = text.index("scripts/migrate_config_0_4_3.py")
    renderer = text.index("scripts/render_ct110_profile.py", migration)
    profile_install = text.index('install_managed_ct "$vmid" "$status"', renderer)
    service_active = text.index(
        'systemctl is-active --quiet hubinet-ops.service',
        profile_install,
    )
    profile_port_check = text.index("CT110 profile health URL does not match api.port")
    managed_health = text.index(
        "/usr/local/sbin/hubinet-maint healthcheck",
        service_active,
    )

    assert migration < renderer < profile_install < service_active
    assert profile_port_check < managed_health
    assert "AGENT_API_PORT" in text


def test_obsolete_ct110_port_occurs_only_in_the_regression_fixture() -> None:
    production_roots = (
        ROOT / "app",
        ROOT / "config",
        ROOT / "deploy",
        ROOT / "docs",
        ROOT / "home-assistant",
        ROOT / "scripts",
    )
    offenders = [
        path.relative_to(ROOT).as_posix()
        for root in production_roots
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() not in {".pyc", ".pyo"}
        and "__pycache__" not in path.parts
        and b"8740" in path.read_bytes()
    ]

    assert offenders == []
