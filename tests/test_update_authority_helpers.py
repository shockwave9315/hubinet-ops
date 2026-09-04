"""Local-safe pytest unit tests for the in-place updater's Python helpers.

Unlike deploy/update-proxmox-0.5.sh and deploy/lib/update-*.sh (bash
orchestration, sandbox-only -- see tests/test_update_proxmox_0_5_smoke.py),
these are small, bounded Python scripts with no privileged/deployment
command execution of their own -- exactly like
deploy/hubinet-package-scan-helper.py's own test file
(tests/test_package_scan_execution.py), they are loaded directly via
importlib and exercised against real temporary SQLite databases / mocked
HTTP, never touching a real PVE/network endpoint.
"""

from __future__ import annotations

import errno
import http.server
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time as _time
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from app.inventory_runtime_config import parse_r0_runtime_config

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_TOOL_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-authority-tool.py"
UPDATE_PROBE_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-update-probe.py"
UPDATE_FENCE_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-update-fence.py"
ACCEPT_SCRIPT_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-bootstrap-accept.py"
VENV_BUILD_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-update-venv-stage.py"
CONTENTION_POLICY_PATH = ROOT / "app" / "inventory" / "contention_policy.py"
HOST_CONTROL_FIELDS_PATH = (
    ROOT / "deploy" / "lib" / "hubinet-ops-update-host-control-fields.py"
)

MARKER = "hubinet_ops_0_5_authority"
BACKEND_ID = "11111111-1111-4111-8111-111111111111"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


authority_tool = _load(AUTHORITY_TOOL_PATH, "hubinet_ops_authority_tool")
update_probe = _load(UPDATE_PROBE_PATH, "hubinet_ops_update_probe")
update_fence = _load(UPDATE_FENCE_PATH, "hubinet_ops_update_fence")
accept_script = _load(ACCEPT_SCRIPT_PATH, "hubinet_ops_bootstrap_accept")
# The backend's pure-constants writer-wait policy, loaded directly by path
# rather than `import app.inventory.contention_policy`: that would execute
# `app/inventory/__init__.py`, pulling in the rest of the authority package
# for what is otherwise a dependency-free cross-check.
contention_policy = _load(
    CONTENTION_POLICY_PATH, "hubinet_ops_contention_policy_fence_mirror_check"
)
host_control_fields = _load(
    HOST_CONTROL_FIELDS_PATH, "hubinet_ops_update_host_control_fields"
)


def _make_authority_db(path: Path, *, marker: str = MARKER, version: int = 8, backend_id: str = BACKEND_ID) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE authority_schema (marker TEXT, schema_version INTEGER)"
        )
        connection.execute(
            "INSERT INTO authority_schema(marker, schema_version) VALUES(?, ?)",
            (marker, version),
        )
        connection.execute(
            "CREATE TABLE backend_instance (backend_instance_id TEXT)"
        )
        connection.execute(
            "INSERT INTO backend_instance(backend_instance_id) VALUES(?)", (backend_id,)
        )
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
    finally:
        connection.close()


class TestInspect:
    def test_missing_file(self, tmp_path):
        ok, facts = authority_tool._read_facts(str(tmp_path / "missing.db"))
        assert ok is False
        assert facts == {"exists": False, "reason": "missing_or_empty"}

    def test_empty_file(self, tmp_path):
        db = tmp_path / "empty.db"
        db.write_bytes(b"")
        ok, facts = authority_tool._read_facts(str(db))
        assert ok is False
        assert facts["reason"] == "missing_or_empty"

    def test_valid_database(self, tmp_path):
        db = tmp_path / "authority.db"
        _make_authority_db(db)
        ok, facts = authority_tool._read_facts(str(db))
        assert ok is True
        assert facts["marker"] == MARKER
        assert facts["schema_version"] == 8
        assert facts["backend_instance_id"] == BACKEND_ID
        # P2-C: schema_objects is a plain read-only structural fact (same
        # query shape as app/inventory/store.py's own schema validation) --
        # this fixture only creates the two marker/backend tables, so that
        # is exactly what comes back. This tool makes no judgment about
        # whether the set is "right" for any target version; see
        # deploy/lib/update-plan.sh's _update_verify_preserve_schema_objects
        # for the caller-side comparison against a target's required set.
        assert facts["schema_objects"] == ["authority_schema", "backend_instance"]

    def test_marker_mismatch(self, tmp_path):
        db = tmp_path / "authority.db"
        _make_authority_db(db, marker="something_else")
        ok, facts = authority_tool._read_facts(str(db))
        assert ok is False
        assert facts["reason"] == "marker_mismatch"

    def test_user_version_mismatch(self, tmp_path):
        db = tmp_path / "authority.db"
        _make_authority_db(db, version=8)
        connection = sqlite3.connect(db)
        connection.execute("PRAGMA user_version=9")
        connection.commit()
        connection.close()
        ok, facts = authority_tool._read_facts(str(db))
        assert ok is False
        assert facts["reason"] == "user_version_mismatch"

    def test_invalid_backend_instance_id(self, tmp_path):
        db = tmp_path / "authority.db"
        _make_authority_db(db, backend_id="not-a-uuid")
        ok, facts = authority_tool._read_facts(str(db))
        assert ok is False
        assert facts["reason"] == "backend_instance_id_invalid"

    def test_not_a_database(self, tmp_path):
        db = tmp_path / "authority.db"
        db.write_text("this is not sqlite at all, just plain text padding" * 5)
        ok, facts = authority_tool._read_facts(str(db))
        assert ok is False
        assert facts["reason"] == "not_a_database"

    def test_missing_authority_tables(self, tmp_path):
        db = tmp_path / "authority.db"
        connection = sqlite3.connect(db)
        connection.execute("CREATE TABLE something_else (x INTEGER)")
        connection.commit()
        connection.close()
        ok, facts = authority_tool._read_facts(str(db))
        assert ok is False
        assert facts["reason"] == "no_authority_marker_table"

    def test_cli_inspect_prints_json(self, tmp_path, capsys):
        db = tmp_path / "authority.db"
        _make_authority_db(db)
        rc = authority_tool.cmd_inspect([str(db)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["backend_instance_id"] == BACKEND_ID


class TestPathState:
    def test_reports_existing_path(self, tmp_path, capsys):
        present = tmp_path / "present"
        present.write_text("x", encoding="utf-8")

        assert authority_tool.cmd_path_state([str(present)]) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True, "exists": True}

    def test_reports_enoent_as_absent(self, tmp_path, capsys):
        assert authority_tool.cmd_path_state([str(tmp_path / "absent")]) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True, "exists": False}

    def test_lstat_eio_is_not_reported_as_absent(self, tmp_path, monkeypatch, capsys):
        def _raise(_path):
            raise OSError(errno.EIO, "simulated probe failure")

        monkeypatch.setattr(authority_tool.os, "lstat", _raise)
        assert authority_tool.cmd_path_state([str(tmp_path / "unknown")]) != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"ok": False, "reason": "path_probe_failed"}
        assert payload.get("exists") is not False

    def test_dangling_symlink_is_existing_path_entry(self, tmp_path, capsys):
        dangling = tmp_path / "dangling"
        dangling.symlink_to(tmp_path / "missing-target")

        assert authority_tool.cmd_path_state([str(dangling)]) == 0
        assert json.loads(capsys.readouterr().out) == {"ok": True, "exists": True}


class TestBackup:
    def test_successful_backup_is_coherent_and_validated(self, tmp_path):
        src = tmp_path / "authority.db"
        dest = tmp_path / "backup" / "authority.db"
        dest.parent.mkdir()
        _make_authority_db(src)
        rc = authority_tool.cmd_backup([str(src), str(dest), MARKER, "8", BACKEND_ID])
        assert rc == 0
        assert dest.exists()
        # The backup is independently readable and passes integrity_check.
        connection = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        connection.close()

    def test_backup_refuses_on_live_recheck_mismatch(self, tmp_path, capsys):
        src = tmp_path / "authority.db"
        dest = tmp_path / "backup.db"
        _make_authority_db(src, version=8)
        rc = authority_tool.cmd_backup([str(src), str(dest), MARKER, "7", BACKEND_ID])
        assert rc != 0
        assert not dest.exists()
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "live_recheck" in payload["reason"]

    def test_backup_refuses_when_source_missing(self, tmp_path):
        src = tmp_path / "does-not-exist.db"
        dest = tmp_path / "backup.db"
        rc = authority_tool.cmd_backup([str(src), str(dest), MARKER, "8", BACKEND_ID])
        assert rc != 0
        assert not dest.exists()

    # -- Correction pass 9, P1: the durability barrier "backup" itself must
    # cross before ever reporting ok:true -- so the caller
    # (deploy/lib/update-activate.sh) never needs its own separate
    # shell-level sync for this transition; see test G in the task prompt's
    # durability test contract.

    def test_backup_crosses_durability_barrier_before_reporting_ok(self, tmp_path):
        src = tmp_path / "authority.db"
        dest = tmp_path / "backup" / "authority.db"
        dest.parent.mkdir()
        _make_authority_db(src)
        calls = []
        real_fsync_file_and_dir = authority_tool._fsync_file_and_dir

        def spy(path):
            # The backup file must already exist, validated and
            # integrity-checked, by the time the barrier is invoked --
            # i.e. the barrier crosses strictly AFTER content validation
            # and strictly BEFORE ok:true is ever printed.
            assert os.path.exists(path)
            calls.append(path)
            return real_fsync_file_and_dir(path)

        authority_tool._fsync_file_and_dir = spy
        try:
            rc = authority_tool.cmd_backup([str(src), str(dest), MARKER, "8", BACKEND_ID])
        finally:
            authority_tool._fsync_file_and_dir = real_fsync_file_and_dir
        assert rc == 0
        assert calls == [str(dest)]

    def test_backup_fails_closed_when_durability_barrier_fails(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "authority.db"
        dest = tmp_path / "backup.db"
        _make_authority_db(src)
        monkeypatch.setattr(authority_tool, "_fsync_file_and_dir", lambda path: False)
        rc = authority_tool.cmd_backup([str(src), str(dest), MARKER, "8", BACKEND_ID])
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"ok": False, "reason": "backup_durability_barrier_failed"}
        # A backup that never crossed its durability barrier is never left
        # behind as a misleading, seemingly-complete artifact.
        assert not dest.exists()


class TestDurabilityBarrierHelpers:
    def test_fsync_dir_succeeds_on_a_real_directory(self, tmp_path):
        assert authority_tool._fsync_dir(str(tmp_path)) is True

    def test_fsync_dir_fails_closed_on_a_nonexistent_directory(self, tmp_path):
        assert authority_tool._fsync_dir(str(tmp_path / "does-not-exist")) is False

    def test_fsync_file_and_dir_succeeds_on_a_real_file(self, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("x", encoding="utf-8")
        assert authority_tool._fsync_file_and_dir(str(target)) is True

    def test_fsync_file_and_dir_fails_closed_on_a_nonexistent_file(self, tmp_path):
        assert authority_tool._fsync_file_and_dir(str(tmp_path / "missing.txt")) is False


class TestRemove:
    def test_removes_db_and_sidecars(self, tmp_path):
        db = tmp_path / "authority.db"
        _make_authority_db(db)
        (tmp_path / "authority.db-wal").write_bytes(b"wal")
        (tmp_path / "authority.db-shm").write_bytes(b"shm")
        rc = authority_tool.cmd_remove([str(db)])
        assert rc == 0
        assert not db.exists()
        assert not (tmp_path / "authority.db-wal").exists()
        assert not (tmp_path / "authority.db-shm").exists()

    def test_remove_is_idempotent_on_missing_file(self, tmp_path):
        rc = authority_tool.cmd_remove([str(tmp_path / "nope.db")])
        assert rc == 0

    def test_remove_fails_closed_on_pre_unlink_probe_unknown(
        self, tmp_path, monkeypatch, capsys
    ):
        db = tmp_path / "authority.db"
        _make_authority_db(db)
        unlink_calls = []
        real_lstat = authority_tool.os.lstat

        def _probe(path):
            if path == str(db):
                raise OSError(errno.EACCES, "simulated probe failure", path)
            return real_lstat(path)

        monkeypatch.setattr(authority_tool.os, "lstat", _probe)
        monkeypatch.setattr(authority_tool.os, "unlink", unlink_calls.append)

        rc = authority_tool.cmd_remove([str(db)])
        assert rc != 0
        assert json.loads(capsys.readouterr().out) == {
            "ok": False,
            "reason": "path_probe_failed:authority.db",
        }
        assert unlink_calls == []
        assert db.exists()

    def test_remove_fails_closed_on_post_unlink_probe_unknown(
        self, tmp_path, monkeypatch, capsys
    ):
        db = tmp_path / "authority.db"
        _make_authority_db(db)
        real_lstat = authority_tool.os.lstat
        db_probe_count = 0

        def _probe(path):
            nonlocal db_probe_count
            if path == str(db):
                db_probe_count += 1
                if db_probe_count == 2:
                    raise OSError(errno.EIO, "simulated verification failure", path)
            return real_lstat(path)

        monkeypatch.setattr(authority_tool.os, "lstat", _probe)

        rc = authority_tool.cmd_remove([str(db)])
        assert rc != 0
        assert json.loads(capsys.readouterr().out) == {
            "ok": False,
            "reason": "path_probe_failed_after_remove:authority.db",
        }
        assert not db.exists()

    def test_remove_fails_closed_when_unlink_raises(self, tmp_path, monkeypatch, capsys):
        # P1-B: a present-but-unremovable file (permission error, busy
        # handle, read-only filesystem, ...) must be an immediate
        # {"ok": false} with a non-zero exit -- never silently swallowed
        # into a claimed success.
        db = tmp_path / "authority.db"
        _make_authority_db(db)

        def _raise(path):
            raise OSError(13, "Permission denied", path)

        monkeypatch.setattr(authority_tool.os, "unlink", _raise)
        rc = authority_tool.cmd_remove([str(db)])
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["reason"].startswith("unlink_failed:")
        # The mocked unlink never actually removed anything.
        assert db.exists()

    def test_remove_fails_closed_when_still_present_after_unlink(self, tmp_path, monkeypatch, capsys):
        # Never trust the unlink call's own reported success alone -- an
        # independent existence re-check after every unlink attempt must
        # also catch a path that (for whatever reason) is still present.
        db = tmp_path / "authority.db"
        _make_authority_db(db)

        monkeypatch.setattr(authority_tool.os, "unlink", lambda path: None)
        rc = authority_tool.cmd_remove([str(db)])
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["reason"].startswith("still_present_after_remove:")
        assert db.exists()

    # -- Correction pass 9, P1: the removal's durability barrier must cross
    # AFTER positive absence is independently re-verified and BEFORE
    # ok:true is ever reported.

    def test_remove_crosses_durability_barrier_after_verifying_absence(self, tmp_path):
        db = tmp_path / "authority.db"
        _make_authority_db(db)
        calls = []
        real_fsync_dir = authority_tool._fsync_dir

        def spy(dir_path):
            assert not db.exists(), "the barrier must run only after removal is verified"
            calls.append(dir_path)
            return real_fsync_dir(dir_path)

        authority_tool._fsync_dir = spy
        try:
            rc = authority_tool.cmd_remove([str(db)])
        finally:
            authority_tool._fsync_dir = real_fsync_dir
        assert rc == 0
        assert calls == [str(tmp_path)]

    def test_remove_fails_closed_when_durability_barrier_fails(self, tmp_path, monkeypatch, capsys):
        db = tmp_path / "authority.db"
        _make_authority_db(db)
        monkeypatch.setattr(authority_tool, "_fsync_dir", lambda path: False)
        rc = authority_tool.cmd_remove([str(db)])
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"ok": False, "reason": "remove_durability_barrier_failed"}
        # The removal itself genuinely happened in the running kernel --
        # only the durability PROOF failed, which is exactly why the
        # caller must still fail closed rather than trust "the file is
        # gone" as sufficient.
        assert not db.exists()


class TestUpdateProbe:
    def test_missing_agent_env_reports_reason(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(update_probe, "AGENT_ENV_PATH", str(tmp_path / "missing.env"))
        rc = update_probe.main()
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "could_not_read_bearer_token" in payload["reason"]

    def test_reads_bearer_token_from_agent_env(self, tmp_path):
        env_file = tmp_path / "agent.env"
        env_file.write_text(
            "HUBINET_OPS_R0_CONFIG=/etc/hubinet-ops/inventory.yaml\n"
            "HUBINET_OPS_R0_API_TOKEN=abc123\n",
            encoding="utf-8",
        )
        assert update_probe.read_bearer_token.__globals__ is update_probe.__dict__
        original = update_probe.AGENT_ENV_PATH
        try:
            update_probe.AGENT_ENV_PATH = str(env_file)
            assert update_probe.read_bearer_token() == "abc123"
        finally:
            update_probe.AGENT_ENV_PATH = original

    @staticmethod
    def _probe_env(tmp_path, monkeypatch):
        env_file = tmp_path / "agent.env"
        env_file.write_text("HUBINET_OPS_R0_API_TOKEN=abc123\n", encoding="utf-8")
        monkeypatch.setattr(update_probe, "AGENT_ENV_PATH", str(env_file))

    @staticmethod
    def _install_responses(monkeypatch, responses):
        def fake_get_json(path, token):
            assert token == "abc123"
            value = responses[path]
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(update_probe, "get_json", fake_get_json)

    @staticmethod
    def _base_responses(package_update):
        return {
            "/backend": {"backend_instance_id": BACKEND_ID},
            "/snapshot": {
                "sources": [
                    {"last_committed_run_sequence": 5, "health": "healthy", "freshness": "fresh"}
                ]
            },
            "/package-update/active": package_update,
        }

    def test_successful_probe_reports_facts(self, tmp_path, monkeypatch, capsys):
        self._probe_env(tmp_path, monkeypatch)
        self._install_responses(
            monkeypatch, self._base_responses({"active": False, "job": None})
        )
        rc = update_probe.main()
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["backend_instance_id"] == BACKEND_ID
        assert payload["last_committed_run_sequence"] == 5
        assert payload["health"] == "healthy"
        assert payload["package_update_active"] is False
        assert payload["package_update_job_id"] is None

    def test_active_workload_job_is_reported_with_its_identity(
        self, tmp_path, monkeypatch, capsys
    ):
        """The updater's fence witness. `true` plus enough to name the job."""

        self._probe_env(tmp_path, monkeypatch)
        self._install_responses(
            monkeypatch,
            self._base_responses(
                {
                    "active": True,
                    "job": {
                        "job_id": "11111111-2222-3333-4444-555555555555",
                        "checkpoint": "mutation_may_have_started",
                    },
                }
            ),
        )
        assert update_probe.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["package_update_active"] is True
        assert payload["package_update_job_id"] == "11111111-2222-3333-4444-555555555555"
        assert payload["package_update_checkpoint"] == "mutation_may_have_started"

    def test_absent_route_is_a_pre_activation_backend_not_a_refusal(
        self, tmp_path, monkeypatch, capsys
    ):
        """A 404 is the ONE failure that means "no job is possible here".

        A backend predating production activation has no update worker and no
        route, so it cannot own a workload job. That is read from the route's
        absence, and only from a real 404.
        """

        self._probe_env(tmp_path, monkeypatch)
        self._install_responses(
            monkeypatch,
            self._base_responses(
                urllib.error.HTTPError(
                    "http://127.0.0.1:8787/r0/v1/package-update/active",
                    404,
                    "Not Found",
                    {},
                    None,
                )
            ),
        )
        assert update_probe.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["package_update_active"] is None

    @pytest.mark.parametrize(
        "failure",
        [
            urllib.error.HTTPError(
                "http://127.0.0.1:8787/r0/v1/package-update/active",
                503,
                "Service Unavailable",
                {},
                None,
            ),
            urllib.error.URLError("connection refused"),
        ],
    )
    def test_unreachable_endpoint_is_never_read_as_no_active_job(
        self, tmp_path, monkeypatch, capsys, failure
    ):
        """"We could not ask" must never be reported as "the answer was no".

        Anything but a 404 makes the whole probe `ok: false`, which the
        updater treats as a refusal to proceed -- exactly the fail-closed
        posture an unanswerable safety question deserves.
        """

        self._probe_env(tmp_path, monkeypatch)
        self._install_responses(monkeypatch, self._base_responses(failure))
        assert update_probe.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "package_update" in payload["reason"]
        assert "package_update_active" not in payload

    def test_malformed_active_payload_fails_closed(self, tmp_path, monkeypatch, capsys):
        self._probe_env(tmp_path, monkeypatch)
        self._install_responses(
            monkeypatch, self._base_responses({"active": "yes", "job": None})
        )
        assert update_probe.main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["reason"] == "package_update_active_malformed"


# ---------------------------------------------------------------------------
# Family C -- pre-ACK side-effect timeout contract, the maintenance-fence
# client. `acquire()`'s POST can legitimately still be waiting on the
# backend's `acquire_product_update_maintenance_fence` when that route is
# itself waiting to become the authority store's one SQLite writer -- the
# real P1 this pass closes. These tests use a REAL local loopback HTTP
# server (never a private-network endpoint) and small, test-scaled
# durations -- never the real 105s/125s production budget -- to prove the
# actual `urllib` timeout mechanics `acquire()` relies on, the same
# methodology `tests/test_hubinet_ops_transport_http.py` already uses for
# the rollback/START HA timeouts.
# ---------------------------------------------------------------------------


class _DelayedFenceHandler(http.server.BaseHTTPRequestHandler):
    """Answers exactly one fence POST after a fixed, genuine delay."""

    delay_seconds: float = 0.0
    holder: str = "run-a"
    calls: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler method name
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        _time.sleep(self.delay_seconds)
        type(self).calls.append(self.path)
        body = json.dumps(
            {"holder": self.holder, "acquired_at": "2026-01-01T00:00:05+00:00"}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep test output quiet; failures still raise/assert normally


@contextmanager
def _run_delayed_fence_server(*, delay_seconds: float, holder: str):
    """Run `_DelayedFenceHandler` on a real loopback socket for one test."""

    handler_cls = type(
        "_ScopedDelayedFenceHandler",
        (_DelayedFenceHandler,),
        {"delay_seconds": delay_seconds, "holder": holder, "calls": []},
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/r0/v1", handler_cls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestUpdateFenceTimeout:
    def test_timeout_budget_matches_backend_writer_wait_policy(self) -> None:
        """Structural proof: the mirrored writer-wait constant has not
        drifted from the real backend policy, and the derived client
        timeout exceeds it -- never merely approximates it. If either
        assertion fails, the P1 this pass closes is silently reopened.
        """

        assert (
            update_fence.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR
            == contention_policy.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
        ), (
            "deploy/lib/hubinet-ops-update-fence.py's mirrored writer-wait "
            "budget has drifted from the real backend constant -- update "
            "AUTHORITY_WRITER_WAIT_BUDGET_SECONDS_MIRROR to match "
            "app.inventory.contention_policy.AUTHORITY_WRITER_WAIT_BUDGET_"
            "SECONDS"
        )
        assert (
            update_fence.TIMEOUT_SECONDS
            > contention_policy.AUTHORITY_WRITER_WAIT_BUDGET_SECONDS
        )
        # The old 15s-style deadline must be gone, not merely nudged.
        assert update_fence.TIMEOUT_SECONDS > 15

    def test_delayed_but_legitimate_fence_acquisition_is_not_abandoned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Required test 1/2: a fence response delayed beyond the old
        15s-style deadline, but within the supported authority writer
        budget, must be received -- the client must not abandon a request
        that may still legitimately acquire the fence -- and the returned
        holder/acquired_at are exactly the (delayed) durable answer, never
        a value invented before the fence actually existed.
        """

        monkeypatch.setattr(update_fence, "TIMEOUT_SECONDS", 2.0)
        with _run_delayed_fence_server(
            delay_seconds=1.0, holder="run-a"
        ) as (base_url, handler_cls):
            monkeypatch.setattr(update_fence, "BASE_URL", base_url)
            result = update_fence.acquire("run-a", "token")

        assert result == {
            "ok": True,
            "holder": "run-a",
            "acquired_at": "2026-01-01T00:00:05+00:00",
        }
        assert handler_cls.calls == ["/r0/v1/package-update/maintenance-fence"]

    def test_response_beyond_the_scaled_budget_still_fails_boundedly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Required test: the fence timeout is longer, not unbounded -- a
        response that outlasts even the (scaled) budget still reports a
        bounded failure rather than hanging. Because the REAL (unscaled)
        budget is proven above to exceed the backend's own real writer-wait
        ceiling, a genuine production timeout under the real contract is
        proof the backend's own legitimate pre-ACK window was already
        exceeded too -- never a race the backend could still legitimately
        win. That is required test 4: there is no path where the client
        reports failure while the backend can still durably create the
        fence afterwards for this same request.
        """

        monkeypatch.setattr(update_fence, "TIMEOUT_SECONDS", 0.3)
        with _run_delayed_fence_server(
            delay_seconds=2.0, holder="run-a"
        ) as (base_url, _handler_cls):
            monkeypatch.setattr(update_fence, "BASE_URL", base_url)
            start = _time.monotonic()
            result = update_fence.acquire("run-a", "token")
            elapsed = _time.monotonic() - start

        assert result["ok"] is False
        assert result["reason"] == "fence_endpoint_unreachable"
        assert elapsed < 5.0


# ---------------------------------------------------------------------------
# Family D -- semantic YAML inheritance/round-trip. The confirmed Codex P2:
# `_update_boundary_config_scalar` used to scan the installation's own YAML
# configuration as SOURCE TEXT with a line-oriented regex, not parse it, so
# an inline comment, a quoted `#`, or a YAML escape sequence inside a
# double-quoted scalar was returned lexically instead of decoded. These
# tests exercise the real `deploy/lib/hubinet-ops-update-host-control-
# fields.py` (real PyYAML, on the ordinary non-sandboxed pytest host --
# never the hardened update-smoke Docker sandbox, which has no PyYAML
# installed; see tests/test_update_boundary_yaml_scalar.py's own module
# docstring) and prove the semantic INPUT decode against the SAME real
# runtime parser (`app.inventory_runtime_config.parse_r0_runtime_config`),
# not merely against a hand-written expectation.
# ---------------------------------------------------------------------------

VALID_RUNTIME_ENV = {
    "HUBINET_OPS_R0_PVE_TOKEN": "root@pam!hubinet-ops=00000000-0000-0000-0000-000000000000",
    "HUBINET_OPS_R0_API_TOKEN": "a" * 32,
}


def _full_raw_config(pve_endpoint="https://pve.example.internal:8006", host_control=None):
    """The minimum complete `parse_r0_runtime_config`-valid mapping, with a
    caller-controlled `package_scan.host_control` mapping -- mirrors
    tests/test_inventory_runtime_config.py's own `_raw()` builder, kept
    local rather than imported so this file's dependency-free-by-default
    load list (see its own module docstring) is not disturbed.
    """

    return {
        "source": {
            "display_name": "Home Proxmox",
            "provider_kind": "proxmox_ve",
            "pve_endpoint": pve_endpoint,
            "freshness_duration_seconds": 300,
            "credential_reference": "secret://pve-token-v1",
            "pve_token_env": "HUBINET_OPS_R0_PVE_TOKEN",
            "tls": {"verify": True},
        },
        "runtime": {
            "authority_db_path": "/var/lib/hubinet-ops/authority.db",
            "api_token_env": "HUBINET_OPS_R0_API_TOKEN",
        },
        "package_scan": {
            "host_control": {
                "private_key_path": "/etc/hubinet-ops/host-control/id_ed25519",
                **(host_control or {}),
            },
        },
    }


def _write_yaml(tmp_path, raw) -> str:
    path = tmp_path / "inventory.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return str(path)


def _write_text(tmp_path, text: str) -> str:
    path = tmp_path / "inventory.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestUpdateHostControlFieldsSemanticDecode:
    """Isolated scalar-shape edge cases (required tests 1-7): the decoded
    value must be the SEMANTIC one, never the lexical source text."""

    def test_plain_scalar(self, tmp_path) -> None:
        path = _write_text(
            tmp_path,
            "source:\n  pve_endpoint: https://pve.example.internal:8006\n"
            "package_scan:\n  host_control:\n    host: pve.example.internal\n",
        )
        fields = host_control_fields.read_host_control_fields("package_scan", path)
        assert fields["host"] == "pve.example.internal"

    def test_inline_comment_is_not_part_of_the_value(self, tmp_path) -> None:
        """The exact Codex P2 witness: `host: pve.example # primary
        endpoint` must decode to `pve.example`, never the comment text
        too."""

        path = _write_text(
            tmp_path,
            "source:\n  pve_endpoint: https://pve.example.internal:8006\n"
            "package_scan:\n  host_control:\n"
            "    host: pve.example # primary endpoint\n",
        )
        fields = host_control_fields.read_host_control_fields("package_scan", path)
        assert fields["host"] == "pve.example"

    def test_single_quoted_scalar_containing_hash(self, tmp_path) -> None:
        path = _write_text(
            tmp_path,
            "source:\n  pve_endpoint: https://pve.example.internal:8006\n"
            "package_scan:\n  host_control:\n"
            "    user: 'svc#deploy'\n"
            "    private_key_path: /etc/hubinet-ops/host-control/id_ed25519\n"
            "    host: pve.example.internal\n",
        )
        fields = host_control_fields.read_host_control_fields("package_scan", path)
        assert fields["user"] == "svc#deploy"

    def test_double_quoted_escape_sequence(self, tmp_path) -> None:
        path = _write_text(
            tmp_path,
            "source:\n  pve_endpoint: https://pve.example.internal:8006\n"
            "package_scan:\n  host_control:\n"
            '    known_hosts_path: "/etc/hubinet-ops/host-control/known\\thosts"\n'
            "    host: pve.example.internal\n",
        )
        fields = host_control_fields.read_host_control_fields("package_scan", path)
        assert fields["known_hosts_path"] == "/etc/hubinet-ops/host-control/known\thosts"

    def test_literal_backslash(self, tmp_path) -> None:
        path = _write_text(
            tmp_path,
            "source:\n  pve_endpoint: https://pve.example.internal:8006\n"
            "package_scan:\n  host_control:\n"
            "    user: svc\\deploy\n"
            "    host: pve.example.internal\n",
        )
        fields = host_control_fields.read_host_control_fields("package_scan", path)
        assert fields["user"] == "svc\\deploy"

    def test_embedded_double_quote(self, tmp_path) -> None:
        path = _write_text(
            tmp_path,
            "source:\n  pve_endpoint: https://pve.example.internal:8006\n"
            "package_scan:\n  host_control:\n"
            '    user: svc"deploy\n'
            "    host: pve.example.internal\n",
        )
        fields = host_control_fields.read_host_control_fields("package_scan", path)
        assert fields["user"] == 'svc"deploy'

    def test_non_ascii(self, tmp_path) -> None:
        path = _write_text(
            tmp_path,
            "source:\n  pve_endpoint: https://pve.example.internal:8006\n"
            "package_scan:\n  host_control:\n"
            '    user: "opérateur"\n'
            "    host: pve.example.internal\n",
        )
        fields = host_control_fields.read_host_control_fields("package_scan", path)
        assert fields["user"] == "opérateur"


class TestUpdateHostControlFieldsDefaultsMatchRuntime:
    """Required tests 8-9: an omitted field's effective value must be the
    EXACT value the real `parse_r0_runtime_config` computes for the same
    file -- proven against the real runtime parser, not a hand-copied
    expectation that could drift from it unnoticed."""

    def test_omitted_fields_get_exact_runtime_defaults(self, tmp_path) -> None:
        raw = _full_raw_config(host_control={"host": "pve.example.internal"})
        path = _write_yaml(tmp_path, raw)

        fields = host_control_fields.read_host_control_fields("package_scan", path)
        runtime_config = parse_r0_runtime_config(raw, env=VALID_RUNTIME_ENV)
        runtime_host_control = runtime_config.package_scan.host_control

        assert fields["port"] == runtime_host_control.port == 22
        assert fields["user"] == runtime_host_control.user == "root"
        assert (
            fields["known_hosts_path"]
            == str(runtime_host_control.known_hosts_path)
            == "/etc/hubinet-ops/host-control/known_hosts"
        )

    def test_omitted_host_derives_the_exact_runtime_hostname(self, tmp_path) -> None:
        raw = _full_raw_config(
            pve_endpoint="https://pve-other.example.internal:8006",
            host_control={
                "port": 2222,
                "user": "svc-deploy",
                "known_hosts_path": "/custom/known_hosts",
            },
        )
        path = _write_yaml(tmp_path, raw)

        fields = host_control_fields.read_host_control_fields("package_scan", path)
        runtime_config = parse_r0_runtime_config(raw, env=VALID_RUNTIME_ENV)
        runtime_host_control = runtime_config.package_scan.host_control

        assert fields["host"] == runtime_host_control.host == "pve-other.example.internal"
        # Positive control: explicit values in the same file are preserved,
        # not overwritten by defaults meant only for the omitted host.
        assert fields["port"] == runtime_host_control.port == 2222
        assert fields["user"] == runtime_host_control.user == "svc-deploy"
        assert fields["known_hosts_path"] == str(runtime_host_control.known_hosts_path) == "/custom/known_hosts"


class TestUpdateHostControlFieldsRoundTrip:
    def test_activated_package_update_config_parses_to_the_current_runtime_values(
        self, tmp_path
    ) -> None:
        """Required test 10: the INPUT semantic decode and OUTPUT semantic
        re-encode together. Reads the CURRENT runtime's effective
        `package_scan.host_control` values (cross-checked against the real
        `parse_r0_runtime_config`, exactly like the two tests above), emits
        them into an activated `package_update.host_control` block using
        the SAME JSON-safe double-quoted scalar shape
        `_update_boundary_yaml_dq_scalar` produces in the real bash
        updater, and proves reading that activated block back
        (`package_update` mode -- no defaulting, exact values) parses to
        values EXACTLY equal to what the current runtime computed --
        including the escape/quote-bearing scalars the old lexical scanner
        could not have round-tripped.
        """

        host_value = 'pve\\example.internal'
        user_value = 'svc"deploy # not a comment'
        known_hosts_value = "/etc/hubinet-ops/host-control/kn\town_hosts"

        raw = _full_raw_config(
            host_control={"host": host_value, "user": user_value, "known_hosts_path": known_hosts_value}
        )
        current_path = _write_yaml(tmp_path, raw)

        runtime_config = parse_r0_runtime_config(raw, env=VALID_RUNTIME_ENV)
        runtime_host_control = runtime_config.package_scan.host_control

        inherited = host_control_fields.read_host_control_fields(
            "package_scan", current_path
        )
        assert inherited["host"] == runtime_host_control.host == host_value
        assert inherited["user"] == runtime_host_control.user == user_value
        assert (
            inherited["known_hosts_path"]
            == str(runtime_host_control.known_hosts_path)
            == known_hosts_value
        )

        # The exact serialization _update_boundary_yaml_dq_scalar uses in
        # production: JSON's string syntax is a legal YAML double-quoted
        # flow scalar (YAML 1.2 is a strict superset of JSON).
        activated_path = tmp_path / "activated.yaml"
        activated_path.write_text(
            "package_update:\n"
            "  enabled: true\n"
            "  host_control:\n"
            f"    host: {json.dumps(inherited['host'])}\n"
            f"    port: {inherited['port']}\n"
            f"    user: {json.dumps(inherited['user'])}\n"
            f"    known_hosts_path: {json.dumps(inherited['known_hosts_path'])}\n",
            encoding="utf-8",
        )
        activated = host_control_fields.read_host_control_fields(
            "package_update", str(activated_path)
        )
        assert activated == inherited


class _FakeAcceptClock:
    """Deterministic fake for time.monotonic/time.sleep -- advances only
    when .sleep() is called (never sleeps for real), so a bounded
    acceptance-polling test proves real ordering/timeout behavior without
    ever waiting in real time (AGENTS.md task prompt correction pass 9)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleep_calls = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1
        self.now += seconds


class TestAcceptScriptMinSequenceExtension:
    """The in-place updater reuses hubinet-ops-bootstrap-accept.py's
    committed-source check with an added optional floor -- see
    deploy/lib/update-activate.sh's _update_accept_discovery. These tests
    exercise the extended contract directly, without any bash/sandbox
    involvement."""

    def _source(self, sequence: int) -> dict:
        return {
            "latest_completed_outcome": "success",
            "last_committed_run_sequence": sequence,
            "last_successful_observed_at": "2026-01-01T00:00:00+00:00",
            "committed_context": {f: "x" for f in accept_script.CONTEXT_FIELDS},
            "current_context": {f: "x" for f in accept_script.CONTEXT_FIELDS},
        }

    def test_no_floor_behaves_like_original_contract(self):
        assert accept_script._check_committed_source(self._source(1)) is None

    def test_sequence_past_floor_passes(self):
        assert accept_script._check_committed_source(self._source(5), min_sequence_exclusive=1) is None

    def test_sequence_not_past_floor_fails(self):
        reason = accept_script._check_committed_source(self._source(1), min_sequence_exclusive=1)
        assert reason is not None
        assert "committed-sequence-not-past-baseline" in reason
        assert accept_script._committed_source_failure_is_transient(reason)

    def test_context_mismatch_under_an_unmet_floor_is_immediate_not_transient(self):
        # Correction pass 9, P1 test C: a real structural problem (context
        # mismatch) must never be masked as "not yet" merely because the
        # sequence also happens to be at or below the floor.
        source = self._source(1)
        source["current_context"] = dict(source["current_context"])
        first_field = accept_script.CONTEXT_FIELDS[0]
        source["current_context"][first_field] = "a-different-value"
        reason = accept_script._check_committed_source(source, min_sequence_exclusive=5)
        assert reason is not None
        assert "committed-current-context-mismatch" in reason
        assert not accept_script._committed_source_failure_is_transient(reason)

    def test_invalid_outcome_under_an_unmet_floor_is_immediate_not_transient(self):
        source = self._source(1)
        source["latest_completed_outcome"] = "partial"
        reason = accept_script._check_committed_source(source, min_sequence_exclusive=5)
        assert reason is not None
        assert "latest-completed-outcome-not-success" in reason
        assert not accept_script._committed_source_failure_is_transient(reason)

    def test_main_rejects_invalid_floor_argument(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["accept.py", "Home Proxmox", "5", "not-a-number"])
        rc = accept_script.main()
        assert rc == 1
        assert "invalid min-committed-sequence-exclusive" in capsys.readouterr().out

    def _install_fake_clock(self, monkeypatch) -> _FakeAcceptClock:
        clock = _FakeAcceptClock()
        monkeypatch.setattr(accept_script.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(accept_script.time, "sleep", clock.sleep)
        return clock

    def _healthy_fresh_source_response(self, sequence: int) -> dict:
        source = self._source(sequence)
        source.update({"name": "Home Proxmox", "health": "healthy", "freshness": "fresh"})
        return {"sources": [source], "nodes": [{"n": 1}], "resources": []}

    def test_main_polls_through_transient_baseline_then_passes(self, monkeypatch, capsys):
        # Test A (AGENTS.md task prompt correction pass 9): baseline=10,
        # snapshots 10/10/11 (all otherwise healthy/fresh) must PASS inside
        # the same bounded acceptance invocation -- no second timeout, no
        # bash-level retry, entirely the script's own existing poll loop.
        monkeypatch.setattr(accept_script, "read_bearer_token", lambda: "token")
        clock = self._install_fake_clock(monkeypatch)
        sequences = iter([10, 10, 11])
        calls = {"backend": 0, "snapshot": 0}

        def fake_get_json(path, token):
            if path == "/backend":
                calls["backend"] += 1
                return {"backend_instance_id": BACKEND_ID}
            calls["snapshot"] += 1
            return self._healthy_fresh_source_response(next(sequences))

        monkeypatch.setattr(accept_script, "get_json", fake_get_json)
        monkeypatch.setattr(sys, "argv", ["accept.py", "Home Proxmox", "300", "10"])
        rc = accept_script.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        last_line = out.strip().splitlines()[-1]
        assert last_line.startswith("PASS"), out
        assert "last_committed_run_sequence=11" in last_line
        assert calls["snapshot"] == 3
        # Two transient not-past-baseline polls before the passing one.
        assert clock.sleep_calls == 2

    def test_main_times_out_when_sequence_never_advances(self, monkeypatch, capsys):
        # Test B: sequence remains at the baseline forever -> FAIL via the
        # EXISTING discovery timeout, never a second invented timeout.
        monkeypatch.setattr(accept_script, "read_bearer_token", lambda: "token")
        clock = self._install_fake_clock(monkeypatch)

        def fake_get_json(path, token):
            if path == "/backend":
                return {"backend_instance_id": BACKEND_ID}
            return self._healthy_fresh_source_response(10)

        monkeypatch.setattr(accept_script, "get_json", fake_get_json)
        monkeypatch.setattr(sys, "argv", ["accept.py", "Home Proxmox", "6", "10"])
        rc = accept_script.main()
        out = capsys.readouterr().out
        assert rc == 1, out
        last_line = out.strip().splitlines()[-1]
        assert last_line.startswith("FAIL discovery-timeout"), out
        assert "committed-sequence-not-past-baseline" in last_line
        # The fake clock only ever advances via time.sleep -- proves the
        # loop actually polled repeatedly (never a single immediate FAIL)
        # before genuinely exhausting the configured deadline.
        assert clock.sleep_calls >= 2
        assert clock.now >= 6

    def test_main_ordinary_call_with_no_floor_is_unaffected(self, monkeypatch, capsys):
        # Test D: an ordinary bootstrap call (no 4th argument) must PASS
        # immediately on the first coherent snapshot, exactly as before.
        monkeypatch.setattr(accept_script, "read_bearer_token", lambda: "token")
        clock = self._install_fake_clock(monkeypatch)

        def fake_get_json(path, token):
            if path == "/backend":
                return {"backend_instance_id": BACKEND_ID}
            return self._healthy_fresh_source_response(1)

        monkeypatch.setattr(accept_script, "get_json", fake_get_json)
        monkeypatch.setattr(sys, "argv", ["accept.py", "Home Proxmox", "180"])
        rc = accept_script.main()
        out = capsys.readouterr().out
        assert rc == 0, out
        assert out.strip().splitlines()[-1].startswith("PASS"), out
        assert clock.sleep_calls == 0

    def test_main_immediate_context_mismatch_under_baseline_never_retries(self, monkeypatch, capsys):
        # Test C at the main()/loop level: a real structural problem must
        # fail on the FIRST poll, never be retried through to timeout.
        monkeypatch.setattr(accept_script, "read_bearer_token", lambda: "token")
        clock = self._install_fake_clock(monkeypatch)

        def fake_get_json(path, token):
            if path == "/backend":
                return {"backend_instance_id": BACKEND_ID}
            response = self._healthy_fresh_source_response(1)
            source = response["sources"][0]
            source["current_context"] = dict(source["current_context"])
            first_field = accept_script.CONTEXT_FIELDS[0]
            source["current_context"][first_field] = "a-different-value"
            return response

        monkeypatch.setattr(accept_script, "get_json", fake_get_json)
        monkeypatch.setattr(sys, "argv", ["accept.py", "Home Proxmox", "300", "5"])
        rc = accept_script.main()
        out = capsys.readouterr().out
        assert rc == 1, out
        last_line = out.strip().splitlines()[-1]
        assert "committed-current-context-mismatch" in last_line, out
        assert clock.sleep_calls == 0


# ---------------------------------------------------------------------------
# Correction pass 8, P1 -- the REAL, non-faked half of the final-path venv
# proof.
#
# These build a genuine stdlib virtualenv in tmp_path and read back a
# console script venv/pip generated itself. Bounded and offline: with_pip
# is bootstrapped by ensurepip from CPython's own bundled wheels, and
# PIP_NO_INDEX/PIP_NO_INPUT are exported so no test can reach a package
# index even if one were reachable. Nothing here touches PVE, LXC, systemd,
# apt, or the pytest host's own environment.
# ---------------------------------------------------------------------------

venv_build = _load(VENV_BUILD_PATH, "hubinet_ops_update_venv_build")


def _offline_env() -> dict:
    env = dict(os.environ)
    env["PIP_NO_INDEX"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_RETRIES"] = "0"
    return env


def _entrypoint_header(script_path: Path) -> str:
    """The interpreter-selection preamble of a pip-generated console script.

    Short venv paths produce a plain `#!<venv>/bin/python` shebang; a path
    over the kernel's shebang limit produces the equivalent `#!/bin/sh` +
    `'''exec' <abs-path>` wrapper instead. Both embed the SAME absolute
    interpreter path, so both forms are covered by reading the preamble.
    """

    with script_path.open("r", encoding="utf-8", errors="replace") as handle:
        return "".join(next(handle, "") for _ in range(3))


@pytest.fixture(scope="module")
def _ensurepip_available() -> None:
    try:
        import ensurepip  # noqa: F401
    except ImportError:  # pragma: no cover -- distro-stripped python3
        pytest.skip("this interpreter has no ensurepip, so no venv can be built offline")


class TestFinalPathVirtualenvBuild:
    def test_generated_entrypoint_names_the_path_the_venv_was_built_at(
        self, tmp_path, _ensurepip_available
    ):
        final = tmp_path / "opt" / "hubinet-ops" / ".venv"
        final.parent.mkdir(parents=True)
        venv_build.create_environment(final)

        pip_script = final / "bin" / "pip"
        assert pip_script.is_file()
        header = _entrypoint_header(pip_script)
        assert f"{final}/bin/python" in header, header
        assert ".staged-" not in header, header

    def test_a_renamed_virtualenv_keeps_its_original_interpreter_path(
        self, tmp_path, _ensurepip_available
    ):
        """The exact reason a staged-then-renamed venv design is rejected.

        A virtualenv is not relocatable: renaming the directory does not
        rewrite the absolute interpreter path pip baked into every console
        script it generated. This is the regression that keeps the
        "build at .venv.staged-<runid>, then mv onto .venv" design from
        coming back.
        """
        staged = tmp_path / "opt" / "hubinet-ops" / ".venv.staged-aaaaaaaa"
        live = tmp_path / "opt" / "hubinet-ops" / ".venv"
        staged.parent.mkdir(parents=True)
        venv_build.create_environment(staged)

        shutil.move(str(staged), str(live))

        header = _entrypoint_header(live / "bin" / "pip")
        assert f"{staged}/bin/python" in header, (
            "renaming a virtualenv must NOT rewrite generated entrypoints -- if this "
            "ever becomes false the staged-then-rename design would be safe, but it "
            "is not, so the updater builds at the final path instead"
        )
        assert f"{live}/bin/python" not in header, header
        assert not staged.exists()

    def test_main_builds_at_the_exact_destination_it_is_given(
        self, tmp_path, _ensurepip_available
    ):
        """End-to-end through the production script's own argv contract."""
        final = tmp_path / "opt" / "hubinet-ops" / ".venv"
        final.parent.mkdir(parents=True)
        requirements = tmp_path / "requirements.txt"
        requirements.write_text("", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(VENV_BUILD_PATH), str(final), str(requirements)],
            capture_output=True,
            env=_offline_env(),
            timeout=300,
        )
        # Offline, `pip install --upgrade pip` legitimately cannot resolve a
        # distribution, so a non-zero exit here is the expected shape of a
        # failed build -- and it must leave the (partial) environment at the
        # FINAL pathname, which is exactly what update-activate.sh's
        # rollback removes and proves absent.
        assert final.is_dir()
        header = _entrypoint_header(final / "bin" / "pip")
        assert f"{final}/bin/python" in header, (header, result.stderr[-500:])
        assert ".staged-" not in header

    def test_refuses_an_already_existing_destination(self, tmp_path):
        existing = tmp_path / ".venv"
        existing.mkdir()
        (existing / "sentinel").write_text("do not touch", encoding="utf-8")
        requirements = tmp_path / "requirements.txt"
        requirements.write_text("", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(VENV_BUILD_PATH), str(existing), str(requirements)],
            capture_output=True,
            env=_offline_env(),
            timeout=60,
        )
        assert result.returncode == 1
        assert b"already-existing path" in result.stderr
        assert (existing / "sentinel").read_text(encoding="utf-8") == "do not touch"
        assert not (existing / "bin").exists()

    def test_refuses_a_missing_requirements_file(self, tmp_path):
        final = tmp_path / ".venv"
        result = subprocess.run(
            [sys.executable, str(VENV_BUILD_PATH), str(final), str(tmp_path / "absent.txt")],
            capture_output=True,
            env=_offline_env(),
            timeout=60,
        )
        assert result.returncode == 1
        assert b"requirements file does not exist" in result.stderr
        assert not final.exists()
