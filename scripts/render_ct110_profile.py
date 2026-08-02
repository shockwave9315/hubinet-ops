#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import yaml


HEALTH_URL_TEMPLATE = "http://127.0.0.1:{api_port}/health"


def api_port_from_config(config: dict[str, Any]) -> int:
    api = config.get("api")
    if not isinstance(api, dict):
        raise ValueError("CT110 configuration must contain an api object")
    port = api.get("port")
    if isinstance(port, bool):
        raise ValueError("CT110 api.port must be an integer between 1 and 65535")
    try:
        parsed = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "CT110 api.port must be an integer between 1 and 65535"
        ) from exc
    if parsed < 1 or parsed > 65535 or str(port) != str(parsed):
        raise ValueError("CT110 api.port must be an integer between 1 and 65535")
    return parsed


def render_profile(
    config: dict[str, Any],
    profile_template: dict[str, Any],
) -> dict[str, Any]:
    if profile_template.get("health_urls") != [HEALTH_URL_TEMPLATE]:
        raise ValueError(
            "CT110 profile template must contain exactly the api.port health URL placeholder"
        )
    rendered = dict(profile_template)
    rendered["health_urls"] = [
        HEALTH_URL_TEMPLATE.format(api_port=api_port_from_config(config))
    ]
    return rendered


def _load_object(path: Path, *, yaml_input: bool) -> dict[str, Any]:
    raw = (
        yaml.safe_load(path.read_text(encoding="utf-8"))
        if yaml_input
        else json.loads(path.read_text(encoding="utf-8"))
    )
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain an object")
    return raw


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render the CT110 managed profile from the migrated agent api.port"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile-template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--print-api-port", action="store_true")
    args = parser.parse_args()

    config = _load_object(args.config, yaml_input=True)
    template = _load_object(args.profile_template, yaml_input=False)
    rendered = render_profile(config, template)
    _atomic_write(
        args.output,
        json.dumps(rendered, sort_keys=True, separators=(",", ":")) + "\n",
    )
    if args.print_api_port:
        print(api_port_from_config(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
