"""Venue and deal catalog, loaded from hand-curated JSON in data/.

Deal conditions are checked here in code, never by the model, so a deal is only
quoted when the requested day, time and party size actually qualify.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import config

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class Catalog:
    venues: dict[str, dict] = field(default_factory=dict)
    deals: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, venues_path: Path | None = None, deals_path: Path | None = None) -> "Catalog":
        venues_path = venues_path or config.VENUES_PATH
        deals_path = deals_path or config.DEALS_PATH
        venues = json.loads(venues_path.read_text()) if venues_path.exists() else []
        deals = json.loads(deals_path.read_text()) if deals_path.exists() else []
        return cls(venues={v["id"]: v for v in venues if v.get("active", True)}, deals=deals)

    def search(self, category: str | None = None, area: str | None = None, query: str | None = None, limit: int | None = 5) -> list[dict]:
        results = []
        for venue in self.venues.values():
            if category and category.lower() not in [c.lower() for c in venue.get("categories", [])]:
                continue
            if area and area.lower() not in (venue.get("area") or "").lower():
                continue
            if query and query.lower() not in json.dumps(venue).lower():
                continue
            results.append(venue)
        # Verified venues with a known booking route first.
        results.sort(key=lambda v: (not v.get("verified"), not v.get("booking_url"), v["name"]))
        return results[:limit]

    def deals_for(self, venue_id: str) -> list[dict]:
        return [d for d in self.deals if d["venue_id"] == venue_id]


def deal_applies(deal: dict, when: datetime, party_size: int, today: date | None = None) -> tuple[bool, str]:
    """Return (applies, reason). Missing conditions mean no restriction."""
    cond = deal.get("conditions", {})
    today = today or when.date()
    expires = deal.get("expires")
    if expires and date.fromisoformat(expires) < today:
        return False, f"expired {expires}"
    days = cond.get("days")
    if days and DAYS[when.weekday()] not in days:
        return False, f"only valid {', '.join(days)}"
    hhmm = when.strftime("%H:%M")
    if cond.get("start") and hhmm < cond["start"]:
        return False, f"only from {cond['start']}"
    if cond.get("end") and hhmm >= cond["end"]:
        return False, f"only before {cond['end']}"
    if cond.get("min_party") and party_size < cond["min_party"]:
        return False, f"needs at least {cond['min_party']} people"
    if cond.get("max_party") and party_size > cond["max_party"]:
        return False, f"max {cond['max_party']} people"
    return True, "applies"
