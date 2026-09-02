"""Constants for the Hubinet Ops integration."""

from datetime import timedelta

DOMAIN = "hubinet_ops"

CONF_BASE_URL = "base_url"
CONF_API_TOKEN = "api_token"
CONF_VERIFY_TLS = "verify_tls"

DEFAULT_VERIFY_TLS = True
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)

DATA_API_FACTORY = "api_factory"
DATA_COORDINATORS = "coordinators"
DATA_SERVICES_REGISTERED = "services_registered"

SERVICE_VIEW_UPDATE_PLAN = "view_update_plan"
SERVICE_APPROVE_UPDATE_PLAN = "approve_update_plan"
SERVICE_VIEW_HEALTH_CONTRACT = "view_health_contract"
SERVICE_SET_HEALTH_CONTRACT = "set_health_contract"
SERVICE_CLEAR_HEALTH_CONTRACT = "clear_health_contract"

MANUFACTURER = "Hubinet Ops"
MODEL_SOURCE = "Proxmox inventory source via Hubinet Ops"
MODEL_NODE = "Proxmox node via Hubinet Ops"
MODEL_QEMU = "QEMU virtual machine via Hubinet Ops"
MODEL_LXC = "LXC container via Hubinet Ops"
