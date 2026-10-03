"""Operator console: complete or fail the bookings that need a person.

From your phone, reply to the Dibs alert:
    booked 3 Ref STK-2291, lane 7
    failed 3 fully booked at 4pm
    jobs
    open 3          (opens the prepared checkout window again)

Or in a terminal:
    python -m dibs.ops list
    python -m dibs.ops booked 3 "Ref STK-2291"
    python -m dibs.ops failed 3 "Fully booked at 4pm, 5:30pm is free"
"""

import argparse
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import config, db, payments
from .catalog import Catalog

OPEN = ("needs_human", "requested", "paid_needs_human")
COMMAND = re.compile(r"^\s*(booked|failed)\s+#?(\d+)\s*(.*)$|^\s*(jobs)\s*$|^\s*(open)\s+#?(\d+)\s*$", re.IGNORECASE | re.DOTALL)
Send = Callable[[str, str], None]


def _booking(conn: sqlite3.Connection, booking_id: int):
    return conn.execute(
        "SELECT b.id, b.status, b.payment_intent, b.amount_cents, b.created_at, p.conv_id, p.handle, p.venue_id, p.starts_at, p.party_size "
        "FROM bookings b JOIN proposals p ON p.id = b.proposal_id WHERE b.id = ?", (booking_id,)).fetchone()


def _tell(conn: sqlite3.Connection, send: Send, conv_id: str, text: str) -> None:
    db.add_message(conn, conv_id, "dibs", "assistant", text)
    if not conv_id.startswith(("cli;", "api;")):
        send(conv_id, text)


def jobs(conn: sqlite3.Connection, catalog: Catalog) -> str:
    rows = conn.execute(
        f"SELECT b.id, b.amount_cents, p.handle, p.venue_id, p.starts_at, p.party_size FROM bookings b "
        f"JOIN proposals p ON p.id = b.proposal_id WHERE b.status IN {OPEN} ORDER BY b.id").fetchall()
    lines = []
    for r in rows:
        venue = catalog.venues.get(r["venue_id"], {})
        held = f", ${r['amount_cents'] / 100:.2f} held" if r["amount_cents"] else ""
        lines.append(f"#{r['id']} {venue.get('name', r['venue_id'])}, {r['starts_at'][:16].replace('T', ' ')}, x{r['party_size']}{held}")
    return "\n".join(lines) or "No bookings waiting."


def complete(conn: sqlite3.Connection, catalog: Catalog, send: Send, booking_id: int, outcome: str, detail: str) -> str:
    """Close a booking as booked or failed, settle the money, and text the user. Returns a line for the operator."""
    row = _booking(conn, booking_id)
    if not row:
        return f"No booking #{booking_id}."
    if row["status"] not in OPEN:
        return f"Booking #{booking_id} is already {row['status']}."
    name = catalog.venues.get(row["venue_id"], {}).get("name", row["venue_id"])
    amount = f"${row['amount_cents'] / 100:.2f}" if row["amount_cents"] else ""
    try:
        if outcome == "booked":
            money = ""
            if row["payment_intent"]:
                payments.capture(row["payment_intent"])
                money = f" I've charged {amount} to your saved card."
            text = f"You're booked at {name}." + (f" {detail.rstrip('.')}." if detail else "") + money
        else:
            money = ""
            if row["payment_intent"]:
                how = payments.release(row["payment_intent"])
                money = f" The {amount} hold on your card is released." if how == "released" else f" I've refunded your {amount} in full."
            text = f"Couldn't lock in {name}{': ' + detail if detail else ''}.{money} Want me to try something else?"
    except payments.PaymentError as exc:
        return f"Booking #{booking_id} NOT closed: {exc}"
    conn.execute("UPDATE bookings SET status = ?, reference = ?, updated_at = ? WHERE id = ?", (outcome, detail, db.now_iso(), booking_id))
    conn.commit()
    _tell(conn, send, row["conv_id"], text)
    return f"Done. #{booking_id} is {outcome} and the user was told." + (f" {amount} {'charged' if outcome == 'booked' else 'returned'}." if amount else "")


def operator_command(conn: sqlite3.Connection, catalog: Catalog, send: Send, text: str) -> str | None:
    """Handle 'booked 3 ref', 'failed 3 reason' or 'jobs' from the operator. Returns the reply, or None if it is not a command."""
    match = COMMAND.match(text)
    if not match:
        return None
    if match.group(4):
        return jobs(conn, catalog)
    if match.group(5):
        return reopen(conn, catalog, int(match.group(6)))
    return complete(conn, catalog, send, int(match.group(2)), match.group(1).lower(), match.group(3).strip())


def reopen(conn: sqlite3.Connection, catalog: Catalog, booking_id: int) -> str:
    """Open the prepared checkout window again for a booking that still waits."""
    from . import supervised
    from .executors import find_slot

    row = conn.execute("SELECT b.status, b.amount_cents, p.* FROM bookings b JOIN proposals p ON p.id = b.proposal_id WHERE b.id = ?",
                       (booking_id,)).fetchone()
    if not row or row["status"] not in OPEN:
        return f"Booking #{booking_id} is not waiting."
    venue = catalog.venues.get(row["venue_id"], {})
    if not supervised.can_supervise(venue):
        return f"No prepared window for {venue.get('name', row['venue_id'])}. Book it on their site."
    slot, rate = find_slot(venue, datetime.fromisoformat(row["starts_at"]), row["rate"], row["party_size"], fresh=True)
    if not slot:
        return f"The slot for #{booking_id} is no longer on the venue's sheet. Reply: failed {booking_id} slot taken"
    customer = supervised.customer_details(db.get_prefs(conn, row["handle"]).get("name"), row["handle"])
    supervised.launch(booking_id, venue, rate.url, row["party_size"], f"${(row['amount_cents'] or 0) / 100:.2f}", customer)
    return f"Opened #{booking_id} in a window on the Mac."


def refund_stale_paid(conn: sqlite3.Connection, catalog: Catalog, send: Send, now_iso: str | None = None) -> int:
    """A held booking that nobody completed in time is released and the user is told. Keeps the promise in the chat."""
    cutoff = (datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)) - timedelta(minutes=config.PAID_TIMEOUT_MINUTES)
    rows = conn.execute("SELECT id, created_at FROM bookings WHERE status = 'paid_needs_human'").fetchall()
    done = 0
    for row in rows:
        if datetime.fromisoformat(row["created_at"]) > cutoff:
            continue
        result = complete(conn, catalog, send, row["id"], "failed", "I ran out of time to complete it")
        if result.startswith("Done"):
            done += 1
        else:
            print(f"ops: {result}")
    return done


def cancel_by_user(conn: sqlite3.Connection, catalog: Catalog, handle: str, conv_id: str, notify_operator: Callable[[str], None]) -> dict:
    """The user cancels their latest booking in this chat."""
    row = conn.execute(
        "SELECT b.id FROM bookings b JOIN proposals p ON p.id = b.proposal_id WHERE p.conv_id = ? AND p.handle = ? "
        "AND b.status IN ('needs_human', 'requested', 'paid_needs_human', 'booked') ORDER BY b.id DESC LIMIT 1", (conv_id, handle)).fetchone()
    if not row:
        return {"error": "no active booking found for this user in this chat"}
    booking = _booking(conn, row["id"])
    name = catalog.venues.get(booking["venue_id"], {}).get("name", booking["venue_id"])
    if booking["status"] == "booked":  # already confirmed with the venue: a person must undo it there
        notify_operator(f"[dibs] {handle} wants to CANCEL confirmed booking #{booking['id']} at {name} ({booking['starts_at'][:16]}). "
                        "Cancel it with the venue, then refund in Stripe if their policy allows.")
        return {"cancelled": False, "status": "passed to a person",
                "tell_user": f"The booking at {name} is already confirmed with the venue, so a person will cancel it for you and "
                             "tell you what the venue's policy allows. Do not promise a refund."}
    money = ""
    try:
        if booking["payment_intent"]:
            how = payments.release(booking["payment_intent"])
            amount = f"${booking['amount_cents'] / 100:.2f}"
            money = f" The {amount} hold on the card is released." if how == "released" else f" The {amount} is refunded in full."
    except payments.PaymentError as exc:
        return {"error": f"could not return the payment: {exc}"}
    conn.execute("UPDATE bookings SET status = 'cancelled', updated_at = ? WHERE id = ?", (db.now_iso(), booking["id"]))
    conn.commit()
    notify_operator(f"[dibs] {handle} cancelled booking #{booking['id']} at {name}. Do not book it.")
    from .executors import _when

    return {"cancelled": True, "tell_user": f"Cancelled: {name}, {_when(datetime.fromisoformat(booking['starts_at']))}.{money}"}


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

    conn, catalog = db.connect(), Catalog.load()
    print(jobs(conn, catalog) if args.cmd == "list" else complete(conn, catalog, send_to_chat, args.booking_id, args.cmd, args.detail))


if __name__ == "__main__":
    main()
