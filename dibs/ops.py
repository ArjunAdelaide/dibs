"""Concierge console: you execute bookings by hand in Phase 0, then close them here.

    python -m dibs.ops list
    python -m dibs.ops booked 3 "Ref STK-2291"
    python -m dibs.ops failed 3 "Fully booked at 4pm, 5:30pm is free"
"""

import argparse

from . import db, payments
from .catalog import Catalog


def refund_stale_paid(conn, catalog: Catalog, send, now_iso: str | None = None) -> int:
    """A paid booking that was not completed in time is refunded and the user is told. Keeps the promise in the chat."""
    from datetime import datetime, timedelta, timezone

    from . import config

    cutoff = (datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)) - timedelta(minutes=config.PAID_TIMEOUT_MINUTES)
    rows = conn.execute(
        "SELECT b.id, b.payment_intent, b.amount_cents, b.created_at, p.conv_id, p.venue_id FROM bookings b "
        "JOIN proposals p ON p.id = b.proposal_id WHERE b.status = 'paid_needs_human'").fetchall()
    done = 0
    for row in rows:
        if datetime.fromisoformat(row["created_at"]) > cutoff:
            continue
        try:
            payments.refund(row["payment_intent"])
        except payments.PaymentError as exc:
            print(f"ops: refund for booking #{row['id']} failed: {exc}")
            continue
        conn.execute("UPDATE bookings SET status = 'refunded', updated_at = ? WHERE id = ?", (db.now_iso(), row["id"]))
        conn.commit()
        name = catalog.venues.get(row["venue_id"], {}).get("name", row["venue_id"])
        text = f"Sorry, I couldn't complete your booking at {name}. I've refunded your ${row['amount_cents'] / 100:.2f} in full."
        db.add_message(conn, row["conv_id"], "dibs", "assistant", text)
        send(row["conv_id"], text)
        done += 1
    return done


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("booked", "failed"):
        p = sub.add_parser(name)
        p.add_argument("booking_id", type=int)
        p.add_argument("detail")
    args = parser.parse_args()

    from .channels.imessage import send_to_chat  # imported here: the bridge imports this module too

    conn = db.connect()
    catalog = Catalog.load()
    if args.cmd == "list":
        rows = conn.execute(
            "SELECT b.id, b.status, p.* FROM bookings b JOIN proposals p ON p.id = b.proposal_id WHERE b.status IN ('needs_human', 'requested', 'paid_needs_human') ORDER BY b.id"
        ).fetchall()
        for r in rows:
            venue = catalog.venues.get(r["venue_id"], {})
            print(f"#{r[0]} {r['handle']} {venue.get('name', r['venue_id'])} {r['starts_at']} x{r['party_size']} -> {venue.get('booking_url') or venue.get('phone')}")
        if not rows:
            print("No bookings waiting.")
        return

    row = conn.execute(
        "SELECT p.conv_id, p.venue_id, p.starts_at, b.payment_intent, b.amount_cents FROM bookings b JOIN proposals p ON p.id = b.proposal_id WHERE b.id = ?",
        (args.booking_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"No booking #{args.booking_id}")
    conn.execute("UPDATE bookings SET status = ?, reference = ?, updated_at = ? WHERE id = ?",
                 (args.cmd, args.detail, db.now_iso(), args.booking_id))
    conn.commit()
    name = catalog.venues.get(row["venue_id"], {}).get("name", row["venue_id"])
    refunded = ""
    if args.cmd == "failed" and row["payment_intent"]:  # never keep money for a booking that did not happen
        payments.refund(row["payment_intent"])
        refunded = f" I've refunded your ${row['amount_cents'] / 100:.2f} in full."
    text = (f"You're booked at {name}. {args.detail}" if args.cmd == "booked"
            else f"Couldn't lock in {name}: {args.detail}.{refunded} Want me to try something else?")
    db.add_message(conn, row["conv_id"], "dibs", "assistant", text)
    if not row["conv_id"].startswith("cli;"):
        send_to_chat(row["conv_id"], text)
    print(text)


if __name__ == "__main__":
    main()
