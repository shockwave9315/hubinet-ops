from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    config_path: Path
    db_path: Path
    api_token: str

    @property
    def api(self) -> dict[str, Any]:
        return self.raw.get("api", {})

    @property
    def executor(self) -> dict[str, Any]:
        return self.raw.get("executor", {})

    @property
    def scheduler(self) -> dict[str, Any]:
        return self.raw.get("scheduler", {})

    @property
    def home_assistant(self) -> dict[str, Any]:
        return self.raw.get("home_assistant", {})

    @property
    def containers(self) -> dict[int, dict[str, Any]]:
        source = self.raw.get("containers", {})
        return {int(k): dict(v) for k, v in source.items()}


def load_settings() -> Settings:
    config_path = Path(os.environ.get("HUBINET_OPS_CONFIG", "/etc/hubinet-ops/config.yaml"))
    db_path = Path(os.environ.get("HUBINET_OPS_DB", "/var/lib/hubinet-ops/ops.db"))
    api_token = os.environ.get("HUBINET_OPS_API_TOKEN", "").strip()

    if not config_path.exists():
        raise RuntimeError(f"Config file not found: {config_path}")
    if len(api_token) < 32:
        raise RuntimeError("HUBINET_OPS_API_TOKEN must contain at least 32 characters")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RuntimeError("Top-level YAML config must be an object")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(raw=raw, config_path=config_path, db_path=db_path, api_token=api_token)
