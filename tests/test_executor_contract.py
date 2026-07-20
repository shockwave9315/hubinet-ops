from __future__ import annotations

import hashlib
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

from app.contracts import (
    EXECUTOR_PROTOCOL_VERSION,
    EXECUTOR_VERSION,
    REQUIRED_APT_ACTIONS,
    evaluate_executor_contract,
    parse_owned_snapshot_name,
)
from app.state import normalize_state

ROOT = Path(__file__).parents[1]
EXECUTOR = ROOT / "deploy" / "managed" / "hubinet-maint"


def _executor_namespace(monkeypatch) -> dict:
    fake_fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_NB=2,
        LOCK_UN=4,
        lockf=lambda *args: None,
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    return runpy.run_path(str(EXECUTOR))


def test_managed_executor_capabilities_are_hashed_and_complete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = tmp_path / "hubinet-maint.json"
    profile.write_text(
        json.dumps(
            {
                "services": ["example.service"],
                "health_urls": [],
                "min_free_mb": 512,
                "ignore_failed_units": [],
                "repair_actions": [],
                "docker": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    namespace = _executor_namespace(monkeypatch)
    namespace["capabilities"].__globals__["CONFIG_PATH"] = profile

    payload = namespace["capabilities"]()

    assert payload["version"] == "0.4.0"
    assert payload["protocol_version"] == 1
    assert set(payload["supported_actions"]) == REQUIRED_APT_ACTIONS
    assert payload["executor_sha256"] == hashlib.sha256(EXECUTOR.read_bytes()).hexdigest()
    assert payload["profile_sha256"] == hashlib.sha256(profile.read_bytes()).hexdigest()
    assert payload["profile_validation_status"] == "valid"


def test_profile_without_health_contract_is_reported_explicitly(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = tmp_path / "hubinet-maint.json"
    profile.write_text(
        '{"services":[],"health_urls":[],"docker":{"enabled":false}}',
        encoding="utf-8",
    )
    namespace = _executor_namespace(monkeypatch)
    namespace["capabilities"].__globals__["CONFIG_PATH"] = profile

    assert namespace["capabilities"]()["profile_validation_status"] == (
        "insufficient_health_contract"
    )


def test_legacy_executor_without_verify_is_incompatible() -> None:
    expected_executor = "a" * 64
    expected_profile = "b" * 64
    compatibility = evaluate_executor_contract(
        {
            "version": "0.2.1",
            "protocol_version": 1,
            "supported_actions": sorted(REQUIRED_APT_ACTIONS - {"verify"}),
            "executor_sha256": expected_executor,
            "profile_sha256": expected_profile,
            "profile_validation_status": "valid",
        },
        expected_executor_sha256=expected_executor,
        expected_profile_sha256=expected_profile,
    )

    assert compatibility.compatible is False
    assert compatibility.missing_actions == ("verify",)
    assert "version 0.2.1" in "; ".join(compatibility.reasons)


def test_complete_executor_contract_is_compatible() -> None:
    expected_executor = "a" * 64
    expected_profile = "b" * 64
    compatibility = evaluate_executor_contract(
        {
            "version": EXECUTOR_VERSION,
            "protocol_version": EXECUTOR_PROTOCOL_VERSION,
            "supported_actions": sorted(REQUIRED_APT_ACTIONS),
            "executor_sha256": expected_executor,
            "profile_sha256": expected_profile,
            "profile_validation_status": "valid",
        },
        expected_executor_sha256=expected_executor,
        expected_profile_sha256=expected_profile,
    )

    assert compatibility.compatible is True
    assert compatibility.reasons == ()
    assert compatibility.state_fields()["executor_compatible"] is True


def test_executor_state_and_snapshot_name_models_are_bounded() -> None:
    state = normalize_state(
        {
            "executor_version": "0.4.0",
            "executor_protocol_version": 1,
            "executor_compatible": True,
            "executor_missing_actions": ["verify", "x" * 100],
            "profile_validation_status": "valid",
        }
    )

    assert state["executor_compatible"] is True
    assert state["executor_missing_actions"] == ["verify", "x" * 64]
    assert parse_owned_snapshot_name(
        "hubinet-ops-106-manual-20260720T182000Z",
        vmid=106,
    ) == {
        "vmid": "106",
        "kind": "manual",
        "timestamp": "20260720T182000Z",
    }
    assert parse_owned_snapshot_name("foreign-snapshot", vmid=106) is None
