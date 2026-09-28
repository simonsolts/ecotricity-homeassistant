"""Constants for the Ecotricity integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ecotricity"

CONF_ACCOUNT_ID: Final = "account_id"
CONF_PROPERTY_ID: Final = "property_id"
CONF_ACCOUNT_NUMBER: Final = "account_number"

UPDATE_INTERVAL: Final = timedelta(hours=3)

# The portal keeps 13 months of reads.
BACKFILL_DAYS: Final = 395
# After the first import, fetch this many days on each update to catch late reads.
REFRESH_DAYS: Final = 14

# Junifer times have no time zone. They are UK local time.
PORTAL_TIME_ZONE: Final = "Europe/London"
