#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  echo "sandbox self-test failed: $*" >&2
  exit 1
}

[[ "${HUBINET_OPS_SYSTEM_SANDBOX:-0}" == 1 ]] || fail "sandbox marker missing"
[[ "$(id -u)" != 0 ]] || fail "process is root"
[[ -w /workspace && -w /tmp ]] || fail "ephemeral workspace is not writable"
mkdir -p "$HOME"
printf 'workspace-write-test\n' > /workspace/write-test
printf 'tmp-write-test\n' > /tmp/write-test

if printf 'unsafe\n' > /repo/.hubinet-sandbox-write-test 2>/dev/null; then
  fail "repository is writable"
fi
if printf 'unsafe\n' > /etc/.hubinet-sandbox-write-test 2>/dev/null; then
  fail "root filesystem is writable"
fi
if printf 'unsafe\n' > /var/tmp/.hubinet-sandbox-write-test 2>/dev/null; then
  fail "non-ephemeral filesystem is writable"
fi
[[ ! -w /dev/shm ]] || fail "shared memory is unexpectedly writable"

[[ ! -e "$HOST_SENTINEL_PATH" ]] || fail "host filesystem sentinel is visible"
[[ ! -e "/proc/$HOST_SENTINEL_PID" ]] || fail "host PID namespace is visible"
for path in \
  /var/run/docker.sock \
  /run/docker.sock \
  /run/podman/podman.sock \
  /etc/hubinet-ops \
  /root/.ssh \
  /home/runner \
  /github
do
  [[ ! -e "$path" ]] || fail "host path is visible: $path"
done
command -v docker >/dev/null 2>&1 && fail "Docker client is visible in sandbox"
command -v podman >/dev/null 2>&1 && fail "Podman client is visible in sandbox"

cap_eff="$(awk '/^CapEff:/ { print $2 }' /proc/self/status)"
[[ "$cap_eff" == 0000000000000000 ]] || fail "effective capabilities are not empty"
no_new_privs="$(awk '/^NoNewPrivs:/ { print $2 }' /proc/self/status)"
[[ "$no_new_privs" == 1 ]] || fail "no-new-privileges is not active"

if bash -c 'exec 3<>/dev/tcp/198.51.100.1/9' 2>/dev/null; then
  fail "Bash /dev/tcp reached a network"
fi
if bash -c 'exec 3<>/dev/udp/198.51.100.1/53; printf probe >&3' 2>/dev/null; then
  fail "Bash /dev/udp reached a network"
fi
python3 - <<'PY'
import socket

probes = (
    (socket.SOCK_STREAM, ("198.51.100.1", 9)),
    (socket.SOCK_DGRAM, ("198.51.100.1", 53)),
)
for kind, address in probes:
    sock = socket.socket(socket.AF_INET, kind)
    sock.settimeout(0.5)
    try:
        sock.connect(address)
        sock.send(b"probe")
    except OSError:
        pass
    else:
        raise SystemExit(f"network probe unexpectedly succeeded: {address}")
    finally:
        sock.close()
try:
    socket.create_connection(("example.invalid", 443), timeout=0.5)
except OSError:
    pass
else:
    raise SystemExit("external DNS/TCP probe unexpectedly succeeded")
PY

echo "sandbox self-test: passed"
/bin/bash /repo/tests/shell/runtime_smoke_0_4_1.sh
/bin/bash /repo/tests/shell/runtime_smoke_ha_0_4_1.sh
