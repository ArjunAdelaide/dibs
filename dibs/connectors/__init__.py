"""Connectors read live availability from a venue's booking platform.

A venue opts in with a "connector" block in data/venues.json, for example:
    "connector": {"type": "quick18", "base_url": "https://example.quick18.com"}
"""

import time
from datetime import date

from . import miclub, quick18

FETCHERS = {"quick18": quick18.fetch_slots, "miclub": miclub.fetch_slots}
_cache: dict[tuple, tuple[float, list]] = {}
CACHE_SECONDS = 60  # be polite to venue servers


def has_connector(venue: dict) -> bool:
    return (venue.get("connector") or {}).get("type") in FETCHERS


def slots_for(venue: dict, day: date) -> list:
    conn = venue["connector"]
    key = (conn["type"], conn["base_url"], str(conn.get("fee_group_ids")), day)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    slots = FETCHERS[conn["type"]](conn, day)
    _cache[key] = (time.monotonic(), slots)
    return slots
