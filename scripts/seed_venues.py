"""Seed data/venues.json from OpenStreetMap (free, no key).

OSM coverage is thin, so treat this as a starting list: hand-check every venue,
fill in booking_url / price_notes / off_peak_notes, and set verified=true.
Re-running merges: it adds new venues and never overwrites your edits.

    python scripts/seed_venues.py
"""

import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dibs import config  # noqa: E402

BBOX = "-35.25,138.40,-34.60,138.85"  # greater Adelaide
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
SELECTORS = {
    '["leisure"="bowling_alley"]': "bowling",
    '["sport"="10pin"]': "bowling",
    '["leisure"="golf_course"]': "golf",
    '["golf"="driving_range"]': "driving_range",
    '["leisure"="miniature_golf"]': "mini_golf",
    '["sport"="laser_tag"]': "laser_tag",
    '["leisure"="escape_game"]': "escape_room",
    '["leisure"="amusement_arcade"]': "arcade",
    '["leisure"="trampoline_park"]': "trampoline",
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def category_of(tags: dict) -> str:
    if re.search(r"\bVR\b|virtual reality", tags.get("name", ""), re.IGNORECASE):
        return "vr"
    for selector, category in SELECTORS.items():
        key, value = re.findall(r'"([^"]+)"', selector)
        if tags.get(key) == value:
            return category
    return "other"


def main() -> None:
    query = "[out:json][timeout:60];(" + "".join(f"nwr{s}({BBOX});" for s in SELECTORS) + ");out center tags;"
    resp = None
    for mirror in MIRRORS:  # the public servers are often busy; try the next one
        try:
            resp = httpx.post(mirror, data={"data": query}, headers={"User-Agent": "dibs-dev/0.1"}, timeout=90)
            resp.raise_for_status()
            break
        except httpx.HTTPError as exc:
            print(f"{mirror}: {exc}")
            resp = None
    if resp is None:
        raise SystemExit("All Overpass mirrors failed; try again in a minute.")

    path = config.VENUES_PATH
    existing = json.loads(path.read_text()) if path.exists() else []
    known_osm = {v.get("osm_id") for v in existing}
    known_ids = {v["id"] for v in existing}
    added = 0
    for el in resp.json()["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name")
        osm_id = f"{el['type']}/{el['id']}"
        if not name or osm_id in known_osm:
            continue
        vid = slug(name)
        if vid in known_ids:
            vid = f"{vid}-{el['id']}"
        center = el.get("center", el)
        street = " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")]))
        existing.append({
            "id": vid,
            "name": name,
            "categories": [category_of(tags)],
            "area": tags.get("addr:suburb") or tags.get("addr:city") or "",
            "address": ", ".join(filter(None, [street, tags.get("addr:suburb")])),
            "lat": center.get("lat"),
            "lon": center.get("lon"),
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "website": tags.get("website") or tags.get("contact:website"),
            "booking_url": None,
            "booking_platform": None,
            "hours": tags.get("opening_hours"),
            "price_notes": "",
            "off_peak_notes": "",
            "verified": False,
            "active": True,
            "source": "osm",
            "osm_id": osm_id,
        })
        known_ids.add(vid)
        added += 1

    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    print(f"Added {added} venues; {len(existing)} total in {path}")


if __name__ == "__main__":
    main()
