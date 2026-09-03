"""Direct, hermetic unit coverage for the shared authorized_keys atomic
mutation primitive (Family 2 correction pass):
deploy/lib/bootstrap-host-control.sh::_host_control_authorized_keys_add /
_host_control_authorized_keys_remove.

These two functions are pure filesystem operations -- mktemp, cat, awk,
chmod, sync, mv -- with no `pct`/`ssh`/`systemctl`/network call anywhere in
them, so exercising them directly (sourcing the two small library files
into a throwaway bash subprocess against a tmp_path-rooted fake root) is
both faithful and fast: no Docker sandbox is required, unlike the full
deploy/update-proxmox-0.5.sh / deploy/bootstrap-proxmox-0.5.sh smoke
suites, which exercise the same functions end to end through their six
real call sites (see TestBoundaryAuthorizedKeysAtomicity in
test_update_proxmox_0_5_smoke.py and the bootstrap symlink/rollback tests
in test_bootstrap_proxmox_0_5_smoke.py).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMON_SH = REPO_ROOT / "deploy" / "lib" / "bootstrap-common.sh"
HOST_CONTROL_SH = REPO_ROOT / "deploy" / "lib" / "bootstrap-host-control.sh"

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("bash") is None, reason="bash is not available"
)


def _run(tmp_path: Path, script: str, *, env_extra: dict | None = None):
    wrapper = f"""
set -Eeuo pipefail
source '{COMMON_SH}'
source '{HOST_CONTROL_SH}'
{script}
"""
    env = dict(os.environ, HUBINET_OPS_TEST_MODE="1", **(env_extra or {}))
    return subprocess.run(
        ["bash", "-c", wrapper],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _line(number: int) -> str:
    return (
        f'command="/usr/local/libexec/hubinet-package-mutation-{number}",'
        f"no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty "
        f"ssh-ed25519 AAAAmarker{number} hubinet-ops-marker-{number}"
    )


class TestAddRemoveCyclePreservesUnrelatedContent:
    def test_f2a_unrelated_operator_and_scan_keys_survive_byte_exact(
        self, tmp_path
    ):
        """F2-A. Add five entries, then remove them all; every unrelated
        line -- an operator key before AND after the Hubinet block, plus
        the package-scan boundary's own entry -- must come back byte-exact.
        """

        authorized = tmp_path / "authorized_keys"
        operator1 = "ssh-ed25519 AAAAoperator1 root@laptop\n"
        scan_entry = (
            'command="/usr/local/libexec/hubinet-package-scan-helper-x",'
            "no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty "
            "ssh-ed25519 AAAAscan hubinet-ops-package-scan-vmid-110-x\n"
        )
        operator2 = "ssh-ed25519 AAAAoperator2 root@desktop\n"
        original = operator1 + scan_entry + operator2
        authorized.write_text(original, encoding="utf-8")
        authorized.chmod(0o600)

        add_calls = "\n".join(
            f'_host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-{i}" \'{_line(i)}\''
            for i in range(5)
        )
        remove_calls = "\n".join(
            f'_host_control_authorized_keys_remove "$AUTHKEYS" "hubinet-ops-marker-{i}"'
            for i in range(5)
        )
        script = f"""
AUTHKEYS="{authorized}"
{add_calls}
grep -c 'hubinet-ops-marker-' "$AUTHKEYS"
{remove_calls}
"""
        result = _run(tmp_path, script)
        assert result.returncode == 0, result.stderr
        assert "5" in result.stdout.splitlines()
        assert authorized.read_text(encoding="utf-8") == original
        assert authorized.stat().st_mode & 0o777 == 0o600

    def test_f2b_missing_final_newline_gets_exactly_one_separator(
        self, tmp_path
    ):
        """F2-B. A hand-managed key with NO trailing newline: the new entry
        must start on its own line, the operator key must remain valid and
        unchanged, and removing the Hubinet entry must restore exactly the
        original (newline-normalized) operator line -- never a
        concatenated, corrupted line.
        """

        authorized = tmp_path / "authorized_keys"
        operator_key = "ssh-ed25519 AAAAoperatorNoNewline root@laptop"
        authorized.write_text(operator_key, encoding="utf-8")  # no trailing \n

        script = f"""
AUTHKEYS="{authorized}"
_host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'
cat "$AUTHKEYS"
"""
        result = _run(tmp_path, script)
        assert result.returncode == 0, result.stderr
        lines = authorized.read_text(encoding="utf-8").splitlines()
        assert lines[0] == operator_key
        assert lines[1] == _line(0)
        assert len(lines) == 2

        remove_script = f"""
AUTHKEYS="{authorized}"
_host_control_authorized_keys_remove "$AUTHKEYS" "hubinet-ops-marker-0"
"""
        result = _run(tmp_path, remove_script)
        assert result.returncode == 0, result.stderr
        # awk's own `print` always terminates the final record: the
        # operator key is preserved, now newline-terminated (a documented,
        # harmless normalization -- never a concatenation with anything
        # else).
        assert authorized.read_text(encoding="utf-8") == operator_key + "\n"


class TestWriteRenameFailureLeavesLiveUntouched:
    def test_f2c_rename_failure_leaves_the_exact_old_file(self, tmp_path):
        authorized = tmp_path / "authorized_keys"
        original = "ssh-ed25519 AAAAoperator root@laptop\n"
        authorized.write_text(original, encoding="utf-8")
        authorized.chmod(0o600)

        script = f"""
AUTHKEYS="{authorized}"
if _host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'; then
  echo UNEXPECTED_SUCCESS
  exit 3
fi
echo "ADD_RC_NONZERO"
"""
        result = _run(
            tmp_path,
            script,
            env_extra={"HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_RENAME": "authorized_keys"},
        )
        assert result.returncode == 0, result.stderr
        assert "ADD_RC_NONZERO" in result.stdout
        # The exact old file, untouched -- no temp file left behind either.
        assert authorized.read_text(encoding="utf-8") == original
        assert authorized.stat().st_mode & 0o777 == 0o600
        leftovers = list(tmp_path.glob("hubinet-ops-authorized-keys.*"))
        assert leftovers == []

    def test_f2c_remove_rename_failure_leaves_the_exact_old_file(
        self, tmp_path
    ):
        authorized = tmp_path / "authorized_keys"
        original = "ssh-ed25519 AAAAoperator root@laptop\n" + _line(0) + "\n"
        authorized.write_text(original, encoding="utf-8")

        script = f"""
AUTHKEYS="{authorized}"
if _host_control_authorized_keys_remove "$AUTHKEYS" "hubinet-ops-marker-0"; then
  echo UNEXPECTED_SUCCESS
  exit 3
fi
echo "REMOVE_RC_NONZERO"
"""
        result = _run(
            tmp_path,
            script,
            env_extra={"HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_RENAME": "authorized_keys"},
        )
        assert result.returncode == 0, result.stderr
        assert "REMOVE_RC_NONZERO" in result.stdout
        assert authorized.read_text(encoding="utf-8") == original


class TestStagingReadWriteFailuresNeverReachRename:
    """Family 2 correction pass (P1 direct sibling): every byte used to
    construct the staged replacement must be positively read/written
    before the atomic rename may ever be attempted. A read/write failure
    while STAGING (as opposed to the already-covered rename/durability
    failures above) must discard the temp file and leave the live file
    byte-identical -- never a corrupted-but-live rename.
    """

    def _seeded_authorized_keys(self, tmp_path):
        authorized = tmp_path / "authorized_keys"
        operator1 = "ssh-ed25519 AAAAoperator1 root@laptop\n"
        scan_entry = (
            'command="/usr/local/libexec/hubinet-package-scan-helper-x",'
            "no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty "
            "ssh-ed25519 AAAAscan hubinet-ops-package-scan-vmid-110-x\n"
        )
        operator2 = "ssh-ed25519 AAAAoperator2 root@desktop\n"
        original = operator1 + scan_entry + operator2
        authorized.write_text(original, encoding="utf-8")
        return authorized, original

    def test_a_partial_source_copy_failure_leaves_the_live_file_untouched(
        self, tmp_path
    ):
        """A. A REALISTIC partial write (some bytes land in the temp file,
        then the copy fails) must still be discarded and reported as
        failure -- never merely "the temp file happened to stay empty".
        """

        authorized, original = self._seeded_authorized_keys(tmp_path)

        script = f"""
AUTHKEYS="{authorized}"
if _host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'; then
  echo UNEXPECTED_SUCCESS
  exit 3
fi
echo "ADD_RC_NONZERO"
"""
        result = _run(
            tmp_path,
            script,
            env_extra={"HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_STAGE": "copy_partial"},
        )
        assert result.returncode == 0, result.stderr
        assert "ADD_RC_NONZERO" in result.stdout
        assert authorized.read_text(encoding="utf-8") == original
        assert "hubinet-ops-marker-0" not in authorized.read_text(encoding="utf-8")
        leftovers = list(tmp_path.glob("hubinet-ops-authorized-keys.*"))
        assert leftovers == []

    def test_b_final_entry_write_failure_leaves_the_live_file_untouched(
        self, tmp_path
    ):
        """B. The source copy succeeds; writing the NEW forced-command
        entry is what fails.
        """

        authorized, original = self._seeded_authorized_keys(tmp_path)

        script = f"""
AUTHKEYS="{authorized}"
if _host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'; then
  echo UNEXPECTED_SUCCESS
  exit 3
fi
echo "ADD_RC_NONZERO"
"""
        result = _run(
            tmp_path,
            script,
            env_extra={"HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_STAGE": "entry_write"},
        )
        assert result.returncode == 0, result.stderr
        assert "ADD_RC_NONZERO" in result.stdout
        assert authorized.read_text(encoding="utf-8") == original
        leftovers = list(tmp_path.glob("hubinet-ops-authorized-keys.*"))
        assert leftovers == []

    def test_c_trailing_byte_read_failure_leaves_the_live_file_untouched(
        self, tmp_path
    ):
        """C. The trailing-newline probe itself fails: UNKNOWN, never
        "already ends in a newline".
        """

        authorized, original = self._seeded_authorized_keys(tmp_path)

        script = f"""
AUTHKEYS="{authorized}"
if _host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'; then
  echo UNEXPECTED_SUCCESS
  exit 3
fi
echo "ADD_RC_NONZERO"
"""
        result = _run(
            tmp_path,
            script,
            env_extra={"HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_STAGE": "trailing_read"},
        )
        assert result.returncode == 0, result.stderr
        assert "ADD_RC_NONZERO" in result.stdout
        assert authorized.read_text(encoding="utf-8") == original
        leftovers = list(tmp_path.glob("hubinet-ops-authorized-keys.*"))
        assert leftovers == []

    def test_d_add_marker_read_failure_is_unknown_not_absent(self, tmp_path):
        """D. `grep -cF`'s own read fails while checking whether the
        marker is already present. Must fail closed -- never silently
        treated as "zero matches" and proceed to append a duplicate.
        """

        authorized, original = self._seeded_authorized_keys(tmp_path)

        script = f"""
AUTHKEYS="{authorized}"
if _host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'; then
  echo UNEXPECTED_SUCCESS
  exit 3
fi
echo "ADD_RC_NONZERO"
"""
        result = _run(
            tmp_path,
            script,
            env_extra={"HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_STAGE": "grep_read"},
        )
        assert result.returncode == 0, result.stderr
        assert "ADD_RC_NONZERO" in result.stdout
        assert authorized.read_text(encoding="utf-8") == original
        assert "hubinet-ops-marker-0" not in authorized.read_text(encoding="utf-8")
        leftovers = list(tmp_path.glob("hubinet-ops-authorized-keys.*"))
        assert leftovers == []

    def test_e_remove_marker_read_failure_is_unknown_not_absent(self, tmp_path):
        """E. `grep -qF`'s own read fails while checking whether the
        marker is present to remove. Must fail closed -- never silently
        treated as "already absent" (which would report false success
        without ever having proven the entry is gone).
        """

        authorized = tmp_path / "authorized_keys"
        unrelated = "ssh-ed25519 AAAAoperator root@laptop\n"
        original = unrelated + _line(0) + "\n"
        authorized.write_text(original, encoding="utf-8")

        script = f"""
AUTHKEYS="{authorized}"
if _host_control_authorized_keys_remove "$AUTHKEYS" "hubinet-ops-marker-0"; then
  echo UNEXPECTED_SUCCESS
  exit 3
fi
echo "REMOVE_RC_NONZERO"
"""
        result = _run(
            tmp_path,
            script,
            env_extra={"HUBINET_OPS_TEST_FAIL_AUTHORIZED_KEYS_STAGE": "grep_read_remove"},
        )
        assert result.returncode == 0, result.stderr
        assert "REMOVE_RC_NONZERO" in result.stdout
        # Untouched -- the marker line is still there because removal was
        # never actually proven, not silently accepted as already absent.
        assert authorized.read_text(encoding="utf-8") == original
        leftovers = list(tmp_path.glob("hubinet-ops-authorized-keys.*"))
        assert leftovers == []


class TestDurabilityFailureAfterRenameIsRetrySafe:
    def test_f2d_add_retry_after_barrier_failure_never_duplicates(
        self, tmp_path
    ):
        """The exact fence-release class this correction pass explicitly
        must not reintroduce: rename succeeds, the FOLLOWING durability
        barrier fails, and a later retry must re-prove the barrier rather
        than silently reporting success OR corrupting/duplicating content.
        """

        authorized = tmp_path / "authorized_keys"
        authorized.write_text("ssh-ed25519 AAAAoperator root@laptop\n", encoding="utf-8")
        fail_env = {"HUBINET_OPS_TEST_FAIL_HOST_SYNC": "authorized_keys"}

        script = f"""
AUTHKEYS="{authorized}"
rc=0
_host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}' || rc=$?
echo "RC=$rc"
"""
        first = _run(tmp_path, script, env_extra=fail_env)
        assert "RC=1" in first.stdout
        # The rename itself already succeeded -- the entry is genuinely
        # live -- even though the barrier that must follow it before
        # success may be CLAIMED has not yet been proven.
        assert authorized.read_text(encoding="utf-8").count(_line(0)) == 1

        second = _run(tmp_path, script, env_extra=fail_env)
        assert "RC=1" in second.stdout
        # Retried, not duplicated: the idempotent-replay branch re-proves
        # the barrier rather than re-appending.
        assert authorized.read_text(encoding="utf-8").count(_line(0)) == 1

        third = _run(tmp_path, script)  # fault cleared
        assert "RC=0" in third.stdout
        assert authorized.read_text(encoding="utf-8").count(_line(0)) == 1

    def test_f2d_remove_retry_after_barrier_failure_stays_idempotent(
        self, tmp_path
    ):
        authorized = tmp_path / "authorized_keys"
        unrelated = "ssh-ed25519 AAAAoperator root@laptop\n"
        authorized.write_text(unrelated + _line(0) + "\n", encoding="utf-8")
        fail_env = {"HUBINET_OPS_TEST_FAIL_HOST_SYNC": "authorized_keys"}

        script = f"""
AUTHKEYS="{authorized}"
rc=0
_host_control_authorized_keys_remove "$AUTHKEYS" "hubinet-ops-marker-0" || rc=$?
echo "RC=$rc"
"""
        first = _run(tmp_path, script, env_extra=fail_env)
        assert "RC=1" in first.stdout
        # The rename already succeeded -- the entry is genuinely gone --
        # even though the barrier has not yet been proven.
        assert _line(0) not in authorized.read_text(encoding="utf-8")
        assert authorized.read_text(encoding="utf-8") == unrelated

        second = _run(tmp_path, script, env_extra=fail_env)
        assert "RC=1" in second.stdout
        assert authorized.read_text(encoding="utf-8") == unrelated

        third = _run(tmp_path, script)  # fault cleared
        assert "RC=0" in third.stdout
        assert authorized.read_text(encoding="utf-8") == unrelated


class TestMissingAndMalformedLivePath:
    def test_add_creates_a_fresh_file_when_none_exists(self, tmp_path):
        authorized = tmp_path / "authorized_keys"
        script = f"""
AUTHKEYS="{authorized}"
_host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'
"""
        result = _run(tmp_path, script)
        assert result.returncode == 0, result.stderr
        assert authorized.read_text(encoding="utf-8") == _line(0) + "\n"
        assert authorized.stat().st_mode & 0o777 == 0o600

    def test_remove_on_a_missing_file_is_a_harmless_no_op(self, tmp_path):
        authorized = tmp_path / "authorized_keys"
        script = f"""
AUTHKEYS="{authorized}"
_host_control_authorized_keys_remove "$AUTHKEYS" "hubinet-ops-marker-0"
"""
        result = _run(tmp_path, script)
        assert result.returncode == 0, result.stderr
        assert not authorized.exists()

    def test_add_refuses_a_dangling_symlink(self, tmp_path):
        authorized = tmp_path / "authorized_keys"
        authorized.symlink_to(tmp_path / "does-not-exist")
        script = f"""
AUTHKEYS="{authorized}"
_host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'
"""
        result = _run(tmp_path, script)
        assert result.returncode != 0
        assert "dangling symlink" in result.stderr


class TestSymlinkedLiveFileIsResolvedNotReplaced:
    def test_add_and_remove_stage_and_rename_onto_the_real_target(
        self, tmp_path
    ):
        """The symlink itself (e.g. PVE's own /root/.ssh/authorized_keys ->
        /etc/pve/priv/authorized_keys) must never be replaced by a rename
        -- only its target's content changes.
        """

        real_dir = tmp_path / "real"
        real_dir.mkdir()
        target = real_dir / "authorized_keys"
        target.write_text("ssh-ed25519 AAAAoperator root@laptop\n", encoding="utf-8")
        target.chmod(0o640)
        link = tmp_path / "authorized_keys"
        link.symlink_to(target)

        script = f"""
AUTHKEYS="{link}"
_host_control_authorized_keys_add "$AUTHKEYS" "hubinet-ops-marker-0" '{_line(0)}'
"""
        result = _run(tmp_path, script)
        assert result.returncode == 0, result.stderr
        assert link.is_symlink()
        assert link.resolve() == target
        assert _line(0) in target.read_text(encoding="utf-8")
        assert target.stat().st_mode & 0o777 == 0o640

        remove_script = f"""
AUTHKEYS="{link}"
_host_control_authorized_keys_remove "$AUTHKEYS" "hubinet-ops-marker-0"
"""
        result = _run(tmp_path, remove_script)
        assert result.returncode == 0, result.stderr
        assert link.is_symlink()
        assert link.resolve() == target
        assert target.read_text(encoding="utf-8") == (
            "ssh-ed25519 AAAAoperator root@laptop\n"
        )
