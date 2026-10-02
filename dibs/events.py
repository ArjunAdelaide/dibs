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

from . import config, db

TM_BASE = "https://app.ticketmaster.com/discovery/v2"
ONSALE_LEAD = timedelta(minutes=15)   # warn this long before a sale with a known time
DATE_ONLY_HOUR = 8                    # a sale with a date but no time: warn at 8am that day


def _local(iso_utc: str | None) -> str | None:
    if not iso_utc:
        return None
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(ZoneInfo(config.TIMEZONE)).isoformat(timespec="minutes")


def normalise_tm(e: dict) -> dict:
    venue = (e.get("_embedded", {}).get("venues") or [{}])[0]
    sales = e.get("sales", {})
    prices = e.get("priceRanges") or []
    return {
        "event_id": f"tm:{e['id']}",
        "name": e.get("name"),
        "start_date": e.get("dates", {}).get("start", {}).get("localDate"),
        "venue": venue.get("name"),
        "status": e.get("dates", {}).get("status", {}).get("code"),
        "onsale_at": _local(sales.get("public", {}).get("startDateTime")),
        "presales": [{"name": p.get("name"), "start_at": _local(p.get("startDateTime"))} for p in sales.get("presales", [])],
        "price_from": min((p["min"] for p in prices if p.get("min") is not None), default=None),
        "url": e.get("url"),
        "source": "ticketmaster",
    }


def search_ticketmaster(keyword: str | None = None, start: date | None = None, end: date | None = None,
                        size: int = 10, client: httpx.Client | None = None) -> list[dict]:
    if not config.TICKETMASTER_API_KEY:
        return []
    params = {"apikey": config.TICKETMASTER_API_KEY, "countryCode": config.COUNTRY_CODE, "city": config.CITY,
              "size": size, "sort": "date,asc"}
    if keyword:
        params["keyword"] = keyword
    if start:
        params["startDateTime"] = f"{start.isoformat()}T00:00:00Z"
    if end:
        params["endDateTime"] = f"{end.isoformat()}T23:59:59Z"
    resp = (client or httpx.Client(timeout=20)).get(f"{TM_BASE}/events.json", params=params)
    resp.raise_for_status()
    return [normalise_tm(e) for e in resp.json().get("_embedded", {}).get("events", [])]


def load_calendar() -> list[dict]:
    if not config.EVENTS_PATH.exists():
        return []
    out = []
    for e in json.loads(config.EVENTS_PATH.read_text()):
        out.append({"event_id": f"cal:{e['id']}", "name": e["name"], "start_date": e.get("start_date"), "end_date": e.get("end_date"),
                    "venue": e.get("venue"), "status": e.get("status"), "onsale_at": e.get("onsale_at"),
                    "presales": e.get("presales", []), "price_from": e.get("price_from"), "url": e.get("url"),
                    "source": "calendar", "tags": e.get("tags", [])})
    return out


def search(keyword: str | None = None, start: date | None = None, end: date | None = None) -> list[dict]:
    """Calendar events first (hand-checked), then Ticketmaster, soonest first."""
    words = [w.rstrip("s") or w for w in (keyword or "").lower().split()]  # "festivals" finds "festival"
    found = []
    for e in load_calendar():
        haystack = " ".join([e["name"], *e.get("tags", [])]).lower()
        if words and not all(w in haystack for w in words):
            continue
        last = date.fromisoformat(e.get("end_date") or e["start_date"])
        if last < (start or date.today()) or (end and date.fromisoformat(e["start_date"]) > end):
            continue
        found.append(e)
    try:
        found += search_ticketmaster(keyword, start, end)
    except httpx.HTTPError as exc:
        print(f"events: Ticketmaster search failed: {type(exc).__name__}")
    return sorted(found, key=lambda e: e.get("start_date") or "9999")


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
                 keyword: str | None = None, fire_at: str | None = None, label: str = "", seen: list[str] | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO event_alerts (conv_id, handle, kind, event_id, event_name, url, keyword, fire_at, label, seen_ids, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conv_id, handle, kind, event and event["event_id"], event and event["name"], event and event.get("url"), keyword,
         fire_at, label, json.dumps(seen or []), db.now_iso()),
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
                fresh = [e for e in search(row["keyword"], now.date()) if e["event_id"] not in seen]
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
