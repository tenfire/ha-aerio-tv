"""Constants for AerioTV."""

from homeassistant.const import Platform

DOMAIN = "aeriotv"
PLATFORMS = [Platform.MEDIA_PLAYER]
CONF_DEVICE_ID = "device_id"
CONF_TOKEN = "token"
CONF_PORT = "port"
DEFAULT_NAME = "AerioTV"
SERVICE_TYPE = "_aeriotv._tcp.local."
