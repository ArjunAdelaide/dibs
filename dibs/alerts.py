"""Alerts: the agent watches a venue and texts only when the wanted slot shows up.

Each alert has three parts (the Instinct pattern):
    goal        venue + day + time window + party size (+ price limit)
    wake-up     checked every ALERT_INTERVAL_MINUTES
    alert rule  message the user once, only when a real slot matches

The check uses no AI model, so alerts cost nothing to run.

    python -m dibs.alerts list
    python -m dibs.alerts run      # one check now; prints instead of texting
"""

import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Callable
from zoneinfo import ZoneInfo

from . import config, db
from .catalog import DAYS, Catalog
from .connectors import has_connector, slots_for


def dates_for(day: str | None, weekday: str | None, today: date) -> list[date]:
    if day:
        target = date.fromisoformat(day)
        return [target] if target >= today else []
    return [today + timedelta(days=n) for n in range(0, 15) if DAYS[(today + timedelta(days=n)).weekday()] == weekday][:2]


def find_matches(venue: dict, days: list[date], time_from: str, time_to: str, party_size: int,
                 max_price: float | None, now: datetime) -> list[dict]:
    matches = []
    for day in days:
        for slot in slots_for(venue, day):
            if not (time_from <= slot.time <= time_to) or not slot.fits(party_size):
                continue
            if day == now.date() and slot.time <= now.strftime("%H:%M"):
                continue
            rates = [r for r in slot.rates if max_price is None or r.price <= max_price]
            if rates:
                best = min(rates, key=lambda r: r.price)
                matches.append({"day": day.isoformat(), "time": slot.time, "rate": best.name, "price_per_person": best.price})
    return matches


def create(conn: sqlite3.Connection, conv_id: str, handle: str, venue_id: str, day: str | None, weekday: str | None,
           time_from: str, time_to: str, party_size: int, max_price: float | None, repeat: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO alerts (conv_id, handle, venue_id, day, weekday, time_from, time_to, party_size, max_price, repeat, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conv_id, handle, venue_id, day, weekday, time_from, time_to, party_size, max_price, int(repeat), db.now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def describe(row: sqlite3.Row, catalog: Catalog) -> str:
    venue = catalog.venues.get(row["venue_id"], {"name": row["venue_id"]})
    when = row["day"] or (f"every {row['weekday']}" if row["repeat"] else f"next {row['weekday']}")
    price = f", up to ${row['max_price']:.0f} per person" if row["max_price"] is not None else ""
    return f"#{row['id']} {venue['name']}, {when}, {row['time_from']} to {row['time_to']}, {row['party_size']} people{price}"


WEEKLY_LEAD_DAYS = 4  # a weekly booking is looked for this many days ahead


def _fire(conn: sqlite3.Connection, venue: dict, row: sqlite3.Row, match: dict, now: datetime,
          notify_operator: Callable[[str], None]) -> str:
    """A slot matched: get it ready for one YES, or book it at once when the user's auto-book limit covers it."""
    from . import executors, payments

    when = datetime.fromisoformat(f"{match['day']}T{match['time']}").replace(tzinfo=ZoneInfo(venue.get("tz") or config.TIMEZONE))
    slot, rate = executors.find_slot(venue, when, match["rate"], row["party_size"])
    proposal_id, shown = executors.create_proposal(conn, venue, row["conv_id"], row["handle"], when, row["party_size"], rate=rate,
                                                   now=now, ttl_minutes=180)
    proposal = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    what = "weekly booking" if row["repeat"] else "alert"
    limit = db.get_prefs(conn, row["handle"]).get("auto_book_cents") or 0
    if limit and proposal["total_cents"] and proposal["total_cents"] <= limit and payments.enabled_for(venue):
        ctx = SimpleNamespace(conn=conn, handle=row["handle"], conv_id=row["conv_id"], now=now, notify_operator=notify_operator)
        result = executors.execute(ctx, proposal, venue)
        if result.get("route") == "paid":
            return (f"Your {what} found a slot, and it is inside your auto-book limit, so I went ahead.\n\n"
                    f"{executors.result_text(result, venue, proposal)}\n\nText \"cancel my booking\" if you don't want it.")
    return f"Your {what} found a slot.\n\n{shown}"


def check_due(conn: sqlite3.Connection, catalog: Catalog, send: Callable[[str, str], None], now: datetime | None = None,
              force: bool = False, notify_operator: Callable[[str], None] = print) -> int:
    """Check every active alert that is due. Returns the number of messages sent."""
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    sent = 0
    for row in conn.execute("SELECT * FROM alerts WHERE status = 'active'").fetchall():
        if not force and row["last_checked"]:
            if now - datetime.fromisoformat(row["last_checked"]) < timedelta(minutes=config.ALERT_INTERVAL_MINUTES):
                continue
        venue = catalog.venues.get(row["venue_id"])
        local_now = now.astimezone(ZoneInfo((venue or {}).get("tz") or config.TIMEZONE))
        days = dates_for(row["day"], row["weekday"], local_now.date())
        if row["repeat"]:  # weekly: only the coming occurrence, a few days ahead, once per week
            days = [d for d in days[:1] if (d - local_now.date()).days <= WEEKLY_LEAD_DAYS and d.isoformat() != row["last_fired_for"]]
        if not venue or not has_connector(venue) or (not days and not row["repeat"]):
            conn.execute("UPDATE alerts SET status = 'expired' WHERE id = ?", (row["id"],))
            conn.commit()
            continue
        try:
            matches = find_matches(venue, days, row["time_from"], row["time_to"], row["party_size"], row["max_price"], local_now)
        except Exception as exc:  # venue site down: try again at the next wake-up
            print(f"alerts: check failed for #{row['id']}: {type(exc).__name__}")
            matches = []
        conn.execute("UPDATE alerts SET last_checked = ? WHERE id = ?", (now.isoformat(), row["id"]))
        if matches:
            text = _fire(conn, venue, row, matches[0], now, notify_operator)
            send(row["conv_id"], text)
            db.add_message(conn, row["conv_id"], "dibs", "assistant", text)
            if row["repeat"]:
                conn.execute("UPDATE alerts SET last_fired_for = ? WHERE id = ?", (matches[0]["day"], row["id"]))
            else:
                conn.execute("UPDATE alerts SET status = 'done' WHERE id = ?", (row["id"],))
            sent += 1
        conn.commit()
    return sent


if __name__ == "__main__":
    import sys

    conn, catalog = db.connect(), Catalog.load()
    if sys.argv[1:] == ["run"]:
        n = check_due(conn, catalog, lambda conv, text: print(f"[would text {conv}] {text}"), force=True)
        print(f"Checked. {n} alert(s) matched.")
    else:
        rows = conn.execute("SELECT * FROM alerts WHERE status = 'active'").fetchall()
        print("\n".join(describe(r, catalog) for r in rows) or "No active alerts.")
