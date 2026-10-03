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


def _client() -> httpx.Client:
    return httpx.Client(timeout=15, headers={"User-Agent": "dibs/0.1 (personal booking agent)"})


def _search(client: httpx.Client, query: str, near: dict | None) -> dict | None:
    params = {"q": query, "format": "json", "limit": 1, "addressdetails": 1}
    if near:  # prefer results close to where the user already is; it is a preference, not a fence
        params["viewbox"] = f"{near['lon'] - 1},{near['lat'] + 1},{near['lon'] + 1},{near['lat'] - 1}"
    resp = client.get("https://nominatim.openstreetmap.org/search", params=params)
    resp.raise_for_status()
    hits = resp.json()
    if not hits:
        return None
    address = hits[0].get("address", {})
    return {"name": hits[0]["display_name"].split(",")[0], "lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]),
            "city": address.get("city") or address.get("town") or address.get("village") or address.get("state"),
            "country_code": address.get("country_code")}


def geocode(place: str, client: httpx.Client | None = None, near: dict | None = None) -> dict | None:
    """A place name anywhere in the world -> coordinates, city and country.

    Short names ("Norwood") are tried in the home region first, then near the user, then worldwide.
    """
    coords = extract_coords(place)
    if coords:
        return {"name": "shared location", "lat": coords[0], "lon": coords[1]}
    client = client or _client()
    if "," not in place and not near:
        hit = _search(client, f"{place}, {config.REGION}", None)
        if hit:
            return hit
    return _search(client, place, near)


def timezone_at(lat: float, lon: float, client: httpx.Client | None = None) -> str | None:
    """The time zone name at a point (Open-Meteo, free, no key)."""
    try:
        resp = (client or _client()).get("https://api.open-meteo.com/v1/forecast",
                                         params={"latitude": lat, "longitude": lon, "timezone": "auto", "current": "temperature_2m"})
        resp.raise_for_status()
        return resp.json().get("timezone")
    except (httpx.HTTPError, ValueError):
        return None


def forecast(lat: float, lon: float, when, client: httpx.Client | None = None) -> dict | None:
    """Weather for one hour at a place, up to about two weeks ahead (Open-Meteo, free, no key)."""
    try:
        resp = (client or _client()).get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 16,
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m"})
        resp.raise_for_status()
        hourly = resp.json()["hourly"]
        i = hourly["time"].index(when.strftime("%Y-%m-%dT%H:00"))
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    return {"temperature_c": hourly["temperature_2m"][i], "chance_of_rain_percent": hourly["precipitation_probability"][i],
            "wind_kmh": hourly["wind_speed_10m"][i]}
