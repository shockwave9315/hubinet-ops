from __future__ import annotations

from pathlib import Path

path = Path("deploy/managed/hubinet-maint")
text = path.read_text(encoding="utf-8")

old = '''DOCKER_PACKAGES = {
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
}
'''
new = '''DOCKER_PACKAGES = {
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
}

# APT/dpkg progress parsing relies on the stable English machine output.
os.environ.setdefault("LC_ALL", "C.UTF-8")
os.environ.setdefault("LANG", "C.UTF-8")
'''
if text.count(old) != 1:
    raise SystemExit("Missing locale insertion target")
text = text.replace(old, new)

old = '''    process.wait(timeout=30)
    if timed_out.is_set():
'''
new = '''    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        stream_result(
            False,
            data={"stdout_tail": tail},
            error="apt-get did not exit after termination and was force-killed",
            rc=1,
        )
    if timed_out.is_set():
'''
if text.count(old) != 1:
    raise SystemExit("Missing process wait target")
text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
