"""How a confirmed booking gets done. One route per venue, chosen in code:

    link       venue has a live connector: re-check the slot, send the exact link
    email      venue has a booking email and the bot has a mailbox: send the request
    page       venue has an online booking page: send that page
    concierge  anything else: alert the operator, who books by hand
"""

import smtplib
from datetime import datetime
from email.message import EmailMessage

from . import config, db, memory, payments
from .connectors import has_connector, slots_for


def pick_route(venue: dict) -> str:
    if has_connector(venue):
        return "link"
    if venue.get("booking_email") and config.SMTP_USER and config.SMTP_PASSWORD:
        return "email"
    if venue.get("booking_url"):
        return "page"
    return "concierge"


def find_slot(venue: dict, when: datetime, rate_name: str | None, party_size: int = 1, fresh: bool = False):
    """Return (slot, rate) for the exact time, or (None, None) if it is gone."""
    for slot in slots_for(venue, when.date(), fresh=fresh):
        if slot.time == when.strftime("%H:%M") and slot.fits(party_size):
            wanted = [r for r in slot.rates if not rate_name or r.name == rate_name]
            if wanted:
                return slot, min(wanted, key=lambda r: r.price) if not rate_name else wanted[0]
    return None, None


def send_booking_email(venue: dict, when: datetime, party_size: int, name: str, phone: str, notes: str) -> str:
    body = (
        f"Hi {venue['name']} team,\n\n"
        f"Could I please book the following?\n\n"
        f"  Date: {when.strftime('%A %d %B %Y')}\n"
        f"  Time: {when.strftime('%I:%M %p')}\n"
        f"  People: {party_size}\n"
        f"  Name: {name}\n"
        f"  Phone: {phone}\n"
        + (f"  Notes: {notes}\n" if notes else "")
        + ("\nWe will pay on arrival. " if venue.get("payment") == "at_venue" else "\nPlease tell us how you take payment. ")
        + "Please reply to confirm, or suggest the closest time if this one is taken.\n\n"
        f"Thanks,\n{name}\n(sent with Dibs, a booking assistant, on {name}'s behalf)\n"
    )
    msg = EmailMessage()
    msg["Subject"] = f"Booking request: {party_size} people, {when.strftime('%a %d %b %I:%M %p')}"
    msg["From"] = config.SMTP_USER
    msg["To"] = venue["booking_email"]
    if config.BOOKING_REPLY_TO:
        msg["Reply-To"] = config.BOOKING_REPLY_TO
    msg.set_content(body)
    if config.DRY_RUN:
        print(f"[dry-run email -> {venue['booking_email']}]\n{body}")
        return "dry_run"
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)
    return "sent"


def _when(when: datetime) -> str:
    return f"{when:%a %-d %b}, {when:%-I:%M}{when:%p}".replace("AM", "am").replace("PM", "pm")


def _people(n: int) -> str:
    return "1 person" if n == 1 else f"{n} people"


def proposal_text(venue: dict, when: datetime, party_size: int, rate=None, deal_note: str | None = None, card: str | None = None) -> str:
    """The exact booking summary the user approves. Written by code so it always matches what gets booked."""
    pay_with = f" with {card}" if card else " (you'll save a card first)"
    lines = [venue["name"], _when(when), _people(party_size)]
    if rate:
        total = total_cents(rate, party_size) / 100
        lines.append(f"${total:.2f}" if party_size == 1 else f"${rate.price:.2f} each, ${total:.2f} total")
        ask = f"Reply YES to book and pay ${total:.2f}{pay_with}." if payments.enabled() else "Reply YES to book."
    else:
        if deal_note:
            lines.append(deal_note)
        ask = "Reply YES to book."
    return "\n".join(lines) + "\n\n" + ask


def result_text(result: dict, venue: dict, proposal) -> str:
    """The reply after a YES, by route. Written by code: no model call, and no wrong amounts."""
    when = _when(datetime.fromisoformat(proposal["starts_at"]))
    what = f"{venue['name']}, {when}, {_people(proposal['party_size'])}"
    if result.get("needs_card"):  # the summary is repeated so the next YES approves exactly this booking
        return ("One quick step first: save a card on Stripe's secure page (card or Apple Pay). Dibs never sees it.\n"
                f"{result['setup_link']}\n\nThen come back here:\n\n{proposal['shown']}")
    route = result.get("route")
    if route == "paid":
        return (f"On it: {what}.\n\nI've put a hold of {result['held']} on {result['card']}. "
                "You're only charged when the booking is confirmed. I'll text you as soon as it is.")
    if route == "link":
        return f"That slot is open: {what}, {result['price']}.\nFinish on the venue's page. It isn't held until you do:\n{result['booking_link']}"
    if route == "page":
        return f"I can't see live times for {venue['name']}. Pick your time and book on their page:\n{result['booking_link']}"
    if route == "email":
        return f"I've sent your booking request to {venue['name']}: {when}, {_people(proposal['party_size'])}. I'll text you when they confirm."
    return f"On it: {what}. I'll text you the confirmation shortly."


def total_cents(rate, party_size: int) -> int:
    return round(rate.price * 100) * party_size


def pay_and_hand_over(ctx, proposal, venue: dict, rate, record) -> dict:
    """Hold the exact total on the user's saved card, then hand the booking to the operator.

    The money is only taken when the booking is confirmed. Until Dibs can pay venues by itself,
    a person completes the booking on the venue site.
    """
    total = total_cents(rate, proposal["party_size"])
    try:
        method = payments.saved_method(ctx.conn, ctx.handle)
        if not method:
            return {"needs_card": True, "setup_link": payments.setup_link(ctx.conn, ctx.handle), "status": "NOT booked and NOT charged"}
        intent = payments.hold(ctx.conn, ctx.handle, total, f"{venue['name']} {proposal['starts_at']} x{proposal['party_size']}", proposal["id"])
    except payments.PaymentError as exc:
        return {"error": f"payment failed: {exc}. Nothing was booked. Tell the user plainly and offer the venue link instead: {rate.url}"}
    booking_id = record("paid_needs_human", total, intent)
    ctx.notify_operator(
        f"[dibs] Booking #{booking_id}: ${total / 100:.2f} HELD from {ctx.handle} for {venue['name']} {proposal['starts_at'][:16]} "
        f"x{proposal['party_size']} ({rate.name}). Book it here: {rate.url}\n"
        f"Then reply: booked {booking_id} <reference>   or: failed {booking_id} <reason>"
    )
    return {"booking_id": booking_id, "route": "paid", "held": f"${total / 100:.2f}", "card": method[1],
            "status": "amount held; the booking is being completed and is NOT confirmed yet"}


def execute(ctx, proposal, venue: dict) -> dict:
    """Run the venue's route for a proposal the user has just approved."""
    when = datetime.fromisoformat(proposal["starts_at"])
    route = pick_route(venue)

    def record(status: str, amount_cents: int | None = None, payment_intent: str | None = None) -> int:
        cur = ctx.conn.execute(
            "INSERT INTO bookings (proposal_id, status, amount_cents, payment_intent, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (proposal["id"], status, amount_cents, payment_intent, db.now_iso(), db.now_iso()),
        )
        ctx.conn.execute("UPDATE proposals SET status = 'confirmed' WHERE id = ?", (proposal["id"],))
        ctx.conn.commit()
        memory.note(ctx.handle, f"Booking ({status}): {venue['name']}, {when:%a %d %b %Y %H:%M}, {proposal['party_size']} people", ctx.now)
        return cur.lastrowid

    if route == "link":
        slot, rate = find_slot(venue, when, proposal["rate"], proposal["party_size"], fresh=True)  # never book on old data
        if not slot:
            return {"error": "that slot was just taken; call check_availability again and offer the nearest times"}
        if payments.enabled():
            return pay_and_hand_over(ctx, proposal, venue, rate, record)
        return {
            "booking_id": record("link_sent"), "route": "link", "booking_link": rate.url,
            "price": f"${rate.price:.2f} per person ({rate.name})",
            "status": "NOT booked yet: the user must finish on the venue page",
            "tell_user": "Do not say it is booked. Say the slot is open right now, send this exact link, and say the "
                         "booking is done only when they pick the players and finish on the venue's page.",
        }

    prefs = db.get_prefs(ctx.conn, ctx.handle)
    if route == "email":
        if not prefs.get("name"):
            return {"error": "need the name for the booking; ask the user, save it with remember(key='name'), then ask for YES again"}
        send_booking_email(venue, when, proposal["party_size"], prefs["name"], ctx.handle, proposal["notes"])
        booking_id = record("requested")
        ctx.notify_operator(f"[dibs] Booking #{booking_id}: emailed {venue['name']} for {ctx.handle}, "
                            f"{proposal['starts_at'][:16]} x{proposal['party_size']}. When they answer, reply: booked {booking_id} <reference>   or: failed {booking_id} <reason>")
        return {"booking_id": booking_id, "route": "email",
                "status": "requested from the venue, NOT confirmed yet",
                "tell_user": "Say the booking request went to the venue by email and you will text when the venue confirms."}

    if route == "page":
        return {"booking_id": record("link_sent"), "route": "page", "booking_link": venue["booking_url"],
                "status": "NOT booked yet: the user must finish on the venue page",
                "tell_user": "You cannot see live times for this venue. Send this booking page link and say they pick the "
                             "time and finish there. Do not say it is booked."}

    booking_id = record("needs_human")
    ctx.notify_operator(
        f"[dibs] Booking #{booking_id} for {ctx.handle} ({prefs.get('name', 'no name')}): {venue['name']} "
        f"{proposal['starts_at'][:16]} x{proposal['party_size']}{' deal ' + proposal['deal_id'] if proposal['deal_id'] else ''}. "
        f"Book via {venue.get('booking_url') or venue.get('phone') or venue.get('website')}.\n"
        f"Then reply: booked {booking_id} <reference>   or: failed {booking_id} <reason>"
    )
    return {"booking_id": booking_id, "route": "concierge",
            "tell_user": "Request is with our team; they will text the confirmation shortly."}
