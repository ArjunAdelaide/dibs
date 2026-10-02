"""Payments through Stripe. Optional, and off until PAYMENTS_ENABLED=1.

The user saves a card once on a Stripe-hosted page (card or Apple Pay). After a
YES, Dibs charges that card for the exact total shown in the proposal. Dibs never
sees or stores card numbers: Stripe holds them.

Safety, enforced here:
    - test keys only, unless PAYMENTS_LIVE=1 is set on purpose
    - one charge per proposal (idempotency key)
    - no charge above MAX_CHARGE_CENTS
"""

import sqlite3

from . import config, db


class PaymentError(Exception):
    pass


def enabled() -> bool:
    return config.PAYMENTS_ENABLED and bool(config.STRIPE_SECRET_KEY)


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


def saved_card(conn: sqlite3.Connection, handle: str) -> str | None:
    """The id of the user's saved card, or None. Asked from Stripe each time, so no webhook is needed."""
    if not db.get_prefs(conn, handle).get("stripe_customer"):
        return None
    stripe = _stripe()
    try:
        cards = stripe.PaymentMethod.list(customer=customer_id(conn, handle), type="card", limit=1)
    except stripe.StripeError as exc:
        raise PaymentError(f"could not reach Stripe ({type(exc).__name__})") from exc
    return cards.data[0].id if cards.data else None


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


def charge(conn: sqlite3.Connection, handle: str, amount_cents: int, description: str, proposal_id: int) -> str:
    """Charge the saved card. Returns the PaymentIntent id, or raises PaymentError with a reason for the user."""
    if amount_cents <= 0 or amount_cents > config.MAX_CHARGE_CENTS:
        raise PaymentError(f"amount is outside the allowed range (max ${config.MAX_CHARGE_CENTS / 100:.0f})")
    stripe = _stripe()
    card = saved_card(conn, handle)
    if not card:
        raise PaymentError("no saved card")
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents, currency=config.CURRENCY, customer=customer_id(conn, handle), payment_method=card,
            off_session=True, confirm=True, description=description, metadata={"dibs_proposal": str(proposal_id)},
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            idempotency_key=f"dibs-{handle}-proposal-{proposal_id}",
        )
    except stripe.CardError as exc:
        raise PaymentError(f"the card was declined ({exc.code})") from exc
    except stripe.StripeError as exc:
        raise PaymentError(f"Stripe could not take the payment ({type(exc).__name__})") from exc
    if intent.status != "succeeded":
        raise PaymentError(f"the payment needs more steps ({intent.status})")
    return intent.id


def refund(payment_intent: str) -> None:
    stripe = _stripe()
    try:
        stripe.Refund.create(payment_intent=payment_intent)
    except stripe.StripeError as exc:
        raise PaymentError(f"refund failed ({type(exc).__name__}): refund it in the Stripe Dashboard") from exc
