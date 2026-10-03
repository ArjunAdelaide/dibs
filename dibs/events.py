"""Ticketed events: concerts, festivals, sport.

Two sources, one shape:
    Ticketmaster Discovery API   needs a free key (TICKETMASTER_API_KEY)
    data/events.json             a calendar you keep by hand, for events Ticketmaster does not list

Event alerts (no AI model needed to run them):
    onsale     text the user shortly before tickets go on sale, or when an event comes (back) on sale
    new_show   text the user when a new event for an artist or team appears

The agent only sends an alert and a link. It never buys tickets or joins queues.

    python -m dibs.events search "fringe"
    python -m dibs.events run          # one check now; prints instead of texting
"""

import json
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

from . import config, db, geo

TM_BASE = "https://app.ticketmaster.com/discovery/v2"
ONSALE_LEAD = timedelta(minutes=15)   # warn this long before a sale with a known time
DATE_ONLY_HOUR = 8                    # a sale with a date but no time: warn at 8am that day
KINDS = {"music": "Music", "sport": "Sports", "arts": "Arts & Theatre", "comedy": "Comedy", "family": "Family"}


RADIUS_KM = 40


def is_home(where: dict | None) -> bool:
    return not where or geo.haversine_km(where["lat"], where["lon"], config.HOME_LAT, config.HOME_LON) <= config.HOME_RADIUS_KM


def _local(iso_utc: str | None, tz: str | None = None) -> str | None:
    if not iso_utc:
        return None
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(ZoneInfo(tz or config.TIMEZONE)).isoformat(timespec="minutes")


def normalise_tm(e: dict, tz: str | None = None) -> dict:
    venue = (e.get("_embedded", {}).get("venues") or [{}])[0]
    sales = e.get("sales", {})
    prices = e.get("priceRanges") or []
    return {
        "event_id": f"tm:{e['id']}",
        "name": e.get("name"),
        "start_date": e.get("dates", {}).get("start", {}).get("localDate"),
        "venue": venue.get("name"),
        "status": e.get("dates", {}).get("status", {}).get("code"),
        "onsale_at": _local(sales.get("public", {}).get("startDateTime"), tz),
        "presales": [{"name": p.get("name"), "start_at": _local(p.get("startDateTime"), tz)} for p in sales.get("presales", [])],
        "price_from": min((p["min"] for p in prices if p.get("min") is not None), default=None),
        "url": e.get("url"),
        "source": "ticketmaster",
    }


def search_ticketmaster(keyword: str | None = None, start: date | None = None, end: date | None = None,
                        size: int = 40, client: httpx.Client | None = None, kind: str | None = None,
                        where: dict | None = None) -> list[dict]:
    if not config.TICKETMASTER_API_KEY:
        return []
    params = {"apikey": config.TICKETMASTER_API_KEY, "size": size, "sort": "date,asc"}
    if is_home(where):
        params.update(countryCode=config.COUNTRY_CODE, city=config.CITY)
    else:  # anywhere else: events around the point
        params.update(latlong=f"{where['lat']},{where['lon']}", radius=RADIUS_KM, unit="km")
    if keyword:
        params["keyword"] = keyword
    if kind in KINDS:
        params["classificationName"] = KINDS[kind]
    # A "day" is the local day where the events are, sent to Ticketmaster in UTC.
    zone = ZoneInfo((None if is_home(where) else where.get("tz")) or config.TIMEZONE)
    utc = ZoneInfo("UTC")
    if start:
        params["startDateTime"] = datetime.combine(start, time(0, 0), zone).astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if end:
        params["endDateTime"] = datetime.combine(end, time(23, 59, 59), zone).astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = (client or httpx.Client(timeout=20)).get(f"{TM_BASE}/events.json", params=params)
    resp.raise_for_status()
    tz = None if is_home(where) else where.get("tz")
    return [normalise_tm(e, tz) for e in resp.json().get("_embedded", {}).get("events", [])]


def load_calendar() -> list[dict]:
    if not config.EVENTS_PATH.exists():
        return []
    out = []
    for e in json.loads(config.EVENTS_PATH.read_text()):
        out.append({"event_id": f"cal:{e['id']}", "name": e["name"], "start_date": e.get("start_date"), "end_date": e.get("end_date"),
                    "venue": e.get("venue"), "status": e.get("status"), "onsale_at": e.get("onsale_at"),
                    "presales": e.get("presales", []), "price_from": e.get("price_from"), "url": e.get("url"),
                    "source": "calendar", "tags": e.get("tags", []), "kind": e.get("kind")})
    return out


def _one_per_show(found: list[dict]) -> list[dict]:
    """A show with many sessions is one result: keep the first date and count the rest."""
    shows: dict[tuple, dict] = {}
    for e in found:
        key = ((e.get("name") or "").strip().lower(), e.get("venue"))
        if key in shows:
            shows[key]["more_dates"] = shows[key].get("more_dates", 0) + 1
        else:
            shows[key] = e
    # Big events list every ticket type as its own "event": more than 3 at one venue on one day is one event.
    by_day: dict[tuple, list[dict]] = {}
    for e in shows.values():
        by_day.setdefault((e.get("venue"), e.get("start_date")), []).append(e)
    out = []
    for group in by_day.values():
        if len(group) > 3:
            first = dict(group[0])
            first["ticket_options"] = len(group)
            out.append(first)
        else:
            out.extend(group)
    return sorted(out, key=lambda e: e.get("start_date") or "9999")


def search(keyword: str | None = None, start: date | None = None, end: date | None = None, kind: str | None = None,
           where: dict | None = None) -> list[dict]:
    """Calendar events (hand-checked, home city only) and Ticketmaster, soonest first.

    kind: music, sport, arts, comedy, family. where: a place {lat, lon, tz}; None means the home city.
    """
    words = [w.rstrip("s") or w for w in (keyword or "").lower().split()]  # "festivals" finds "festival"
    found = []
    for e in load_calendar() if is_home(where) else []:
        haystack = " ".join([e["name"], *e.get("tags", [])]).lower()
        if words and not all(w in haystack for w in words):
            continue
        if kind and e.get("kind") != kind:
            continue
        last = date.fromisoformat(e.get("end_date") or e["start_date"])
        if last < (start or date.today()) or (end and date.fromisoformat(e["start_date"]) > end):
            continue
        found.append(e)
    try:
        found += search_ticketmaster(keyword, start, end, kind=kind, where=where)
    except httpx.HTTPError as exc:
        print(f"events: Ticketmaster search failed: {type(exc).__name__}")
    return _one_per_show(sorted(found, key=lambda e: e.get("start_date") or "9999"))


def get_event(event_id: str) -> dict | None:
    source, _, raw = event_id.partition(":")
    if source == "cal":
        return next((e for e in load_calendar() if e["event_id"] == event_id), None)
    if source == "tm" and config.TICKETMASTER_API_KEY:
        resp = httpx.Client(timeout=20).get(f"{TM_BASE}/events/{raw}.json", params={"apikey": config.TICKETMASTER_API_KEY})
        return normalise_tm(resp.json()) if resp.status_code == 200 else None
    return None


def _as_moment(value: str) -> datetime:
    """A sale time, or 8am local on a sale date with no time."""
    tz = ZoneInfo(config.TIMEZONE)
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time(DATE_ONLY_HOUR), tz)
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def next_sale(event: dict, now: datetime) -> tuple[str, str] | None:
    """(label, start) of the first sale still ahead: a presale or the public on-sale."""
    sales = [(p.get("name") or "Presale", p["start_at"]) for p in event.get("presales", []) if p.get("start_at")]
    if event.get("onsale_at"):
        sales.append(("General sale", event["onsale_at"]))
    ahead = [(label, start) for label, start in sales if _as_moment(start) > now]
    return min(ahead, key=lambda s: _as_moment(s[1])) if ahead else None


def create_alert(conn: sqlite3.Connection, conv_id: str, handle: str, kind: str, event: dict | None = None,
                 keyword: str | None = None, fire_at: str | None = None, label: str = "", seen: list[str] | None = None,
                 where: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO event_alerts (conv_id, handle, kind, event_id, event_name, url, keyword, fire_at, label, seen_ids, place, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conv_id, handle, kind, event and event["event_id"], event and event["name"], event and event.get("url"), keyword,
         fire_at, label, json.dumps(seen or []), json.dumps(where) if where else None, db.now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def describe(row: sqlite3.Row) -> str:
    if row["kind"] == "new_show":
        return f"event-{row['id']} new shows for \"{row['keyword']}\""
    when = f", {row['label']} {row['fire_at']}" if row["fire_at"] else ", waiting for an on-sale date"
    return f"event-{row['id']} on-sale alert for {row['event_name']}{when}"


def check_due(conn: sqlite3.Connection, send: Callable[[str, str], None], now: datetime | None = None, force: bool = False) -> int:
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    sent = 0

    def text_user(row, text: str) -> None:
        nonlocal sent
        send(row["conv_id"], text)
        db.add_message(conn, row["conv_id"], "dibs", "assistant", text)
        sent += 1

    for row in conn.execute("SELECT * FROM event_alerts WHERE status = 'active'").fetchall():
        if row["kind"] == "onsale" and row["fire_at"]:
            moment = _as_moment(row["fire_at"])
            lead = ONSALE_LEAD if len(row["fire_at"]) > 10 else timedelta(0)
            if now >= moment - lead:
                when = f"at {moment:%-I:%M%p}" if len(row["fire_at"]) > 10 else "today"
                text_user(row, f"Heads up: {row['label']} for {row['event_name']} opens {when}. Tickets: {row['url']}")
                conn.execute("UPDATE event_alerts SET status = 'done' WHERE id = ?", (row["id"],))
            continue
        # The rest need a fresh look at the source: do it a few times a day.
        if not force and row["last_checked"] and \
                now - datetime.fromisoformat(row["last_checked"]) < timedelta(hours=config.EVENT_CHECK_HOURS):
            continue
        conn.execute("UPDATE event_alerts SET last_checked = ? WHERE id = ?", (now.isoformat(), row["id"]))
        try:
            if row["kind"] == "onsale":
                event = get_event(row["event_id"])
                sale = event and next_sale(event, now)
                if sale:
                    conn.execute("UPDATE event_alerts SET fire_at = ?, label = ? WHERE id = ?", (sale[1], sale[0], row["id"]))
                elif event and event.get("status") == "onsale":
                    text_user(row, f"{row['event_name']} is on sale now. Tickets: {event.get('url') or row['url']}")
                    conn.execute("UPDATE event_alerts SET status = 'done' WHERE id = ?", (row["id"],))
            else:
                seen = set(json.loads(row["seen_ids"]))
                where = json.loads(row["place"]) if row["place"] else None
                fresh = [e for e in search(row["keyword"], now.date(), where=where) if e["event_id"] not in seen]
                for e in fresh[:2]:  # never flood the chat
                    sale = next_sale(e, now)
                    extra = f" {sale[0]} opens {sale[1]}." if sale else ""
                    text_user(row, f"New show for \"{row['keyword']}\": {e['name']}, {e['start_date']} at {e.get('venue') or 'venue to be announced'}.{extra} {e.get('url') or ''}".strip())
                conn.execute("UPDATE event_alerts SET seen_ids = ? WHERE id = ?",
                             (json.dumps(sorted(seen | {e["event_id"] for e in fresh})), row["id"]))
        except Exception as exc:  # source down: try again at the next check
            print(f"events: check failed for event-{row['id']}: {type(exc).__name__}")
    conn.commit()
    return sent


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["search"]:
        for e in search(" ".join(sys.argv[2:]) or None):
            print(f"{e['start_date']}  {e['name']}  [{e['status']}]  on sale: {e.get('onsale_at') or '-'}  ({e['source']})")
        if not config.TICKETMASTER_API_KEY:
            print("(calendar only: TICKETMASTER_API_KEY is not set)")
    elif sys.argv[1:] == ["run"]:
        n = check_due(db.connect(), lambda conv, text: print(f"[would text {conv}] {text}"), force=True)
        print(f"Checked. {n} event alert(s) fired.")
    else:
        print('Usage: python -m dibs.events search "keyword"  |  python -m dibs.events run')
