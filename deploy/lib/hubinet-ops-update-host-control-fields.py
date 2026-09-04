#!/usr/bin/env python3
"""Semantic reader for the package-update host-control endpoint fields.

Invoked INSIDE the Hubinet CT (via `pct exec <vmid> -- <venv-python>
<this-file> <mode> <config-path>`), where the CT's own installed backend
virtualenv guarantees the real `PyYAML` dependency `app/inventory_runtime_
config.py` itself requires (`requirements.txt`). Never invoked on the
Proxmox host: PyYAML is not guaranteed present there, and hand-rolling a
second, partial YAML grammar to avoid that dependency is exactly the defect
this script exists to close -- see below.

Why this exists
----------------

`deploy/lib/update-boundaries.sh` used to derive the package-update host-
control endpoint (`host`, `port`, `user`, `known_hosts_path`) by scanning
the installation's OWN YAML configuration as source TEXT with a line-
oriented regex, not by parsing it. An inline comment
(`host: pve.example # primary endpoint`), a single-quoted scalar containing
`#`, or a YAML escape sequence inside a double-quoted scalar was returned
lexically instead of decoded, so the updater could inherit a materially
different string than `app/inventory_runtime_config.parse_r0_runtime_
config` -- the running service's own effective value -- would compute for
the exact same file. The activation block this updater writes is then
serialized correctly, but from the WRONG decoded input.

This script performs the semantic read instead: a real `yaml.safe_load`,
never a second hand-rolled grammar, and reproduces `parse_r0_runtime_
config`'s own default rules for the four `package_scan.host_control`
fields -- never a new default this updater invented.

Two modes
---------

`package_scan` -- the pre-activation read `update_boundaries_activate`
uses to derive the endpoint it inherits into the new `package_update`
block: an explicit `package_scan.host_control.<field>` scalar when
present, or the identical runtime default when it is not (`host` from
`urlsplit(source.pve_endpoint).hostname`, `port` 22, `user` "root",
`known_hosts_path` under `/etc/hubinet-ops/host-control`).

`package_update` -- the post-activation read `update_boundaries_accept_
all` uses to prove the four fields it just wrote round-trip back to the
same values. No defaulting: activation always writes all four explicitly,
so an absent field here is reported as `null`, and the bash caller decides
what an incomplete just-activated block means.

Prints one JSON object to stdout and always exits 0 (the caller decides
what a given "ok": false reason means) unless argv itself is unusable:

  {"ok": true, "host": "...", "port": 22, "user": "...",
   "known_hosts_path": "..."}
  {"ok": false, "reason": "<short-code>", "detail": "<bounded message>"}
"""
from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import urlsplit

import yaml

#: Mirrors app/inventory_runtime_config.py's own `_HOST_CONTROL_DIR` --
#: never a new default, the identical runtime one.
_HOST_CONTROL_DIR = "/etc/hubinet-ops/host-control"

_MODES = ("package_scan", "package_update")


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, separators=(",", ":")))
    return 0


def _mapping(value: Any) -> dict[str, Any]:
    """`None`/absent sections decode to `{}`, exactly like the runtime
    parser's own `raw.get(...) or {}` -- never an error, since an omitted
    section is ordinary, not malformed.
    """

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected a mapping")
    return value


def _read_package_update_fields(root: dict[str, Any]) -> dict[str, Any]:
    host_control = _mapping(_mapping(root.get("package_update")).get("host_control"))
    return {
        "host": host_control.get("host"),
        "port": host_control.get("port"),
        "user": host_control.get("user"),
        "known_hosts_path": host_control.get("known_hosts_path"),
    }


def _read_package_scan_fields(root: dict[str, Any]) -> dict[str, Any]:
    source = _mapping(root.get("source"))
    host_control = _mapping(_mapping(root.get("package_scan")).get("host_control"))

    host = host_control.get("host")
    if host is None:
        pve_endpoint = source.get("pve_endpoint")
        if not isinstance(pve_endpoint, str) or not pve_endpoint:
            raise LookupError("no_host_control_host", "")
        hostname = urlsplit(pve_endpoint).hostname
        if not hostname:
            raise LookupError("pve_endpoint_no_hostname", pve_endpoint)
        host = hostname

    port = host_control.get("port")
    if port is None:
        port = 22

    user = host_control.get("user")
    if user is None:
        user = "root"

    known_hosts_path = host_control.get("known_hosts_path")
    if known_hosts_path is None:
        known_hosts_path = f"{_HOST_CONTROL_DIR}/known_hosts"

    return {
        "host": host,
        "port": port,
        "user": user,
        "known_hosts_path": known_hosts_path,
    }


def read_host_control_fields(mode: str, config_path: str) -> dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    root = _mapping(raw)
    if mode == "package_scan":
        return _read_package_scan_fields(root)
    return _read_package_update_fields(root)


def main(argv: list[str]) -> int:
    arguments = argv[1:]
    if len(arguments) != 2 or arguments[0] not in _MODES:
        print(
            "usage: hubinet-ops-update-host-control-fields.py "
            "<package_scan|package_update> <config-path>",
            file=sys.stderr,
        )
        return 2
    mode, config_path = arguments

    try:
        fields = read_host_control_fields(mode, config_path)
    except FileNotFoundError as exc:
        return _emit(
            {"ok": False, "reason": "config_not_found", "detail": str(exc)[:500]}
        )
    except LookupError as exc:
        reason, detail = exc.args
        return _emit({"ok": False, "reason": reason, "detail": detail})
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        return _emit(
            {"ok": False, "reason": "config_unreadable", "detail": str(exc)[:500]}
        )

    return _emit({"ok": True, **fields})


if __name__ == "__main__":
    sys.exit(main(sys.argv))
