"""Constants for the Hubinet Ops integration."""

from datetime import timedelta

DOMAIN = "hubinet_ops"

CONF_BASE_URL = "base_url"
CONF_API_TOKEN = "api_token"
CONF_VERIFY_TLS = "verify_tls"

DEFAULT_VERIFY_TLS = True
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)

DATA_API_FACTORY = "api_factory"

MANUFACTURER = "Hubinet Ops"
MODEL_NODE = "Proxmox node via Hubinet Ops"
MODEL_QEMU = "QEMU virtual machine via Hubinet Ops"
MODEL_LXC = "LXC container via Hubinet Ops"
