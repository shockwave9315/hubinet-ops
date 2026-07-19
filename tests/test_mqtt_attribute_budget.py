from __future__ import annotations

import inspect
import json

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
    assert payload["docker"] == {"required_healthy": 3, "required_total": 3}
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


def test_core_mqtt_uses_the_0_2_3_byte_budget() -> None:
    from app import mqtt

    source = inspect.getsource(mqtt.MqttTelemetry.publish_container_state)

    assert mqtt.VERSION == "0.2.3"
    assert mqtt.bounded_state is bounded_state
    assert "bounded_state(state)" in source
    assert "_bounded_state" not in source
