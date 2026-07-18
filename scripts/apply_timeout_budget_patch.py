from __future__ import annotations

from pathlib import Path

path = Path("app/service.py")
text = path.read_text(encoding="utf-8")
replacements = {
    'self.executor.run("scan", vmid, timeout=300)': 'self.executor.run("scan", vmid, timeout=700)',
    'self._execute("preflight", vmid, 300, emit)': 'self._execute("preflight", vmid, 700, emit)',
    'self._execute("update", vmid, 3900, emit)': 'self._execute("update", vmid, 4500, emit)',
    'self.executor.run("scan", vmid, timeout=300)': 'self.executor.run("scan", vmid, timeout=700)',
}
for old, new in replacements.items():
    count = text.count(old)
    if count < 1:
        raise SystemExit(f"Missing timeout target: {old}")
    text = text.replace(old, new)
path.write_text(text, encoding="utf-8")
