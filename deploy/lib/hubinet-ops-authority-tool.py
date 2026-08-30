#!/usr/bin/env python3
"""Minimal, read-only-first authority-database tool for the in-place updater.

Invoked INSIDE the Hubinet CT (via `pct exec <vmid> -- python3 <this-file>
<subcommand> ...`), never on the Proxmox host. Uses only the Python stdlib
(``sqlite3``, ``json``, ``uuid``) so it runs against the CT's system
python3 -- it never imports ``app.inventory`` and never depends on the
service's own virtualenv, so it keeps working even while that venv is
mid-swap during an update.

This script does not invent, migrate, or write any authority SCHEMA. It
only classifies an existing database (marker/version/backend identity),
makes one coherent backup via the sqlite3 stdlib backup API, and removes a
database file (plus WAL/SHM sidecars) once a caller-validated backup
exists. The target runtime is solely responsible for creating a fresh
schema after a reset -- see AGENTS.md/STATUS.md and
deploy/lib/update-authority.sh.

Subcommands (argv[1]):

  path-state <path>
      Read-only. Reports whether one updater-owned path exists as
      {"ok": true, "exists": true|false}. An OS-level probe error is
      {"ok": false, "reason": "path_probe_failed"} with non-zero exit.
      The bash caller independently restricts paths to its fixed live and
      run-owned rollback paths before invoking this operation.

  inspect <db_path>
      Read-only. Prints one JSON object to stdout:
        {"ok": true, "exists": true, "marker": "...", "schema_version": N,
         "backend_instance_id": "...", "schema_objects": ["...", ...]}
      schema_objects is the sorted list of table/index/trigger names
      actually present (the same structural fact
      app/inventory/store.py's own schema validation checks) -- a plain
      read-only fact, not a judgment; this tool has no target version to
      compare it against.
      or
        {"ok": false, "exists": <bool>, "reason": "<short-code>"}
      Never raises for an expected malformed/missing condition -- every
      failure path is reported via "ok": false so the caller (bash) can
      make a fail-closed decision without depending on this process's
      exit code alone. Exit code is always 0 unless argv itself is
      malformed.

  backup <db_path> <dest_path> <expected_marker> <expected_schema_version>
         <expected_backend_instance_id>
      Requires the live database to still match every "expected_*" value
      (a live re-check immediately before backing up, closing the window
      between an earlier `inspect` call and this one). Creates ONE
      coherent SQLite backup of <db_path> at <dest_path> via the sqlite3
      stdlib online backup API (never a raw file copy of a WAL-mode
      database, which is not guaranteed point-in-time consistent), then
      reopens the backup read-only and re-validates PRAGMA
      integrity_check plus the same marker/version/backend-identity
      facts against the backup itself. Prints one JSON object:
        {"ok": true, "backup_path": "...", "backend_instance_id": "..."}
      or
        {"ok": false, "reason": "<short-code>"}
      On any failure, <dest_path> is removed if this invocation created
      it, so a failed backup never leaves a partial/misleading file
      behind. Exit code is 0 only when "ok" is true; non-zero on both
      argv errors and every reported "ok": false, so a bash caller can
      use returncode alone as the fail-closed gate while offering `inspect`-
      style JSON on both stdout paths for a specific reason.

  remove <db_path>
      Removes <db_path> and its WAL/SHM sidecars (<db_path>-wal,
      <db_path>-shm). Idempotent -- a missing file is not an error. Never
      called by update-authority.sh except immediately after a `backup`
      subcommand has reported "ok": true for the exact same database.
      Fails closed: for each of the three paths, a present-but-unremovable
      file (permission error, busy handle, read-only filesystem, ...) is
      an immediate {"ok": false, "reason": "<short-code>"} with a non-zero
      exit -- never silently swallowed. After every removal attempt, this
      command independently re-verifies (a fresh existence check, not the
      unlink call's own reported success) that all three paths are
      actually absent before ever printing {"ok": true}; if any target is
      still present after that verification, it is still {"ok": false}.
      Never prints {"ok": true} on a best-effort basis.

Never writes schema DDL, never opens a write connection to <db_path> in
`inspect` mode, and never touches secrets -- only structural authority
facts (marker text, an integer schema version, and a UUID-shaped
backend_instance_id) cross back to the caller, exactly like the existing
deploy/lib/hubinet-ops-bootstrap-accept.py convention.
"""
from __future__ import annotations

import errno
import json
import os
import sqlite3
import sys
import uuid

# app/inventory/store.py::AUTHORITY_SCHEMA_MARKER -- duplicated here (not
# imported) deliberately, so this tool never depends on the service's own
# venv/import graph. Keep in sync if that constant's *text* ever changes;
# its current value is itself part of this repository's stable on-disk
# contract, not expected to change casually.
EXPECTED_MARKER_TEXT = "hubinet_ops_0_5_authority"

_PATH_EXISTS = "exists"
_PATH_ABSENT = "absent"
_PATH_UNKNOWN = "unknown"


def _path_entry_state(path: str) -> str:
    """Strictly classify one path entry without hiding probe failures."""

    try:
        os.lstat(path)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return _PATH_ABSENT
        return _PATH_UNKNOWN
    return _PATH_EXISTS


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.int != 0 and str(parsed) == value


def _read_facts(db_path: str) -> tuple[bool, dict]:
    """Read-only inspection. Returns (ok, facts_or_reason_dict)."""

    if not os.path.isfile(db_path) or os.path.getsize(db_path) == 0:
        return False, {"exists": False, "reason": "missing_or_empty"}

    try:
        uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return False, {"exists": True, "reason": "cannot_open"}

    try:
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except sqlite3.DatabaseError:
            return False, {"exists": True, "reason": "not_a_database"}

        if "authority_schema" not in tables or "backend_instance" not in tables:
            return False, {"exists": True, "reason": "no_authority_marker_table"}

        try:
            marker_rows = connection.execute(
                "SELECT marker, schema_version FROM authority_schema"
            ).fetchall()
            user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            backend_rows = connection.execute(
                "SELECT backend_instance_id FROM backend_instance"
            ).fetchall()
        except sqlite3.DatabaseError:
            return False, {"exists": True, "reason": "structurally_unreadable"}

        if len(marker_rows) != 1:
            return False, {"exists": True, "reason": "marker_row_not_singleton"}
        marker = str(marker_rows[0]["marker"])
        schema_version = marker_rows[0]["schema_version"]
        if type(schema_version) is not int or schema_version <= 0:
            return False, {"exists": True, "reason": "schema_version_malformed"}
        if marker != EXPECTED_MARKER_TEXT:
            return False, {"exists": True, "reason": "marker_mismatch"}
        if user_version != schema_version:
            return False, {"exists": True, "reason": "user_version_mismatch"}
        if len(backend_rows) != 1 or not _is_canonical_uuid(
            backend_rows[0]["backend_instance_id"]
        ):
            return False, {"exists": True, "reason": "backend_instance_id_invalid"}

        # schema_objects: the same read-only structural fact
        # app/inventory/store.py's own _open_or_initialize/_validate_schema
        # checks itself (identical SQL shape) -- table/index/trigger names,
        # sqlite_-internal names excluded. This tool still makes no
        # judgment about whether the set is "right" for any particular
        # target version (it has no target to compare against and
        # deliberately never invents schema knowledge); the caller
        # (deploy/lib/update-plan.sh) is the one that statically extracts
        # the target's required set and compares, before ever stopping the
        # service on a would-be schema-preserving update.
        schema_objects = sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger') "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )

        return True, {
            "exists": True,
            "marker": marker,
            "schema_version": schema_version,
            "backend_instance_id": str(backend_rows[0]["backend_instance_id"]),
            "schema_objects": schema_objects,
        }
    finally:
        connection.close()


def cmd_inspect(argv: list[str]) -> int:
    if len(argv) != 1:
        print(json.dumps({"ok": False, "reason": "usage"}))
        return 2
    ok, facts = _read_facts(argv[0])
    print(json.dumps({"ok": ok, **facts}, separators=(",", ":")))
    return 0


def cmd_path_state(argv: list[str]) -> int:
    if len(argv) != 1:
        print(json.dumps({"ok": False, "reason": "usage"}))
        return 2
    state = _path_entry_state(argv[0])
    if state == _PATH_UNKNOWN:
        print(json.dumps({"ok": False, "reason": "path_probe_failed"}))
        return 1
    print(json.dumps({"ok": True, "exists": state == _PATH_EXISTS}, separators=(",", ":")))
    return 0


def cmd_backup(argv: list[str]) -> int:
    if len(argv) != 5:
        print(json.dumps({"ok": False, "reason": "usage"}))
        return 2
    db_path, dest_path, expected_marker, expected_version_raw, expected_backend_id = argv
    try:
        expected_version = int(expected_version_raw)
    except ValueError:
        print(json.dumps({"ok": False, "reason": "usage"}))
        return 2

    ok, facts = _read_facts(db_path)
    if not ok:
        print(json.dumps({"ok": False, "reason": f"live_recheck_{facts.get('reason', 'failed')}"}))
        return 1
    if (
        facts["marker"] != expected_marker
        or facts["schema_version"] != expected_version
        or facts["backend_instance_id"] != expected_backend_id
    ):
        print(json.dumps({"ok": False, "reason": "live_recheck_context_changed"}))
        return 1

    dest_created_here = not os.path.exists(dest_path)
    try:
        source = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error:
        print(json.dumps({"ok": False, "reason": "backup_source_open_failed"}))
        return 1
    try:
        try:
            dest = sqlite3.connect(dest_path, timeout=5.0)
        except sqlite3.Error:
            print(json.dumps({"ok": False, "reason": "backup_dest_open_failed"}))
            return 1
        try:
            source.backup(dest)
        except sqlite3.Error:
            dest.close()
            if dest_created_here:
                _silent_unlink(dest_path)
            print(json.dumps({"ok": False, "reason": "backup_copy_failed"}))
            return 1
        dest.close()
    finally:
        source.close()

    # Reopen the freshly written backup for independent post-write
    # validation -- never trust the copy call's own success alone.
    ok, backup_facts = _read_facts(dest_path)
    if not ok:
        if dest_created_here:
            _silent_unlink(dest_path)
        print(json.dumps({"ok": False, "reason": f"backup_validation_{backup_facts.get('reason', 'failed')}"}))
        return 1
    if (
        backup_facts["marker"] != expected_marker
        or backup_facts["schema_version"] != expected_version
        or backup_facts["backend_instance_id"] != expected_backend_id
    ):
        if dest_created_here:
            _silent_unlink(dest_path)
        print(json.dumps({"ok": False, "reason": "backup_content_mismatch"}))
        return 1

    try:
        integrity_connection = sqlite3.connect(f"file:{os.path.abspath(dest_path)}?mode=ro", uri=True)
        integrity_result = integrity_connection.execute("PRAGMA integrity_check").fetchone()
        integrity_connection.close()
    except sqlite3.Error:
        if dest_created_here:
            _silent_unlink(dest_path)
        print(json.dumps({"ok": False, "reason": "backup_integrity_check_failed"}))
        return 1
    if integrity_result is None or str(integrity_result[0]).lower() != "ok":
        if dest_created_here:
            _silent_unlink(dest_path)
        print(json.dumps({"ok": False, "reason": "backup_integrity_check_not_ok"}))
        return 1

    print(json.dumps({
        "ok": True,
        "backup_path": os.path.abspath(dest_path),
        "backend_instance_id": backup_facts["backend_instance_id"],
    }, separators=(",", ":")))
    return 0


def _silent_unlink(path: str) -> None:
    # Best-effort only -- used exclusively for this tool's OWN cleanup of a
    # backup/dest file it just created itself on an earlier failure path
    # (see cmd_backup above). Never used for cmd_remove's fail-closed
    # target-database removal below.
    try:
        os.unlink(path)
    except OSError:
        pass


def cmd_remove(argv: list[str]) -> int:
    if len(argv) != 1:
        print(json.dumps({"ok": False, "reason": "usage"}))
        return 2
    db_path = argv[0]
    candidates = (db_path, db_path + "-wal", db_path + "-shm")
    for candidate in candidates:
        state = _path_entry_state(candidate)
        if state == _PATH_UNKNOWN:
            print(json.dumps({
                "ok": False,
                "reason": f"path_probe_failed:{os.path.basename(candidate)}",
            }))
            return 1
        if state == _PATH_ABSENT:
            continue
        try:
            os.unlink(candidate)
        except OSError as exc:
            print(json.dumps({
                "ok": False,
                "reason": f"unlink_failed:{os.path.basename(candidate)}:{exc.errno or 'unknown'}",
            }))
            return 1

    # Never trust the unlink calls' own reported success alone -- an
    # independent existence re-check closes any gap between "os.unlink()
    # raised nothing" and "the path is actually gone" (e.g. a concurrent
    # recreate, or a filesystem that accepts an unlink() call but does not
    # actually make the path disappear).
    verification_states = [
        (candidate, _path_entry_state(candidate))
        for candidate in candidates
    ]
    unknown = [
        candidate
        for candidate, state in verification_states
        if state == _PATH_UNKNOWN
    ]
    if unknown:
        print(json.dumps({
            "ok": False,
            "reason": "path_probe_failed_after_remove:"
            + ",".join(os.path.basename(p) for p in unknown),
        }))
        return 1

    still_present = [
        candidate
        for candidate, state in verification_states
        if state == _PATH_EXISTS
    ]
    if still_present:
        print(json.dumps({
            "ok": False,
            "reason": "still_present_after_remove:" + ",".join(os.path.basename(p) for p in still_present),
        }))
        return 1

    print(json.dumps({"ok": True}, separators=(",", ":")))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "reason": "usage"}))
        return 2
    subcommand, rest = sys.argv[1], sys.argv[2:]
    if subcommand == "path-state":
        return cmd_path_state(rest)
    if subcommand == "inspect":
        return cmd_inspect(rest)
    if subcommand == "backup":
        return cmd_backup(rest)
    if subcommand == "remove":
        return cmd_remove(rest)
    print(json.dumps({"ok": False, "reason": "unknown_subcommand"}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
