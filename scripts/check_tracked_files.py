from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


def main() -> int:
    tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
    forbidden: list[str] = []
    for name in tracked:
        path = PurePosixPath(name)
        lower = path.name.lower()
        if lower in {".env", "config.yaml", "secrets.yaml", "authorized_keys", "ssh_known_hosts"}:
            forbidden.append(name)
        if lower.endswith((".db", ".sqlite", ".sqlite3", ".key", ".pem", ".log", ".bak")):
            forbidden.append(name)
        if lower.startswith("id_ed25519") or any(part in {"data", "state", "logs", "backups"} for part in path.parts):
            forbidden.append(name)
    if forbidden:
        print("Forbidden runtime files are tracked:", *sorted(set(forbidden)), sep="\n- ")
        return 1
    print(f"Tracked-file validation: {len(tracked)} paths checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
