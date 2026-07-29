from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from app.config import Settings
from app.database import Database
from app.service import OpsService
from app.time_utils import parse_utc_timestamp


class RecordingMqtt:
    availability = "online"

    def __init__(self) -> None:
        self.resource_states: list[tuple[int, dict[str, Any]]] = []
        self.agent_states: list[dict[str, Any]] = []

    def set_state_provider(self, provider: Any) -> None:
        self.provider = provider

    def publish_resource_state(self, vmid: int, state: dict[str, Any]) -> None:
        self.resource_states.append((vmid, dict(state)))

    def publish_agent_state(self, state: dict[str, Any]) -> None:
        self.agent_states.append(dict(state))

    def publish_event(self, vmid: int, event: dict[str, Any]) -> None:
        pass

    def publish_job(self, vmid: int, job: dict[str, Any], *, force: bool = False) -> None:
        pass


class InventoryExecutor:
    def run(
        self,
        action: str,
        vmid: int,
        argument: str | None = None,
        timeout: int | None = None,
        on_event: Any = None,
    ) -> dict[str, Any]:
        if action == "capabilities":
            return {"ok": True, "data": {}}
        if action == "status":
            return {
                "ok": True,
                "data": {"runtime_status": "running", "lxc_status": "running"},
            }
        assert action == "inspect"
        return {
            "ok": True,
            "data": {
                "runtime_status": "running",
                "health_status": "healthy",
                "health_score": 100,
            },
        }


def _service(
    tmp_path: Path,
    moment: datetime | None = None,
) -> tuple[OpsService, RecordingMqtt]:
    raw = yaml.safe_load(Path("config/config.example.yaml").read_text(encoding="utf-8"))
    cfg = Settings(
        raw=raw,
        config_path=tmp_path / "config.yaml",
        db_path=tmp_path / "ops.db",
        api_token="t" * 64,
    )
    mqtt = RecordingMqtt()
    moment = moment or datetime(2026, 7, 19, 20, 29, 43, 175219, tzinfo=UTC)
    service = OpsService(
        cfg,
        Database(cfg.db_path),
        InventoryExecutor(),
        mqtt=mqtt,  # type: ignore[arg-type]
        now=lambda: moment,
    )
    return service, mqtt


def test_refresh_all_publishes_eleven_resources_and_one_agent_state(
    tmp_path: Path,
) -> None:
    service, mqtt = _service(tmp_path)
    service._ensure_initial_states()
    mqtt.resource_states.clear()
    mqtt.agent_states.clear()

    refreshed = service.refresh_all(operator=False)

    assert len(refreshed) == 11
    assert [vmid for vmid, _ in mqtt.resource_states] == list(range(100, 111))
    assert len(mqtt.agent_states) == 1
    assert mqtt.agent_states[0]["last_refresh"] == "2026-07-19T20:29:43+00:00"


def test_ensure_initial_states_publishes_agent_once(tmp_path: Path) -> None:
    service, mqtt = _service(tmp_path)

    service._ensure_initial_states()

    assert len(mqtt.resource_states) == 11
    assert len(mqtt.agent_states) == 1


def test_single_refresh_publishes_agent_once(tmp_path: Path) -> None:
    service, mqtt = _service(tmp_path)
    service._ensure_initial_states()
    mqtt.resource_states.clear()
    mqtt.agent_states.clear()

    service.refresh_container(106)

    assert [vmid for vmid, _ in mqtt.resource_states] == [106]
    assert len(mqtt.agent_states) == 1
    assert mqtt.agent_states[0]["last_refresh"] is None


def test_job_event_saves_do_not_repeat_identical_agent_summary(tmp_path: Path) -> None:
    service, mqtt = _service(tmp_path)
    service._ensure_initial_states()
    plan = service.db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="updates",
        risk="low",
        payload={"pending_count": 1},
        ttl_minutes=60,
    )
    approved = service.approve(plan["id"])
    mqtt.agent_states.clear()
    emit = service._emitter(approved["job"])

    for progress in (10, 20, 30):
        emit(
            stage="preflight",
            progress=progress,
            event_type="progress",
            message=f"event {progress}",
        )

    assert mqtt.agent_states == []


def test_active_job_count_publishes_on_job_start_and_finish(tmp_path: Path) -> None:
    service, mqtt = _service(tmp_path)
    service._ensure_initial_states()
    plan = service.db.create_plan(
        vmid=106,
        container_name="weather",
        fingerprint="updates",
        risk="low",
        payload={"pending_count": 1},
        ttl_minutes=60,
    )
    mqtt.agent_states.clear()

    approved = service.approve(plan["id"])
    service._terminal(approved["job"], "success", "success", None)

    assert [state["active_job_count"] for state in mqtt.agent_states] == [1, 0]


def test_agent_last_refresh_is_utc_iso_without_microseconds(tmp_path: Path) -> None:
    service, mqtt = _service(tmp_path)
    service.refresh_all(operator=False)
    value = mqtt.agent_states[-1]["last_refresh"]
    parsed = parse_utc_timestamp(value)

    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed.microsecond == 0
    assert "." not in value


def test_utc_second_timestamp_preserves_aware_utc(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path,
        datetime(2026, 7, 19, 20, 29, 43, 175219, tzinfo=UTC),
    )

    assert service._utc_second_timestamp() == "2026-07-19T20:29:43+00:00"


def test_utc_second_timestamp_converts_aware_offset_to_utc(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path,
        datetime(
            2026,
            7,
            19,
            22,
            29,
            43,
            175219,
            tzinfo=timezone(timedelta(hours=2)),
        ),
    )

    assert service._utc_second_timestamp() == "2026-07-19T20:29:43+00:00"


def test_utc_second_timestamp_treats_naive_datetime_as_utc(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path,
        datetime(2026, 7, 19, 20, 29, 43, 175219),
    )

    value = service._utc_second_timestamp()

    assert value == "2026-07-19T20:29:43+00:00"
    assert "." not in value
