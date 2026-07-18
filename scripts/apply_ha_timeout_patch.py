from __future__ import annotations

from pathlib import Path

package = Path("home-assistant/packages/hubinet_ops.yaml")
text = package.read_text(encoding="utf-8")
for old, new in {
    "    timeout: 360\n\n  hubinet_ops_refresh_container:": "    timeout: 780\n\n  hubinet_ops_refresh_container:",
    "    timeout: 120\n\n  hubinet_ops_retry_healthcheck:": "    timeout: 180\n\n  hubinet_ops_retry_healthcheck:",
    "    timeout: 360\n\n  hubinet_ops_rollback:": "    timeout: 600\n\n  hubinet_ops_rollback:",
    "    timeout: 1500\n\n  hubinet_ops_approve:": "    timeout: 1800\n\n  hubinet_ops_approve:",
}.items():
    if text.count(old) != 1:
        raise SystemExit(f"Missing HA timeout target: {old!r}")
    text = text.replace(old, new)
package.write_text(text, encoding="utf-8")

config = Path("config/config.example.yaml")
text = config.read_text(encoding="utf-8")
old = "  command_timeout_seconds: 3900\n"
new = "  command_timeout_seconds: 4800\n"
if text.count(old) != 1:
    raise SystemExit("Missing executor timeout target")
config.write_text(text.replace(old, new), encoding="utf-8")
