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
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_TOOL_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-authority-tool.py"
UPDATE_PROBE_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-update-probe.py"
ACCEPT_SCRIPT_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-bootstrap-accept.py"
VENV_BUILD_PATH = ROOT / "deploy" / "lib" / "hubinet-ops-update-venv-stage.py"

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
accept_script = _load(ACCEPT_SCRIPT_PATH, "hubinet_ops_bootstrap_accept")


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

    def test_successful_probe_reports_facts(self, tmp_path, monkeypatch, capsys):
        env_file = tmp_path / "agent.env"
        env_file.write_text("HUBINET_OPS_R0_API_TOKEN=abc123\n", encoding="utf-8")
        monkeypatch.setattr(update_probe, "AGENT_ENV_PATH", str(env_file))

        responses = {
            "/backend": {"backend_instance_id": BACKEND_ID},
            "/snapshot": {
                "sources": [
                    {"last_committed_run_sequence": 5, "health": "healthy", "freshness": "fresh"}
                ]
            },
        }

        def fake_get_json(path, token):
            assert token == "abc123"
            return responses[path]

        monkeypatch.setattr(update_probe, "get_json", fake_get_json)
        rc = update_probe.main()
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["backend_instance_id"] == BACKEND_ID
        assert payload["last_committed_run_sequence"] == 5
        assert payload["health"] == "healthy"


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

    def test_main_accepts_optional_fourth_argument(self, monkeypatch, capsys):
        monkeypatch.setattr(accept_script, "read_bearer_token", lambda: "token")

        def fake_get_json(path, token):
            if path == "/backend":
                return {"backend_instance_id": BACKEND_ID}
            source = self._source(1)
            source.update({"name": "Home Proxmox", "health": "healthy", "freshness": "fresh"})
            return {"sources": [source], "nodes": [{"n": 1}], "resources": []}

        monkeypatch.setattr(accept_script, "get_json", fake_get_json)
        monkeypatch.setattr(sys, "argv", ["accept.py", "Home Proxmox", "5", "1"])
        rc = accept_script.main()
        assert rc == 1
        out = capsys.readouterr().out
        assert "committed-sequence-not-past-baseline" in out

    def test_main_rejects_invalid_floor_argument(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["accept.py", "Home Proxmox", "5", "not-a-number"])
        rc = accept_script.main()
        assert rc == 1
        assert "invalid min-committed-sequence-exclusive" in capsys.readouterr().out


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
