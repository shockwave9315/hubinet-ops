from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .ha_entities import (
    AGENT_ENTITY_SPECS,
    APT_ENTITY_SPECS,
    normalize_resource_identity,
    obsolete_discovery_keys,
    resource_entity_id,
    resource_entity_specs,
    resource_prefix,
)
from .security import sanitize_data, sanitize_text
from .mqtt_budget import bounded_attributes, bounded_state

LOGGER = logging.getLogger("hubinet_ops.mqtt")
VERSION = "0.4.1"


@dataclass(frozen=True)
class PublishItem:
    topic: str
    payload: str
    retain: bool
    force: bool = False


class MqttTelemetry:
    def __init__(
        self,
        config: dict[str, Any] | None,
        resources: dict[int, dict[str, Any]],
        *,
        client_factory: Callable[..., Any] | None = None,
    ):
        self.config = dict(config or {})
        self.resources = resources
        self.containers = resources  # Compatibility attribute for 0.2.x callers.
        self.enabled = bool(self.config.get("enabled", False))
        self.base_topic = str(self.config.get("base_topic", "hubinet/ops")).strip("/") or "hubinet/ops"
        self.discovery_prefix = (
            str(self.config.get("discovery_prefix", "homeassistant")).strip("/") or "homeassistant"
        )
        self.retain_state = bool(self.config.get("retain_state", True))
        self._client_factory = client_factory
        self._client: Any = None
        # Two complete discovery passes must fit during reconnect without
        # evicting retained cleanup messages as the 0.4.x entity contract grows.
        self._queue: queue.Queue[PublishItem | None] = queue.Queue(maxsize=2000)
        self._cache: dict[str, str] = {}
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._publisher = threading.Thread(
            target=self._publisher_loop,
            name="mqtt-publisher",
            daemon=True,
        )
        self._state_provider: Callable[[], tuple[Any, ...]] | None = None
        self._started = False

    def set_state_provider(self, provider: Callable[[], tuple[Any, ...]]) -> None:
        self._state_provider = provider

    @property
    def availability(self) -> str:
        if not self.enabled:
            return "disabled"
        return "online" if self._connected.is_set() else "offline"

    def start(self) -> None:
        if not self.enabled or self._started:
            return
        self._started = True
        try:
            self._client = self._new_client()
            username = str(self.config.get("username", ""))
            if username:
                self._client.username_pw_set(username, str(self.config.get("password", "")))
            self._client.will_set(
                f"{self.base_topic}/agent/availability",
                payload="offline",
                qos=1,
                retain=True,
            )
            self._client.reconnect_delay_set(
                min_delay=max(1, int(self.config.get("reconnect_min_seconds", 2))),
                max_delay=max(2, int(self.config.get("reconnect_max_seconds", 60))),
            )
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._publisher.start()
            self._client.connect_async(
                str(self.config.get("host", "localhost")),
                int(self.config.get("port", 1883)),
                int(self.config.get("keepalive_seconds", 60)),
            )
            self._client.loop_start()
        except Exception as exc:
            LOGGER.warning("MQTT startup failed: %s", sanitize_text(exc, limit=500))

    def stop(self) -> None:
        if not self.enabled or not self._started:
            return
        self._stop.set()
        self._connected.set()
        if self._client is not None:
            try:
                self._client.publish(
                    f"{self.base_topic}/agent/availability",
                    "offline",
                    qos=1,
                    retain=True,
                )
                self._client.disconnect()
                self._client.loop_stop()
            except Exception as exc:
                LOGGER.warning("MQTT shutdown failed: %s", sanitize_text(exc, limit=500))
        self._put(None)
        if self._publisher.is_alive():
            self._publisher.join(timeout=5)

    def publish_agent_state(self, state: dict[str, Any], *, force: bool = False) -> None:
        self._publish_json(
            f"{self.base_topic}/agent/state",
            state,
            retain=self.retain_state,
            force=force,
        )

    def publish_container_state(
        self,
        vmid: int,
        state: dict[str, Any],
        *,
        force: bool = False,
    ) -> None:
        self.publish_resource_state(vmid, state, force=force)

    def publish_resource_state(
        self,
        vmid: int,
        state: dict[str, Any],
        *,
        force: bool = False,
    ) -> None:
        payload = bounded_state(state)
        attributes = bounded_attributes(state)
        self._publish_json(
            f"{self.base_topic}/resource/{int(vmid)}/state",
            payload,
            retain=self.retain_state,
            force=force,
        )
        self._publish_json(
            f"{self.base_topic}/resource/{int(vmid)}/attributes",
            attributes,
            retain=self.retain_state,
            force=force,
        )
        if self._is_lxc(vmid):
            self._publish_json(
                f"{self.base_topic}/ct/{int(vmid)}/state",
                payload,
                retain=self.retain_state,
                force=force,
            )
            self._publish_json(
                f"{self.base_topic}/ct/{int(vmid)}/attributes",
                attributes,
                retain=self.retain_state,
                force=force,
            )

    def publish_job(self, vmid: int, job: dict[str, Any], *, force: bool = False) -> None:
        self._publish_json(
            f"{self.base_topic}/resource/{int(vmid)}/job",
            job,
            retain=self.retain_state,
            force=force,
        )
        if self._is_lxc(vmid):
            self._publish_json(
                f"{self.base_topic}/ct/{int(vmid)}/job",
                job,
                retain=self.retain_state,
                force=force,
            )

    def publish_event(self, vmid: int, event: dict[str, Any]) -> None:
        self._publish_json(
            f"{self.base_topic}/resource/{int(vmid)}/event",
            event,
            retain=False,
            force=True,
        )
        if self._is_lxc(vmid):
            self._publish_json(
                f"{self.base_topic}/ct/{int(vmid)}/event",
                event,
                retain=False,
                force=True,
            )

    def publish_discovery(self, *, force: bool = False) -> None:
        if not self.enabled:
            return
        availability = f"{self.base_topic}/agent/availability"
        agent_state = f"{self.base_topic}/agent/state"
        agent_device = {
            "identifiers": ["hubinet_ops_agent"],
            "name": "Hubinet Ops Agent",
            "manufacturer": "Hubinet",
            "model": "Ops Agent",
            "sw_version": VERSION,
        }
        for spec in AGENT_ENTITY_SPECS:
            state_topic = availability if spec.key == "availability" else agent_state
            self._discovery_sensor(
                object_id=f"hubinet_ops_agent_{spec.key}",
                unique_id=f"hubinet_ops_agent_{spec.key}",
                default_entity_id=f"sensor.hubinet_ops_agent_{spec.suffix}",
                name=spec.name,
                state_topic=state_topic,
                value_template=spec.value_template,
                availability_topic=availability if spec.key != "availability" else None,
                device=agent_device,
                extra=dict(spec.extra),
                force=force,
            )

        for vmid, cfg in sorted(self.resources.items()):
            identity = normalize_resource_identity(cfg)
            prefix = "vm" if identity.resource_type == "qemu" else "ct"
            identifier = f"hubinet_ops_{prefix}_{vmid}"
            state_topic = f"{self.base_topic}/resource/{vmid}/state"
            attributes_topic = f"{self.base_topic}/resource/{vmid}/attributes"
            model = (
                "Observed Proxmox QEMU"
                if identity.resource_type == "qemu"
                else "Managed Proxmox LXC"
            )
            if identity.adapter == "agent_self":
                model = "Hubinet Ops Agent"
            device = {
                "identifiers": [identifier],
                "name": f"Hubinet Ops {prefix.upper()}{vmid}",
                "manufacturer": "Hubinet",
                "model": model,
                "via_device": "hubinet_ops_agent",
            }
            for spec in resource_entity_specs(cfg):
                self._discovery_sensor(
                    object_id=f"hubinet_ops_{prefix}{vmid}_{spec.key}",
                    unique_id=f"hubinet_ops_{prefix}_{vmid}_{spec.key}",
                    default_entity_id=resource_entity_id(vmid, cfg, spec.key),
                    name=spec.name,
                    state_topic=state_topic,
                    value_template=spec.value_template,
                    availability_topic=availability,
                    device=device,
                    # The dashboard reads rich attributes from this one entity only.
                    # Attaching the same large JSON payload to every sensor bloats HA state storage.
                    attributes_topic=(
                        attributes_topic if spec.key == "health_status" else None
                    ),
                    extra=dict(spec.extra),
                    force=force,
                )
            self._discovery_sensor(
                object_id=f"hubinet_ops_{prefix}{vmid}_dashboard_path",
                unique_id=f"hubinet_ops_{prefix}_{vmid}_dashboard_path",
                default_entity_id=f"sensor.hubinet_ops_{resource_prefix(vmid, cfg)}_dashboard_path",
                name="Dashboard path",
                state_topic=state_topic,
                value_template="{{ value_json.dashboard_path }}",
                availability_topic=availability,
                device=device,
                force=force,
            )
            for key in obsolete_discovery_keys(cfg):
                self._publish_raw(
                    f"{self.discovery_prefix}/sensor/hubinet_ops_{prefix}{vmid}_{key}/config",
                    "",
                    retain=True,
                    force=force,
                )

    def _is_lxc(self, vmid: int) -> bool:
        cfg = self.resources.get(int(vmid))
        if cfg is None:
            # Preserve the legacy CT compatibility topic for callers publishing
            # state before an inventory entry is available.
            return True
        return normalize_resource_identity(cfg).resource_type == "lxc"

    def _discovery_sensor(
        self,
        *,
        object_id: str,
        unique_id: str,
        default_entity_id: str,
        name: str,
        state_topic: str,
        value_template: str,
        availability_topic: str | None,
        device: dict[str, Any],
        attributes_topic: str | None = None,
        extra: dict[str, Any] | None = None,
        force: bool,
    ) -> None:
        payload: dict[str, Any] = {
            "name": name,
            "object_id": object_id,
            "unique_id": unique_id,
            "default_entity_id": default_entity_id,
            "state_topic": state_topic,
            "value_template": value_template,
            "device": device,
            "origin": {"name": "Hubinet Ops", "sw_version": VERSION},
        }
        if availability_topic:
            payload.update(
                {
                    "availability_topic": availability_topic,
                    "payload_available": "online",
                    "payload_not_available": "offline",
                }
            )
        if attributes_topic:
            payload["json_attributes_topic"] = attributes_topic
        payload.update(extra or {})
        self._publish_json(
            f"{self.discovery_prefix}/sensor/{object_id}/config",
            payload,
            retain=True,
            force=force,
        )

    def _publish_raw(self, topic: str, payload: str, *, retain: bool, force: bool) -> None:
        if self.enabled:
            self._put(PublishItem(topic, payload, retain, force))

    def _publish_json(self, topic: str, value: Any, *, retain: bool, force: bool) -> None:
        if not self.enabled:
            return
        try:
            payload = json.dumps(
                sanitize_data(value),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._put(PublishItem(topic, payload, retain, force))
        except Exception as exc:
            LOGGER.warning("MQTT payload rejected: %s", sanitize_text(exc, limit=500))

    def _put(self, item: PublishItem | None) -> None:
        try:
            self._queue.put_nowait(item)
            return
        except queue.Full:
            pass

        # Non-retained events are recoverable from SQLite and may be dropped when the
        # bounded transport queue is saturated. A retained state/discovery update is
        # more important: evict one oldest queued item so the latest truth eventually wins.
        if item is not None and item.retain:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
                LOGGER.warning("MQTT queue full; evicted oldest telemetry for retained state")
                return
            except (queue.Empty, queue.Full):
                pass
        LOGGER.warning("MQTT publish queue full; dropping telemetry")

    def _publisher_loop(self) -> None:
        pending: PublishItem | None = None
        while not self._stop.is_set():
            # Never rotate a dequeued message to the back while disconnected. Keeping
            # it in `pending` preserves FIFO order and prevents the Gemini-reported loss.
            if not self._connected.wait(timeout=0.5):
                continue
            if self._stop.is_set():
                return
            if pending is None:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    return
                pending = item

            item = pending
            if item.retain and not item.force and self._cache.get(item.topic) == item.payload:
                pending = None
                continue
            try:
                info = self._client.publish(
                    item.topic,
                    item.payload,
                    qos=1,
                    retain=item.retain,
                )
                result_code = getattr(info, "rc", 0)
                if result_code not in (None, 0):
                    raise RuntimeError(f"MQTT publish returned rc={result_code}")
                if item.retain:
                    self._cache[item.topic] = item.payload
                pending = None
            except Exception as exc:
                LOGGER.warning("MQTT publish failed: %s", sanitize_text(exc, limit=500))
                # Keep the same item and retry it before later messages. MQTT is not
                # authoritative, so this thread waits without affecting update jobs.
                self._stop.wait(0.5)

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if reason_code != 0:
            LOGGER.warning("MQTT connection refused with code %s", reason_code)
            return
        self._connected.set()
        self._cache.clear()
        try:
            client.publish(
                f"{self.base_topic}/agent/availability",
                "online",
                qos=1,
                retain=True,
            )
            self.publish_discovery(force=True)
            if self._state_provider is not None:
                snapshot = self._state_provider()
                agent, containers = snapshot[:2]
                jobs = snapshot[2] if len(snapshot) > 2 else []
                self.publish_agent_state(agent, force=True)
                for state in containers:
                    self.publish_container_state(int(state["vmid"]), state, force=True)
                for job in jobs:
                    self.publish_job(int(job["vmid"]), job, force=True)
        except Exception as exc:
            LOGGER.warning(
                "MQTT reconnect publication failed: %s",
                sanitize_text(exc, limit=500),
            )

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        self._connected.clear()
        if not self._stop.is_set():
            LOGGER.warning(
                "MQTT disconnected; client backoff will retry (code %s)",
                reason_code,
            )

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(
                client_id=str(self.config.get("client_id", "hubinet-ops-agent"))
            )
        import paho.mqtt.client as mqtt

        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=str(self.config.get("client_id", "hubinet-ops-agent")),
            protocol=mqtt.MQTTv311,
        )



def _ct_entities() -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        (spec.key, spec.name, spec.value_template, dict(spec.extra))
        for spec in APT_ENTITY_SPECS
    ]


def _resource_entities(
    cfg: dict[str, Any],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        (spec.key, spec.name, spec.value_template, dict(spec.extra))
        for spec in resource_entity_specs(cfg)
    ]
