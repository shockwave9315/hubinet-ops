from __future__ import annotations

import inspect
import json

import pytest

from app.mqtt_budget import HA_ATTRIBUTE_BUDGET_BYTES, bounded_state


def _encoded_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def test_large_container_attributes_fit_home_assistant_recorder_budget() -> None:
    state = {
        "vmid": 106,
        "health_status": "healthy",
        "health_score": 100,
        "lxc_status": "running",
        "update_status": "update_available",
        "operation_status": "waiting_approval",
        "job_stage": "idle",
        "job_progress": 0,
        "pending_updates": 250,
        "risk": "high",
        "active_plan_id": "a" * 32,
        "active_job_id": None,
        "disk": {"used_percent": 35.1, "free_mb": 5951, "raw": "d" * 20_000},
        "memory": {"used_percent": 9.1},
        "docker": {
            "required_healthy": 3,
            "required_total": 3,
            "containers": [{"name": "container-" + "x" * 2_000} for _ in range(100)],
        },
        "updates": {
            "pending_count": 250,
            "fingerprint": "f" * 64,
            "packages": [
                {
                    "name": f"pakiet-{index}-" + "ą" * 1_000,
                    "current": "1" * 1_000,
                    "target": "2" * 1_000,
                }
                for index in range(250)
            ],
        },
        "recent_job_events": [
            {
                "created_at": "2026-07-19T07:28:15Z",
                "stage": "updating",
                "progress": index,
                "level": "info",
                "message": "zdarzenie-" + "ż" * 2_000,
            }
            for index in range(60)
        ],
        "failed_units": ["unit-" + "u" * 2_000 for _ in range(100)],
        "ip_addresses": ["2001:db8::" + "1" * 200 for _ in range(40)],
        "last_error": "error-" + "e" * 10_000,
        "last_job_event": {
            "created_at": "2026-07-19T07:28:39Z",
            "stage": "updating",
            "progress": 49,
            "message": "latest-" + "z" * 5_000,
        },
    }

    payload = bounded_state(state)
    metadata = payload["attribute_payload"]

    assert _encoded_size(payload) <= HA_ATTRIBUTE_BUDGET_BYTES
    assert payload["health_status"] == "healthy"
    assert payload["active_plan_id"] == "a" * 32
    assert payload["docker"]["required_healthy"] == 3
    assert payload["docker"]["required_total"] == 3
    assert 0 < len(payload["docker"]["containers"]) <= 10
    assert metadata["budget_bytes"] == HA_ATTRIBUTE_BUDGET_BYTES
    assert metadata["packages_total"] == 250
    assert metadata["events_total"] == 60
    assert metadata["packages_visible"] == len(payload["updates"]["packages"])
    assert metadata["events_visible"] == len(payload["recent_job_events"])
    assert metadata["packages_visible"] > 0
    assert metadata["events_visible"] > 0
    assert metadata["truncated"] is True
    assert payload["recent_job_events"][-1]["progress"] == 59


def test_small_items_keep_existing_200_package_and_50_event_caps() -> None:
    payload = bounded_state(
        {
            "updates": {"packages": [{"name": str(index)} for index in range(200)]},
            "recent_job_events": [
                {"progress": index, "message": str(index)} for index in range(50)
            ],
        }
    )

    assert _encoded_size(payload) <= HA_ATTRIBUTE_BUDGET_BYTES
    assert len(payload["updates"]["packages"]) == 200
    assert len(payload["recent_job_events"]) == 50
    assert payload["attribute_payload"]["packages_total"] == 200
    assert payload["attribute_payload"]["events_total"] == 50
    assert payload["attribute_payload"]["truncated"] is False


def test_malformed_collection_shapes_do_not_break_publication() -> None:
    payload = bounded_state(
        {
            "updates": "unexpected",
            "recent_job_events": {"message": "single event"},
            "failed_units": "none",
            "ip_addresses": 1234,
            "disk": "unexpected",
            "memory": None,
            "docker": 42,
        }
    )

    assert _encoded_size(payload) <= HA_ATTRIBUTE_BUDGET_BYTES
    assert payload["updates"]["packages"] == []
    assert payload["recent_job_events"] == [{"message": "single event"}]
    assert payload["failed_units"] == ["none"]
    assert payload["ip_addresses"] == ["1234"]
    assert payload["disk"] == {}
    assert payload["memory"] == {}
    assert payload["docker"] == {}


def test_core_mqtt_uses_the_bounded_resource_payloads_in_0_4_0() -> None:
    from app import mqtt

    source = inspect.getsource(mqtt.MqttTelemetry.publish_container_state)

    assert mqtt.VERSION == "0.4.0"
    assert mqtt.bounded_state is bounded_state
    assert "publish_resource_state" in source
    assert "_bounded_state" not in source


def test_unknown_pending_count_survives_retained_mqtt_budgeting() -> None:
    payload = bounded_state(
        {
            "update_status": "unknown",
            "pending_updates": None,
            "packages_remaining_count": None,
            "updates": {"pending_count": None, "packages": []},
        }
    )

    assert payload["update_status"] == "unknown"
    assert payload["pending_updates"] is None
    assert payload["packages_remaining_count"] is None
    assert payload["updates"]["pending_count"] is None


def test_resource_scalars_survive_unicode_package_event_and_docker_truncation() -> None:
    capabilities = {
        "refresh": True,
        "scan": True,
        "approve": True,
        "reject": True,
        "retry_healthcheck": True,
        "rollback": True,
        "start": True,
        "shutdown": True,
        "reboot": True,
    }
    payload = bounded_state(
        {
            "vmid": 106,
            "health_status": "healthy",
            "lxc_status": "running",
            "operator_capabilities": capabilities,
            "lifecycle_action": "reboot",
            "lifecycle_status": "success",
            "lifecycle_started_at": "2026-07-19T12:00:00+00:00",
            "lifecycle_finished_at": "2026-07-19T12:00:05+00:00",
            "expected_lxc_status": "running",
            "intentional_shutdown": False,
            "lifecycle_health_pending": True,
            "verification_status": "warning",
            "last_verification": "2026-07-19T12:01:00+00:00",
            "apt_check_ok": True,
            "dpkg_audit_ok": True,
            "reboot_required": True,
            "packages_updated_count": 80,
            "packages_remaining_count": 2,
            "docker_required_healthy": 3,
            "docker_required_total": 3,
            "recovery_scan_enabled": True,
            "recovery_scan_status": "completed",
            "last_recovery_scan": "2026-07-19T11:00:00+00:00",
            "last_recovery_scan_result": "existing_plan",
            "last_terminal_at": "2026-07-19T12:01:00+00:00",
            "docker": {
                "required_healthy": 3,
                "required_total": 3,
                "containers": [{"name": "żółć-" + "界" * 2000} for _ in range(20)],
            },
            "updates": {
                "pending_count": 200,
                "packages": [
                    {
                        "name": f"pakiet-{index}-" + "ą界" * 1000,
                        "current": "wersja-" + "ż" * 500,
                        "target": "wersja-" + "ź" * 500,
                    }
                    for index in range(200)
                ],
            },
            "recent_job_events": [
                {
                    "stage": "verifying",
                    "progress": index,
                    "message": "zdarzenie-" + "ę界" * 1000,
                }
                for index in range(50)
            ],
        }
    )

    assert _encoded_size(payload) <= 10_000
    assert payload["operator_capabilities"] == capabilities
    assert payload["lifecycle_action"] == "reboot"
    assert payload["lifecycle_status"] == "success"
    assert payload["expected_lxc_status"] == "running"
    assert payload["intentional_shutdown"] is False
    assert payload["lifecycle_health_pending"] is True
    assert payload["verification_status"] == "warning"
    assert payload["apt_check_ok"] is True
    assert payload["dpkg_audit_ok"] is True
    assert payload["packages_remaining_count"] == 2
    assert payload["recovery_scan_status"] == "completed"
    assert payload["last_terminal_at"] == "2026-07-19T12:01:00+00:00"


def test_resource_payload_redacts_self_journal_and_never_exposes_secret_fields() -> None:
    payload = bounded_state(
        {
            "vmid": 110,
            "resource_type": "lxc",
            "adapter": "agent_self",
            "recent_warnings": [
                "Authorization: Bearer top-secret",
                "mqtt_password=also-secret",
            ],
            "api_token": "raw-secret",
            "cpu": {"cores": 2, "load_1m": 0.25},
        }
    )
    raw = json.dumps(payload, ensure_ascii=False)

    assert "top-secret" not in raw
    assert "also-secret" not in raw
    assert "raw-secret" not in raw
    assert payload["cpu"]["load_1m"] == 0.25


@pytest.mark.parametrize(
    "state",
    [
        {
            "resource_type": "qemu",
            "adapter": "haos",
            "disk": {"used_bytes": 11, "total_bytes": 22, "free_bytes": 33},
            "memory": {"used_bytes": 44, "total_bytes": 55, "available_bytes": 66},
            "network": {"in_bytes": 77, "out_bytes": 88},
        },
        {
            "resource_type": "lxc",
            "adapter": "agent_self",
            "disk": {"used_bytes": 11, "total_bytes": 22, "free_bytes": 33},
            "memory": {"used_bytes": 44, "total_bytes": 55, "available_bytes": 66},
        },
        {
            "resource_type": "lxc",
            "adapter": "apt",
            "disk": {
                "used_percent": 25,
                "free_mb": 512,
                "used_bytes": 11,
                "total_bytes": 22,
                "free_bytes": 33,
            },
            "memory": {
                "used_percent": 50,
                "used_bytes": 44,
                "total_bytes": 55,
                "available_bytes": 66,
            },
            "network": {"in_bytes": 77, "out_bytes": 88},
        },
    ],
    ids=("qemu", "agent-self", "apt-lxc"),
)
def test_byte_metrics_survive_when_payload_fits_budget(state: dict) -> None:
    payload = bounded_state(state)

    assert payload["disk"]["used_bytes"] == 11
    assert payload["disk"]["total_bytes"] == 22
    assert payload["disk"]["free_bytes"] == 33
    assert payload["memory"]["used_bytes"] == 44
    assert payload["memory"]["total_bytes"] == 55
    assert payload["memory"]["available_bytes"] == 66
    if "network" in state:
        assert payload["network"] == {"in_bytes": 77, "out_bytes": 88}


def test_large_mixed_payload_keeps_byte_metrics_within_budget() -> None:
    payload = bounded_state(
        {
            "resource_type": "qemu",
            "adapter": "haos",
            "disk": {"used_bytes": 11, "total_bytes": 22, "free_bytes": 33},
            "memory": {"used_bytes": 44, "total_bytes": 55, "available_bytes": 66},
            "network": {"in_bytes": 77, "out_bytes": 88},
            "updates": {
                "packages": [
                    {"name": f"package-{index}-" + "x" * 500}
                    for index in range(200)
                ]
            },
            "recent_job_events": [
                {"message": f"event-{index}-" + "y" * 500}
                for index in range(50)
            ],
        }
    )

    assert _encoded_size(payload) <= HA_ATTRIBUTE_BUDGET_BYTES
    assert payload["disk"]["used_bytes"] == 11
    assert payload["memory"]["available_bytes"] == 66
    assert payload["network"] == {"in_bytes": 77, "out_bytes": 88}
