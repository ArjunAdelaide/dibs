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
           time_from: str, time_to: str, party_size: int, max_price: float | None) -> int:
    cur = conn.execute(
        "INSERT INTO alerts (conv_id, handle, venue_id, day, weekday, time_from, time_to, party_size, max_price, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conv_id, handle, venue_id, day, weekday, time_from, time_to, party_size, max_price, db.now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def describe(row: sqlite3.Row, catalog: Catalog) -> str:
    venue = catalog.venues.get(row["venue_id"], {"name": row["venue_id"]})
    when = row["day"] or f"next {row['weekday']}"
    price = f", up to ${row['max_price']:.0f} per person" if row["max_price"] is not None else ""
    return f"#{row['id']} {venue['name']}, {when}, {row['time_from']} to {row['time_to']}, {row['party_size']} people{price}"


def check_due(conn: sqlite3.Connection, catalog: Catalog, send: Callable[[str, str], None], now: datetime | None = None,
              force: bool = False) -> int:
    """Check every active alert that is due. Returns the number of messages sent."""
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    sent = 0
    for row in conn.execute("SELECT * FROM alerts WHERE status = 'active'").fetchall():
        if not force and row["last_checked"]:
            if now - datetime.fromisoformat(row["last_checked"]) < timedelta(minutes=config.ALERT_INTERVAL_MINUTES):
                continue
        venue = catalog.venues.get(row["venue_id"])
        days = dates_for(row["day"], row["weekday"], now.date())
        if not venue or not has_connector(venue) or not days:
            conn.execute("UPDATE alerts SET status = 'expired' WHERE id = ?", (row["id"],))
            conn.commit()
            continue
        try:
            local_now = now.astimezone(ZoneInfo(venue.get("tz") or config.TIMEZONE))
            matches = find_matches(venue, days, row["time_from"], row["time_to"], row["party_size"], row["max_price"], local_now)
        except Exception as exc:  # venue site down: try again at the next wake-up
            print(f"alerts: check failed for #{row['id']}: {type(exc).__name__}")
            matches = []
        conn.execute("UPDATE alerts SET last_checked = ? WHERE id = ?", (now.isoformat(), row["id"]))
        if matches:
            m = matches[0]
            when = datetime.fromisoformat(f"{m['day']}T{m['time']}")
            text = (f"Your alert came through: {venue['name']} has {when:%-I:%M%p} open on {when:%a %-d %b} "
                    f"at ${m['price_per_person']:.2f} per person. Reply \"book it\" and I'll set it up.")
            send(row["conv_id"], text)
            db.add_message(conn, row["conv_id"], "dibs", "assistant", text)
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
