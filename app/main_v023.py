from __future__ import annotations

from . import mqtt as legacy_mqtt
from .mqtt_v023 import MqttTelemetry, VERSION

legacy_mqtt.MqttTelemetry = MqttTelemetry
legacy_mqtt.VERSION = VERSION

# Import only after patching so app.main and app.service bind the 0.2.3 class/version.
from .main import app  # noqa: E402,F401
