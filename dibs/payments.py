"""Payments through Stripe. Optional, and off until PAYMENTS_ENABLED=1.

The user saves a card once on a Stripe-hosted page (card or Apple Pay). After a
YES, Dibs charges that card for the exact total shown in the proposal. Dibs never
sees or stores card numbers: Stripe holds them.

Safety, enforced here:
    - test keys only, unless PAYMENTS_LIVE=1 is set on purpose
    - the amount is only held at YES; it is taken when the booking is confirmed
    - one hold per proposal (idempotency key)
    - no charge above MAX_CHARGE_CENTS
"""

import sqlite3

from . import config, db


CHARGEABLE = ("card", "link")  # saved payment method types that can be charged after a YES


class PaymentError(Exception):
    pass


def enabled() -> bool:
    return config.PAYMENTS_ENABLED and bool(config.STRIPE_SECRET_KEY)


def enabled_for(venue: dict) -> bool:
    """Payments run only for venues in the home country: one currency, and a person can complete the booking."""
    return enabled() and (venue.get("country_code") or config.COUNTRY_CODE).lower() == config.COUNTRY_CODE.lower()


def _stripe():
    import stripe

    if config.STRIPE_SECRET_KEY.startswith("sk_live_") and not config.PAYMENTS_LIVE:
        raise PaymentError("a live Stripe key is set but PAYMENTS_LIVE is not 1; refusing to move real money")
    stripe.api_key = config.STRIPE_SECRET_KEY
    return stripe


def customer_id(conn: sqlite3.Connection, handle: str) -> str:
    prefs = db.get_prefs(conn, handle)
    if prefs.get("stripe_customer"):
        return prefs["stripe_customer"]
    customer = _stripe().Customer.create(name=prefs.get("name"), metadata={"dibs_handle": handle})
    db.set_pref(conn, handle, "stripe_customer", customer.id)
    return customer.id


def saved_method(conn: sqlite3.Connection, handle: str) -> tuple[str, str] | None:
    """(id, label) of the user's saved payment method, or None. Asked from Stripe each time, so no webhook is needed.

    The Stripe page can save a plain card (also Apple Pay and Google Pay) or a Link account: both can be held and charged later.
    """
    if not db.get_prefs(conn, handle).get("stripe_customer"):
        return None
    stripe = _stripe()
    try:
        methods = stripe.Customer.list_payment_methods(customer_id(conn, handle), limit=10)
    except stripe.StripeError as exc:
        raise PaymentError(f"could not reach Stripe ({type(exc).__name__})") from exc
    for m in methods.data:
        if m.type == "card":
            return m.id, f"{m.card.brand.title()} ending {m.card.last4}"
        if m.type == "link":
            return m.id, "your Link account"
    return None


def saved_card(conn: sqlite3.Connection, handle: str) -> str | None:
    method = saved_method(conn, handle)
    return method[0] if method else None


def remove_saved_methods(conn: sqlite3.Connection, handle: str) -> int:
    """Delete every saved payment method for this user at Stripe."""
    if not db.get_prefs(conn, handle).get("stripe_customer"):
        return 0
    stripe = _stripe()
    try:
        methods = stripe.Customer.list_payment_methods(customer_id(conn, handle), limit=20).data
        for m in methods:
            stripe.PaymentMethod.detach(m.id)
    except stripe.StripeError as exc:
        raise PaymentError(f"could not remove the card ({type(exc).__name__})") from exc
    return len(methods)


def setup_link(conn: sqlite3.Connection, handle: str) -> str:
    """A Stripe-hosted page where the user saves a card for later charges."""
    stripe = _stripe()
    try:
        # Payment methods (card, Apple Pay, Google Pay) come from the Stripe Dashboard settings.
        session = stripe.checkout.Session.create(
            mode="setup", customer=customer_id(conn, handle), currency=config.CURRENCY,
            success_url=config.PAYMENT_RETURN_URL, cancel_url=config.PAYMENT_RETURN_URL,
        )
    except stripe.StripeError as exc:
        raise PaymentError(f"could not open the card page ({type(exc).__name__})") from exc
    return session.url


def hold(conn: sqlite3.Connection, handle: str, amount_cents: int, description: str, proposal_id: int) -> str:
    """Reserve the amount on the saved card without taking it. Returns the PaymentIntent id.

    The money only moves when capture() runs, after the booking is confirmed. release() gives it back.
    """
    if amount_cents <= 0 or amount_cents > config.MAX_CHARGE_CENTS:
        raise PaymentError(f"amount is outside the allowed range (max ${config.MAX_CHARGE_CENTS / 100:.0f})")
    stripe = _stripe()
    card = saved_card(conn, handle)
    if not card:
        raise PaymentError("no saved card")
    try:
        customer = customer_id(conn, handle)
        intent = stripe.PaymentIntent.create(
            amount=amount_cents, currency=config.CURRENCY, customer=customer, payment_method=card,
            off_session=True, confirm=True, capture_method="manual", description=description,
            metadata={"dibs_proposal": str(proposal_id)},
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            idempotency_key=f"dibs-{customer}-hold-{proposal_id}",  # one hold per customer and proposal, even if the database is rebuilt
        )
    except stripe.CardError as exc:
        raise PaymentError(f"the card was declined ({exc.code})") from exc
    except stripe.StripeError as exc:
        raise PaymentError(f"Stripe could not reserve the payment ({type(exc).__name__})") from exc
    if intent.status not in ("requires_capture", "succeeded"):
        raise PaymentError(f"the payment needs more steps ({intent.status})")
    return intent.id


def capture(payment_intent: str) -> None:
    """Take the held money: the booking is confirmed."""
    stripe = _stripe()
    try:
        if stripe.PaymentIntent.retrieve(payment_intent).status == "requires_capture":
            stripe.PaymentIntent.capture(payment_intent)
    except stripe.StripeError as exc:
        raise PaymentError(f"could not take the held payment ({type(exc).__name__}); check the Stripe Dashboard") from exc


def release(payment_intent: str) -> str:
    """Give the money back: cancel a hold, or refund if it was already taken. Returns 'released' or 'refunded'."""
    stripe = _stripe()
    try:
        status = stripe.PaymentIntent.retrieve(payment_intent).status
        if status == "requires_capture":
            stripe.PaymentIntent.cancel(payment_intent)
            return "released"
        if status == "succeeded":
            stripe.Refund.create(payment_intent=payment_intent)
            return "refunded"
        return "released"  # already cancelled: nothing was taken
    except stripe.StripeError as exc:
        raise PaymentError(f"could not return the payment ({type(exc).__name__}); do it in the Stripe Dashboard") from exc
