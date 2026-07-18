from __future__ import annotations

from pathlib import Path

path = Path("deploy/upgrade-0.2.1-from-pve.sh")
text = path.read_text(encoding="utf-8")

replacements = [
    (
        '''restore_all() {\n  local rc=$?\n''',
        '''restore_all() {\n  local rc="${1:-$?}"\n  [[ "$rc" -ne 0 ]] || rc=1\n''',
    ),
    (
        '''trap restore_all ERR INT TERM\ntrap 'rm -f "$ARCHIVE"' EXIT\n''',
        '''trap 'restore_all $?' ERR\ntrap 'restore_all 130' INT\ntrap 'restore_all 143' TERM\ntrap 'rm -f "$ARCHIVE"' EXIT\n''',
    ),
    (
        '''pct exec "$CT106_VMID" -- python3 - <<'PY'\nimport json\nfrom pathlib import Path\n\npath = Path("/etc/hubinet-maint.json")\ndata = json.loads(path.read_text(encoding="utf-8"))\ndata.setdefault("repair_actions", ["restart_services", "restart_required_containers"])\npath.write_text(json.dumps(data, indent=2) + "\\n", encoding="utf-8")\nPY\n''',
        '''pct exec "$CT106_VMID" -- bash -s <<'REMOTE_UPDATE_PROFILE'\nset -Eeuo pipefail\npython3 - <<'PY'\nimport json\nfrom pathlib import Path\n\npath = Path("/etc/hubinet-maint.json")\ndata = json.loads(path.read_text(encoding="utf-8"))\ndata.setdefault("repair_actions", ["restart_services", "restart_required_containers"])\npath.write_text(json.dumps(data, indent=2) + "\\n", encoding="utf-8")\nPY\nREMOTE_UPDATE_PROFILE\n''',
    ),
]

for old, new in replacements:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"Expected exactly one patch target, found {count}")
    text = text.replace(old, new)

path.write_text(text, encoding="utf-8")
