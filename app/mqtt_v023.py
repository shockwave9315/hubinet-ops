from __future__ import annotations

from typing import Any

from . import mqtt as legacy_mqtt
from .mqtt_budget import bounded_state

VERSION = "0.2.3"
legacy_mqtt.VERSION = VERSION


class MqttTelemetry(legacy_mqtt.MqttTelemetry):
    """0.2.3 transport preserving the 0.2.2 Discovery/entity contract."""

    def publish_container_state(
        self,
        vmid: int,
        state: dict[str, Any],
        *,
        force: bool = False,
    ) -> None:
        self._publish_json(
            f"{self.base_topic}/ct/{int(vmid)}/state",
            bounded_state(state),
            retain=self.retain_state,
            force=force,
        )
