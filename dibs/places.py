"""Venues anywhere: when a user is outside the home city, find places near them on OpenStreetMap.

The home city has a hand-checked list (data/venues.json). Everywhere else, Dibs looks up
venues around the user the first time someone asks from that area, keeps them, and then
checks each venue's website in the background for a booking platform it can read live.
"""

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db, fingerprint, geo
from .catalog import Catalog

MIRRORS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
RADIUS_KM = 15
KEEP_DAYS = 30
SITES_PER_AREA = 30  # venue websites checked for a booking platform, one per second

KINDS = {  # OpenStreetMap tag -> our category
    ("leisure", "bowling_alley"): "bowling", ("sport", "10pin"): "bowling", ("leisure", "golf_course"): "golf",
    ("golf", "driving_range"): "driving_range", ("leisure", "miniature_golf"): "mini_golf", ("sport", "laser_tag"): "laser_tag",
    ("leisure", "escape_game"): "escape_room", ("leisure", "amusement_arcade"): "arcade", ("leisure", "trampoline_park"): "trampoline",
    ("sport", "climbing"): "climbing", ("sport", "karting"): "karting", ("leisure", "ice_rink"): "ice_skating",
}


def category_of(tags: dict) -> str:
    if re.search(r"\bVR\b|virtual reality", tags.get("name", ""), re.IGNORECASE):
        return "vr"
    return next((kind for (key, value), kind in KINDS.items() if tags.get(key) == value), "other")


def is_home(lat: float, lon: float) -> bool:
    return geo.haversine_km(lat, lon, config.HOME_LAT, config.HOME_LON) <= config.HOME_RADIUS_KM


def _tile(lat: float, lon: float) -> str:
    return f"{round(lat, 1)},{round(lon, 1)}"  # about 10 km: one lookup serves everyone nearby


def fetch_osm(lat: float, lon: float, client: httpx.Client | None = None) -> list[dict]:
    around = f"(around:{RADIUS_KM * 1000},{lat},{lon})"
    parts = "".join(f'nwr["{key}"="{value}"]{around};' for key, value in KINDS)
    query = f"[out:json][timeout:25];({parts});out center tags 150;"
    client = client or httpx.Client(timeout=35, headers={"User-Agent": "dibs/0.1 (personal booking agent)"})
    for mirror in MIRRORS:  # the public servers are often busy; try the next one
        try:
            resp = client.post(mirror, data={"data": query})
            resp.raise_for_status()
            return resp.json()["elements"]
        except (httpx.HTTPError, ValueError):
            continue
    raise ConnectionError("OpenStreetMap lookup failed")


def to_venue(element: dict, place: dict) -> dict | None:
    tags = element.get("tags", {})
    if not tags.get("name"):
        return None
    center = element.get("center", element)
    street = " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")]))
    return {
        "id": f"{re.sub(r'[^a-z0-9]+', '-', tags['name'].lower()).strip('-')}-{element['id']}",
        "name": tags["name"], "categories": [category_of(tags)],
        "area": tags.get("addr:suburb") or tags.get("addr:city") or place.get("city") or "",
        "address": ", ".join(filter(None, [street, tags.get("addr:city")])),
        "lat": center.get("lat"), "lon": center.get("lon"),
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": tags.get("website") or tags.get("contact:website"),
        "hours": tags.get("opening_hours"), "verified": False, "source": "osm-live",
        "country_code": place.get("country_code"), "tz": place.get("tz"),
    }


def load_into(catalog: Catalog, conn: sqlite3.Connection) -> int:
    """At start-up: put the venues found earlier back into the catalog."""
    rows = conn.execute("SELECT data FROM discovered").fetchall()
    for row in rows:
        venue = json.loads(row["data"])
        catalog.venues.setdefault(venue["id"], venue)
    return len(rows)


def _save(conn: sqlite3.Connection, venue: dict, tile: str) -> None:
    conn.execute("INSERT INTO discovered (id, tile, data) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                 (venue["id"], tile, json.dumps(venue)))


def ensure_area(conn: sqlite3.Connection, catalog: Catalog, place: dict, fetch=None, background: bool = True) -> dict:
    """Make sure venues around this place are in the catalog. Returns {"new": n} or {"error": ...}."""
    if is_home(place["lat"], place["lon"]):
        return {"new": 0, "home": True}
    tile = _tile(place["lat"], place["lon"])
    seen = conn.execute("SELECT fetched_at FROM areas WHERE tile = ?", (tile,)).fetchone()
    if seen and datetime.now(timezone.utc) - datetime.fromisoformat(seen["fetched_at"]) < timedelta(days=KEEP_DAYS):
        return {"new": 0}
    try:
        elements = (fetch or fetch_osm)(place["lat"], place["lon"])
    except Exception as exc:
        return {"error": f"could not look up venues near there ({type(exc).__name__})"}
    found = [v for v in (to_venue(e, place) for e in elements) if v and v["id"] not in catalog.venues]
    for venue in found:
        catalog.venues[venue["id"]] = venue
        _save(conn, venue, tile)
    conn.execute("INSERT INTO areas (tile, fetched_at) VALUES (?, ?) ON CONFLICT(tile) DO UPDATE SET fetched_at = excluded.fetched_at",
                 (tile, db.now_iso()))
    conn.commit()
    if background and found:
        threading.Thread(target=inspect_sites, args=(catalog, [v["id"] for v in found if v.get("website")][:SITES_PER_AREA], tile),
                         daemon=True).start()
    return {"new": len(found)}


def inspect_sites(catalog: Catalog, venue_ids: list[str], tile: str, pause: float = 1.0) -> int:
    """In the background: read each new venue's website to learn its booking page and platform."""
    client = httpx.Client(follow_redirects=True, timeout=12, headers={"User-Agent": "Mozilla/5.0 (dibs venue check)"})
    conn = db.connect()  # a thread needs its own database connection
    live = 0
    for venue_id in venue_ids:
        venue = catalog.venues.get(venue_id)
        if not venue:
            continue
        try:
            if fingerprint.inspect_site(client, venue):
                _save(conn, venue, tile)
                conn.commit()
                live += bool(venue.get("connector"))
        except Exception:  # one odd website must not stop the rest
            pass
        time.sleep(pause)
    conn.close()
    return live
