"""Constants for the DSP Switcher Audio Console integration.

This module is deliberately free of Home Assistant imports so that it (and
``api.py``, which imports it) can be exercised by a plain ``pytest`` run
without installing the ``homeassistant`` package.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "dsp_switcher"

# Config entry / options keys.
CONF_BASE_URL: Final = "base_url"
CONF_API_TOKEN: Final = "api_token"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_SCAN_INTERVAL: Final = 5
MIN_SCAN_INTERVAL: Final = 2
MAX_SCAN_INTERVAL: Final = 60

# HTTP.
REQUEST_TIMEOUT: Final = 10
PATH_STATE: Final = "/api/state"
PATH_SESSION: Final = "/api/session"
PATH_COMMAND: Final = "/api/command"

# Console fader range. Every level the gateway exposes -- zone outputs, matrix
# sends and source trims -- shares this dB range (internal/matrix/service.go,
# zoneDbMin/zoneDbMax), and the percentage is a plain linear position on it.
DB_MIN: Final = -60.0
DB_MAX: Final = 0.0

# Command frame types accepted by POST /api/command for a member-role token
# (internal/api/ws.go, applyCommand). Admin-only frames are intentionally
# absent: a token can never satisfy them.
CMD_ZONE_LEVEL: Final = "zone/level"
CMD_ZONE_MUTE: Final = "zone/mute"
CMD_ZONE_SOURCE: Final = "zone/source"
CMD_CROSSPOINT: Final = "crosspoint"
CMD_CROSSPOINT_LEVEL: Final = "crosspoint/level"
CMD_SOURCE_ENABLE: Final = "source/enable"
CMD_SOURCE_LEVEL: Final = "source/level"
CMD_SOURCE_CONTROL: Final = "source/control"
CMD_MASTER: Final = "master"
CMD_MASTER_OVERWRITE: Final = "master/overwrite"
CMD_SPOTIFY_PLAYPAUSE: Final = "spotify/playpause"
CMD_SPOTIFY_NEXT: Final = "spotify/next"
CMD_SPOTIFY_PREVIOUS: Final = "spotify/previous"

TRANSPORT_ACTIONS: Final = ("playpause", "next", "previous")
SIDES: Final = ("l", "r")

# Service names.
SERVICE_SET_CROSSPOINT: Final = "set_crosspoint"
SERVICE_SET_SEND_LEVEL: Final = "set_send_level"
SERVICE_MASTER_OVERWRITE: Final = "master_overwrite"
SERVICE_SOURCE_CONTROL: Final = "source_control"
SERVICE_SEND_COMMAND: Final = "send_command"

ATTR_ENTRY_ID: Final = "entry_id"
ATTR_INPUT: Final = "input"
ATTR_OUTPUT: Final = "output"
ATTR_ON: Final = "on"
ATTR_SIDE: Final = "side"
ATTR_LEVEL_PCT: Final = "level_pct"
ATTR_LEVEL_DB: Final = "level_db"
ATTR_MASTER: Final = "master"
ATTR_ACTION: Final = "action"
ATTR_TYPE: Final = "type"
ATTR_PAYLOAD: Final = "payload"
