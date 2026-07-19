#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP_ROOT="$(mktemp -d)"
FAKE_BIN="$TMP_ROOT/bin"
LOG="$TMP_ROOT/pct.log"
CT_ROOT="$TMP_ROOT/ct"
mkdir -p "$FAKE_BIN" "$CT_ROOT/usr/local/sbin" "$CT_ROOT/etc"
trap '/usr/bin/rm -rf "$TMP_ROOT"' EXIT
export HUBINET_OPS_TEST_MODE=1
export HUBINET_OPS_MANAGED_TEST_LOG="$LOG"
export HUBINET_OPS_MANAGED_TEST_ROOT="$CT_ROOT"
export PATH="$FAKE_BIN:$PATH"

printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN/python3"
chmod +x "$FAKE_BIN/python3"

cat > "$FAKE_BIN/pct" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '<%s>' "$@" >> "$HUBINET_OPS_MANAGED_TEST_LOG"
printf '\n' >> "$HUBINET_OPS_MANAGED_TEST_LOG"
case "${1:-}" in
  status)
    printf 'status: %s\n' "${MANAGED_TEST_STATUS:-stopped}"
    ;;
  mount)
    if [[ ${MANAGED_TEST_MOUNT_OUTPUT:-valid} == malformed ]]; then
      printf 'CT mounted but path unavailable\n'
    else
      printf "mounted on '%s'\n" "$HUBINET_OPS_MANAGED_TEST_ROOT"
    fi
    ;;
  unmount)
    ;;
  exec)
    if [[ " ${*:3} " == *' test -e '* ]]; then
      [[ ${MANAGED_TEST_EXISTS:-yes} == yes ]]
    fi
    ;;
  pull)
    if [[ ${MANAGED_TEST_PULL_FAIL:-no} == yes ]]; then
      exit 1
    fi
    printf 'original\n' > "$4"
    ;;
  push)
    ;;
esac
SH
chmod +x "$FAKE_BIN/pct"

cat > "$FAKE_BIN/install" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${MANAGED_TEST_SIGNAL:-} == TERM && "${*: -1}" == *'.hubinet-maint.hubinet-ops-new'* ]]; then
  kill -TERM "$PPID"
  sleep 1
fi
if [[ ${MANAGED_TEST_SIGNAL:-} == INT && "${*: -1}" == *'.hubinet-maint.hubinet-ops-new'* ]]; then
  kill -INT "$PPID"
  sleep 1
fi
if [[ ${MANAGED_TEST_INSTALL_FAIL:-no} == yes && "${*: -1}" == *'.hubinet-maint.hubinet-ops-new'* ]]; then
  exit 1
fi
exec /usr/bin/install "$@"
SH
chmod +x "$FAKE_BIN/install"

reset_ct() {
  : > "$LOG"
  /usr/bin/rm -rf "$CT_ROOT/usr" "$CT_ROOT/etc"
  mkdir -p "$CT_ROOT/usr/local/sbin" "$CT_ROOT/etc"
  printf 'executor-before\n' > "$CT_ROOT/usr/local/sbin/hubinet-maint"
  printf 'config-before\n' > "$CT_ROOT/etc/hubinet-maint.json"
}

assert_unchanged_and_unmounted() {
  grep -Fxq 'executor-before' "$CT_ROOT/usr/local/sbin/hubinet-maint"
  grep -Fxq 'config-before' "$CT_ROOT/etc/hubinet-maint.json"
  grep -Fq '<unmount><101>' "$LOG"
}

reset_ct
export MANAGED_TEST_STATUS=stopped MANAGED_TEST_MOUNT_OUTPUT=malformed
if bash "$ROOT/deploy/managed/install-managed.sh" 101; then
  echo 'Malformed mount output unexpectedly succeeded' >&2
  exit 1
fi
assert_unchanged_and_unmounted

reset_ct
export MANAGED_TEST_MOUNT_OUTPUT=valid MANAGED_TEST_INSTALL_FAIL=yes
if bash "$ROOT/deploy/managed/install-managed.sh" 101; then
  echo 'Failure after valid mount unexpectedly succeeded' >&2
  exit 1
fi
unset MANAGED_TEST_INSTALL_FAIL
assert_unchanged_and_unmounted

for signal in TERM INT; do
  reset_ct
  export MANAGED_TEST_SIGNAL="$signal"
  if bash "$ROOT/deploy/managed/install-managed.sh" 101; then
    echo "$signal during mounted operation unexpectedly succeeded" >&2
    exit 1
  fi
  unset MANAGED_TEST_SIGNAL
  assert_unchanged_and_unmounted
done

# Confirmed absence creates no pull requirement and installation may proceed.
: > "$LOG"
export MANAGED_TEST_STATUS=running MANAGED_TEST_EXISTS=no
bash "$ROOT/deploy/managed/install-managed.sh" 101
if grep -Fq '<pull>' "$LOG"; then
  echo 'Absent managed file was unexpectedly pulled' >&2
  exit 1
fi
grep -Fq '<push><101>' "$LOG"

# Existing files must both be pulled successfully before any mutation starts.
: > "$LOG"
export MANAGED_TEST_EXISTS=yes MANAGED_TEST_PULL_FAIL=no
bash "$ROOT/deploy/managed/install-managed.sh" 101
[[ "$(grep -Fc '<pull><101>' "$LOG")" -eq 2 ]]

: > "$LOG"
export MANAGED_TEST_PULL_FAIL=yes
if bash "$ROOT/deploy/managed/install-managed.sh" 101; then
  echo 'Failed pull of an existing file unexpectedly succeeded' >&2
  exit 1
fi
if grep -Fq '<push><101>' "$LOG"; then
  echo 'Managed files were modified after an incomplete backup' >&2
  exit 1
fi
if grep -Fq '<rm><-f></usr/local/sbin/hubinet-maint>' "$LOG"; then
  echo 'Original executor was removed after an incomplete backup' >&2
  exit 1
fi

echo '0.3.0 managed mount and backup safety smoke passed'
