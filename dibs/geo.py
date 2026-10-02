"""Location: distance between points, and place name -> coordinates.

Geocoding uses OpenStreetMap Nominatim (free, no key, max 1 request per second).
iMessage does not give a user's location by itself. A user gives it in one of
three ways: a place name ("I'm in Norwood"), a shared location pin, or a maps link.
"""

import math
import re

import httpx

from . import config

COORDS = re.compile(r"(-?\d{1,2}\.\d{3,})\\?,\s*(-?\d{2,3}\.\d{3,})")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def extract_coords(text: str) -> tuple[float, float] | None:
    """Coordinates inside a maps link or a 'lat,lon' pair."""
    match = COORDS.search(text or "")
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def geocode(place: str, client: httpx.Client | None = None) -> dict | None:
    coords = extract_coords(place)
    if coords:
        return {"name": "shared location", "lat": coords[0], "lon": coords[1]}
    client = client or httpx.Client(timeout=15, headers={"User-Agent": "dibs/0.1 (personal booking agent)"})
    resp = client.get("https://nominatim.openstreetmap.org/search",
                      params={"q": f"{place}, {config.REGION}", "format": "json", "limit": 1})
    resp.raise_for_status()
    hits = resp.json()
    if not hits:
        return None
    return {"name": hits[0]["display_name"].split(",")[0], "lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"])}
