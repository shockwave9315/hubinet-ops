#!/usr/bin/env bash
set -Eeuo pipefail

# Hermetic execution boundary for tests/test_bootstrap_proxmox_0_5_smoke.py
# (the only test file that actually executes deploy/bootstrap-proxmox-0.5.sh
# as a real subprocess). Restricted to controlled ephemeral GitHub-hosted CI
# runners, exactly like the AGENTS.md-mandated deployment-script sandbox
# model this adapts from tests/shell's previous (legacy-runtime-scoped,
# since-retired) smoke-sandbox infrastructure -- this file is new, generic
# tooling for the 0.5 bootstrap script; it does not reintroduce any 0.4
# runtime/deployment behavior.

[[ "${GITHUB_ACTIONS:-}" == true \
  && "${HUBINET_OPS_EPHEMERAL_CI:-0}" == 1 \
  && "${RUNNER_ENVIRONMENT:-}" == github-hosted \
  && "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] || {
  echo "bootstrap-smoke sandbox is restricted to controlled ephemeral GitHub CI" >&2
  exit 2
}
[[ "$(uname -s)" == Linux ]] || {
  echo "bootstrap-smoke sandbox requires Linux" >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || {
  echo "Docker is required; refusing to run bootstrap smoke tests on the host" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_TAG="hubinet-ops-bootstrap-smoke:${GITHUB_RUN_ID:-local}-$$"
HOST_SENTINEL_DIR="$(mktemp -d)"
HOST_SENTINEL="$HOST_SENTINEL_DIR/host-secret-sentinel"
printf 'host-only-secret-sentinel\n' > "$HOST_SENTINEL"
sleep 300 &
HOST_SENTINEL_PID=$!

cleanup() {
  local rc=$?
  kill "$HOST_SENTINEL_PID" 2>/dev/null || true
  wait "$HOST_SENTINEL_PID" 2>/dev/null || true
  rm -rf -- "$HOST_SENTINEL_DIR"
  docker image rm -f "$IMAGE_TAG" >/dev/null 2>&1 || true
  return "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker build \
  --pull \
  --tag "$IMAGE_TAG" \
  --file "$ROOT/tests/shell/Dockerfile.bootstrap-smoke" \
  "$ROOT/tests/shell"

docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --ipc none \
  --pids-limit 256 \
  --memory 768m \
  --memory-swap 768m \
  --cpus 2 \
  --ulimit nofile=1024:1024 \
  --user 65534:65534 \
  --hostname hubinet-bootstrap-smoke \
  --mount "type=bind,src=$ROOT,dst=/repo,readonly" \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=768m,mode=1777 \
  --tmpfs /workspace:rw,nosuid,nodev,noexec,size=256m,mode=0700,uid=65534,gid=65534 \
  --env HOME=/workspace/home \
  --env TMPDIR=/tmp \
  --env HUBINET_OPS_SYSTEM_SANDBOX=1 \
  --env "HOST_SENTINEL_PATH=$HOST_SENTINEL" \
  --env "HOST_SENTINEL_PID=$HOST_SENTINEL_PID" \
  "$IMAGE_TAG" \
  /bin/bash /repo/tests/shell/bootstrap_smoke_sandbox_entrypoint.sh
