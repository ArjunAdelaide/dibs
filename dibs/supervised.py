"""Supervised checkout (phase A): Dibs prepares the booking, a person pays.

When a held booking needs completing, Dibs opens a visible browser window on
this Mac at the exact slot, picks the number of players, and fills in the
customer's details on each page. It never ticks terms, never presses a
continue or pay button, and never touches payment fields. The operator checks
the page, pays, and Dibs reads the confirmation and closes the booking itself:
the hold is captured and the user gets their confirmation.

If Dibs cannot read a reference, nothing is closed: the operator texts
"booked 3 <reference>" as before.
"""

import re
import threading
import time
from typing import Callable

from . import config

SUPPORTED = ("quick18",)
WINDOW_MINUTES = 20

# Read a booking reference from a confirmation page.
REFERENCE = re.compile(
    r"(?:confirmation|reservation|booking|order)\s*(?:number|no\.?|#|id|code|reference|ref)\s*(?:is)?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})",
    re.IGNORECASE,
)
CONFIRMED_PAGE = re.compile(r"(thank you|thanks)[^.]{0,60}(booking|reservation|order)|booking (is )?confirmed|reservation (is )?confirmed",
                            re.IGNORECASE)

BANNER_JS = """
(info) => {
  let bar = document.getElementById('dibs-banner');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'dibs-banner';
    bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;background:#111827;color:#f9fafb;' +
      'font:14px/1.45 -apple-system,sans-serif;padding:12px 16px;box-shadow:0 -4px 16px rgba(0,0,0,.3);white-space:pre-line';
    document.body.appendChild(bar);
  }
  bar.textContent = info;
}
"""

# Fill empty name, email and phone boxes. Card fields, hidden fields and payment frames are never touched.
AUTOFILL_JS = """
(c) => {
  const NEVER = /card|cc-|cvv|cvc|expir|security|pass|promo|coupon|gift/i;
  const label = el => {
    const l = el.id && document.querySelector('label[for="' + el.id + '"]');
    return [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.getAttribute('autocomplete'), l && l.innerText]
      .filter(Boolean).join(' ');
  };
  let filled = 0;
  document.querySelectorAll('input').forEach(el => {
    const type = (el.type || 'text').toLowerCase();
    if (!['text', 'email', 'tel', ''].includes(type) || el.value || el.offsetParent === null) return;
    const text = label(el);
    if (NEVER.test(text)) return;
    let value = null;
    if (/e-?mail/i.test(text)) value = c.email;
    else if (/phone|mobile|cell/i.test(text)) value = c.phone;
    else if (/first|given/i.test(text)) value = c.first;
    else if (/last|sur|family/i.test(text)) value = c.last;
    else if (/^(?!.*(company|club|member)).*name/i.test(text)) value = c.full;
    if (!value) return;
    el.focus(); el.value = value;
    el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
    filled++;
  });
  return filled;
}
"""


def can_supervise(venue: dict) -> bool:
    return config.SUPERVISED_CHECKOUT and (venue.get("connector") or {}).get("type") in SUPPORTED


def customer_details(name: str | None, handle: str) -> dict:
    full = (name or "").strip() or "Dibs Guest"
    first, _, last = full.partition(" ")
    return {"full": full, "first": first, "last": last or first, "phone": handle if handle.startswith("+") else "",
            "email": config.BOOKING_EMAIL or (handle if "@" in handle else "")}


def read_reference(page_text: str) -> str | None:
    match = REFERENCE.search(page_text or "")
    return match.group(1) if match else None


def prepare_quick18(page, party_size: int) -> None:
    """On a Quick18 slot page: choose the number of players. Nothing is submitted."""
    if party_size <= 4:
        page.check(f"#Players{party_size - 1}")
    else:
        page.click("#lnkMorePlyrs")
        page.select_option("#GrpPlayers", str(party_size))


PREPARE = {"quick18": prepare_quick18}


def run_window(booking_id: int, venue: dict, slot_url: str, party_size: int, total: str, customer: dict,
               on_reference: Callable[[str], None], log: Callable[[str], None] = print, headless: bool = False) -> str:
    """Open the window and wait. Returns how it ended: 'confirmed', 'no_reference', 'closed' or 'timeout'."""
    from playwright.sync_api import sync_playwright

    info = (f"DIBS booking #{booking_id}: {venue['name']}, {party_size} people, {total} held from the customer.\n"
            f"Customer: {customer['full']}   {customer['phone']}   {customer['email']}\n"
            "Check the details, tick any terms, and pay. Dibs reads the confirmation and tells the customer.")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        try:
            page.goto(slot_url, wait_until="domcontentloaded", timeout=30000)
            PREPARE[venue["connector"]["type"]](page, party_size)
            deadline = time.monotonic() + WINDOW_MINUTES * 60
            last_url = None
            while time.monotonic() < deadline:
                if page.is_closed() or not browser.is_connected():
                    return "closed"
                try:
                    if page.url != last_url:  # a new step: label it and fill what we can
                        last_url = page.url
                        page.wait_for_load_state("domcontentloaded")
                        page.evaluate(BANNER_JS, info)
                        filled = page.evaluate(AUTOFILL_JS, customer)
                        if filled:
                            log(f"supervised #{booking_id}: filled {filled} field(s) on {page.url}")
                    text = page.evaluate("document.body.innerText")
                except Exception:  # the page is changing under us: look again next second
                    time.sleep(1)
                    continue
                if CONFIRMED_PAGE.search(text) or REFERENCE.search(text):
                    reference = read_reference(text)
                    if reference:
                        on_reference(reference)
                        page.evaluate(BANNER_JS, f"DIBS: read reference {reference}. The customer has their confirmation. You can close this window.")
                        time.sleep(4)
                        return "confirmed"
                    page.evaluate(BANNER_JS, f"DIBS: this looks confirmed, but no reference was found. Text: booked {booking_id} <reference>")
                    time.sleep(4)
                    return "no_reference"
                time.sleep(1)
            return "timeout"
        finally:
            try:
                browser.close()
            except Exception:
                pass


def launch(booking_id: int, venue: dict, slot_url: str, party_size: int, total: str, customer: dict) -> None:
    """Start the window in the background. On a reference, the booking is closed as booked."""
    def on_reference(reference: str) -> None:
        from . import db, ops
        from .catalog import Catalog
        from .channels.imessage import send_to_chat, notify_operator

        conn = db.connect()  # this runs in its own thread
        result = ops.complete(conn, Catalog.load(), send_to_chat, booking_id, "booked", f"Ref {reference}")
        conn.close()
        notify_operator(f"[dibs] #{booking_id} confirmed from the page (ref {reference}). {result}")

    def work() -> None:
        try:
            ended = run_window(booking_id, venue, slot_url, party_size, total, customer, on_reference)
        except Exception as exc:
            ended = f"error: {type(exc).__name__}"
        print(f"supervised #{booking_id}: window ended ({ended})")

    threading.Thread(target=work, daemon=True).start()
