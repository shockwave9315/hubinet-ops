#!/usr/bin/env bash
set -Eeuo pipefail

[[ "${GITHUB_ACTIONS:-}" == true \
  && "${HUBINET_OPS_EPHEMERAL_CI:-0}" == 1 \
  && "${RUNNER_ENVIRONMENT:-}" == github-hosted \
  && "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]] || {
  echo "deployment-smoke sandbox is restricted to controlled ephemeral GitHub CI" >&2
  exit 2
}
[[ "$(uname -s)" == Linux ]] || {
  echo "system deployment-smoke sandbox requires Linux" >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || {
  echo "Docker is required; refusing to run deployment smoke on the host" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_TAG="hubinet-ops-runtime-smoke:${GITHUB_RUN_ID:-local}-$$"
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
  --file "$ROOT/tests/shell/Dockerfile.runtime-smoke" \
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
  --hostname hubinet-runtime-smoke \
  --mount "type=bind,src=$ROOT,dst=/repo,readonly" \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=768m,mode=1777 \
  --tmpfs /workspace:rw,nosuid,nodev,noexec,size=128m,mode=0700,uid=65534,gid=65534 \
  --env HOME=/workspace/home \
  --env TMPDIR=/tmp \
  --env HUBINET_OPS_SYSTEM_SANDBOX=1 \
  --env "HOST_SENTINEL_PATH=$HOST_SENTINEL" \
  --env "HOST_SENTINEL_PID=$HOST_SENTINEL_PID" \
  "$IMAGE_TAG" \
  /bin/bash /repo/tests/shell/runtime_smoke_sandbox_entrypoint.sh
