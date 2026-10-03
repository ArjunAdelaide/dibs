"""Tools the agent can call. Every fact the agent quotes must come from one of these.

The money guard lives in confirm_booking: it refuses unless the user's own latest
message is an explicit yes, so the model cannot book on its own initiative.
"""

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from . import alerts, config, db, events, executors, geo, memory, payments
from .catalog import Catalog, deal_applies
from .connectors import has_connector, slots_for

_YES = r"(yes|yep|yeah|yup|y|ok|okay|sure|confirm|confirmed|go ahead|do it|book it|lock it in|sounds good|👍)"
# The whole message must be a yes. "ok what about 5pm" or "yes but 3 people" is not a yes.
AFFIRMATIVE = re.compile(rf"^\s*{_YES}([\s,]+({_YES}|please|thanks|thank you|mate))*[\s.!]*$", re.IGNORECASE)
PREF_KEYS = {"name", "usual_party_size", "budget", "max_travel_km"}
DEFAULT_TRAVEL_KM = 15
MAX_LIVE_LOOKUPS = 6  # venue sites we read for one suggestion


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    catalog: Catalog
    handle: str
    conv_id: str
    last_user_text: str
    now: datetime
    notify_operator: Callable[[str], None]
    llm: object = None
    send_later: Callable[[str], None] | None = None  # texts this chat after the turn ends; None = no background work


_one_browser = threading.Lock()  # one headless browser at a time: this runs on a laptop


def _venue_summary(ctx: ToolContext, venue: dict) -> dict:
    return {
        "venue_id": venue["id"],
        "name": venue["name"],
        "categories": venue.get("categories", []),
        "area": venue.get("area"),
        "live_availability": has_connector(venue),
        "payment": venue.get("payment", "unknown"),
        "has_deals": bool(ctx.catalog.deals_for(venue["id"])),
        "verified": bool(venue.get("verified")),
    }


def _origin(ctx: ToolContext, near: str | None) -> dict | None:
    """Where to measure distance from: a place named in this request, else the user's saved location."""
    if near:
        try:
            return geo.geocode(near)
        except Exception:
            return None
    return db.get_prefs(ctx.conn, ctx.handle).get("location")


def search_venues(ctx: ToolContext, category: str | None = None, near: str | None = None, query: str | None = None) -> dict:
    found = ctx.catalog.search(category=category, query=query, limit=None)
    origin = _origin(ctx, near)
    rows = []
    for venue in found:
        row = _venue_summary(ctx, venue)
        if origin and venue.get("lat") is not None:
            row["distance_km"] = round(geo.haversine_km(origin["lat"], origin["lon"], venue["lat"], venue["lon"]), 1)
        rows.append(row)
    if origin:  # nearest first; venues without coordinates last
        rows.sort(key=lambda r: r.get("distance_km", 9999))
    out = {"venues": rows[:6], "count": len(rows)}
    out["sorted_by"] = f"distance from {origin['name']}" if origin else "no location known: ask where the user is, then call set_location"
    return out


def _site_report(venue: dict, when: str, res) -> str:
    link = venue["booking_url"]
    if res.status == "done":
        price = res.result.get("price")
        price = f" ({price})" if price and "not shown" not in str(price).lower() else ""
        return (f"I can see {', '.join(res.result['times'][:5])} open at {venue['name']} on {when}{price}. "
                f"There may be more times on their page, where you also book and pay: {link}")
    if res.status == "blocked":
        return f"{venue['name']}'s site has a bot check, so I can't read their times. You can check here: {link}"
    if res.status == "reached_payment":
        return f"{venue['name']}'s site went straight to payment, so I stopped. You can pick a time here: {link}"
    return f"I couldn't read {venue['name']}'s times this time. Their booking page: {link}"


def check_site(ctx: ToolContext, venue_id: str, day: str, around_time: str = "16:00", party_size: int = 2) -> dict:
    """Read open times from a venue's own booking site with the browser agent (slow: a minute or two)."""
    from . import browser

    venue = ctx.catalog.venues.get(venue_id)
    if not venue:
        return {"error": f"unknown venue_id {venue_id}"}
    if has_connector(venue):
        return {"error": "this venue has live_availability: use check_availability, it is instant"}
    if not venue.get("booking_url"):
        return {"error": "no booking site is on file for this venue"}
    try:
        the_day = date.fromisoformat(day)
    except ValueError:
        return {"error": "day must be YYYY-MM-DD"}
    when = f"{the_day:%A %-d %B %Y}"
    goal = (f"Find the open times at {venue['name']} for {party_size} people on {when}, close to {around_time}. "
            "Report the open times near that time and the price. Stop before any personal details or payment.")

    def work() -> str:
        with _one_browser:
            try:
                # the browser agent gets its own (stronger) model when one is set
                browser_llm = ctx.llm.job("browser") if hasattr(ctx.llm, "job") else ctx.llm
                res = browser.browse(venue["booking_url"], goal, browser_llm)
            except Exception as exc:
                res = browser.BrowseResult(status="failed", result={"reason": type(exc).__name__})
        return _site_report(venue, f"{the_day:%a %-d %b}", res)

    if ctx.send_later is None:  # channels that cannot text back later get the answer in this turn
        return {"report": work()}

    def background() -> None:
        text = work()
        conn = db.connect()  # a thread needs its own database connection
        db.add_message(conn, ctx.conv_id, "dibs", "assistant", text)
        conn.close()
        ctx.send_later(text)

    threading.Thread(target=background, daemon=True).start()
    return {"started": True, "next": "Say you are checking their site now and will text back in a couple of minutes. Do not guess times."}


def suggest_ideas(ctx: ToolContext, day: str, around_time: str = "16:00", party_size: int = 2, max_km: float | None = None,
                  category: str | None = None) -> dict:
    """Venues with a real open slot near a time: one call does the search and the availability check."""
    prefs = db.get_prefs(ctx.conn, ctx.handle)
    origin = prefs.get("location")
    if not origin and not category:
        return {"error": "no location yet: ask where they are and how far they will travel, then call set_location"}
    try:
        max_km = float(max_km or prefs.get("max_travel_km") or DEFAULT_TRAVEL_KM)
        target = int(around_time[:2]) * 60 + int(around_time[3:5])
        the_day = date.fromisoformat(day)
    except ValueError:
        return {"error": "day must be YYYY-MM-DD and around_time HH:MM"}

    nearby = []
    for venue in ctx.catalog.venues.values():
        if category and category.lower() not in [c.lower() for c in venue.get("categories", [])]:
            continue
        if not origin:  # an activity was named but we do not know where they are: search the whole city
            nearby.append((0.0, venue))
            continue
        if venue.get("lat") is None:
            continue
        km = geo.haversine_km(origin["lat"], origin["lon"], venue["lat"], venue["lon"])
        if km <= max_km or (category and has_connector(venue)):  # a bookable venue of the asked kind is worth the trip
            nearby.append((km, venue))
    nearby.sort(key=lambda pair: (not has_connector(pair[1]), pair[0]))  # live venues first, then nearest

    ideas, lookups = [], 0
    for km, venue in nearby:
        idea = {"venue_id": venue["id"], "name": venue["name"], "kind": (venue.get("categories") or ["other"])[0], "live": False}
        if origin:
            idea["distance_km"] = round(km, 1)
        if has_connector(venue) and lookups < MAX_LIVE_LOOKUPS:
            lookups += 1
            try:
                slots = [s for s in slots_for(venue, the_day) if s.fits(party_size)]
            except Exception:
                slots = []
            near = [s for s in slots if abs(int(s.time[:2]) * 60 + int(s.time[3:]) - target) <= 90]
            if not near:
                continue  # live feed shows nothing near that time: do not suggest it
            best = min(near, key=lambda s: abs(int(s.time[:2]) * 60 + int(s.time[3:]) - target))
            cheapest = min(best.rates, key=lambda r: r.price)
            idea.update(live=True, open_slot=best.time, price_per_person=cheapest.price, rate=cheapest.name,
                        other_open_times=[s.time for s in sorted(near, key=lambda s: s.time) if s.time != best.time][:4])
        ideas.append(idea)

    # One idea per kind first, so the user sees different things to do.
    seen, varied, rest = set(), [], []
    for idea in ideas:
        (rest if idea["kind"] in seen else varied).append(idea)
        seen.add(idea["kind"])
    picked = (varied + rest)[:6]
    return {"ideas": picked, "within_km": max_km, "from": origin["name"] if origin else "anywhere in the city (location unknown)",
            "note": "live=true ideas have a real open slot. For live=false you cannot see times: say the venue confirms."
                    if picked else "Nothing within that distance. Offer to look further."}


def set_location(ctx: ToolContext, place: str) -> dict:
    try:
        spot = geo.geocode(place)
    except Exception as exc:
        return {"error": f"location lookup failed ({type(exc).__name__}); ask for a suburb name"}
    if not spot:
        return {"error": f"could not find '{place}'; ask for a suburb name"}
    db.set_pref(ctx.conn, ctx.handle, "location", spot)
    memory.note(ctx.handle, f"Location given: {spot['name']}", ctx.now)
    return {"saved": spot, "next": "search_venues now sorts by distance from here"}


def get_venue(ctx: ToolContext, venue_id: str) -> dict:
    venue = ctx.catalog.venues.get(venue_id)
    if not venue:
        return {"error": f"unknown venue_id {venue_id}"}
    fields = ["id", "name", "categories", "area", "address", "phone", "website", "booking_url",
              "booking_platform", "payment", "hours", "price_notes", "off_peak_notes", "verified"]
    detail = {k: venue.get(k) for k in fields}
    detail["live_availability"] = has_connector(venue)
    detail["deals"] = [
        {"deal_id": d["id"], "title": d["title"], "price": d.get("price"), "conditions": d.get("conditions", {}),
         "expires": d.get("expires"), "source_url": d.get("source_url")}
        for d in ctx.catalog.deals_for(venue_id)
    ]
    return detail


def _parse_local(ctx: ToolContext, starts_at: str) -> datetime:
    dt = datetime.fromisoformat(starts_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(config.TIMEZONE))
    return dt


def find_deals(ctx: ToolContext, starts_at: str, party_size: int, category: str | None = None) -> dict:
    when = _parse_local(ctx, starts_at)
    matches, near_misses = [], []
    for deal in ctx.catalog.deals:
        venue = ctx.catalog.venues.get(deal["venue_id"])
        if not venue:
            continue
        if category and category.lower() not in [c.lower() for c in venue.get("categories", [])]:
            continue
        ok, reason = deal_applies(deal, when, party_size, today=ctx.now.date())
        row = {"deal_id": deal["id"], "venue_id": venue["id"], "venue": venue["name"], "title": deal["title"], "price": deal.get("price")}
        if ok:
            matches.append(row)
        elif not reason.startswith("expired"):
            near_misses.append({**row, "why_not": reason})
    return {"applies": matches, "near_misses": near_misses[:5]}


def check_availability(ctx: ToolContext, venue_id: str, day: str, around_time: str = "16:00", party_size: int = 2) -> dict:
    venue = ctx.catalog.venues.get(venue_id)
    if not venue:
        return {"error": f"unknown venue_id {venue_id}"}
    if not has_connector(venue):
        return {"live": False, "note": "No live feed for this venue. You can still propose a booking; the venue confirms the time."}
    try:
        slots = slots_for(venue, date.fromisoformat(day))
    except Exception as exc:
        return {"live": False, "note": f"Live feed failed ({type(exc).__name__}); treat availability as unknown."}
    fits = [s for s in slots if s.fits(party_size)]
    target = int(around_time[:2]) * 60 + int(around_time[3:5])
    fits.sort(key=lambda s: abs(int(s.time[:2]) * 60 + int(s.time[3:]) - target))
    nearest = sorted(fits[:5], key=lambda s: s.time)
    return {
        "live": True, "day": day, "open_slots_that_day": len(slots),
        "nearest": [{"time": s.time, "rates": [{"rate": r.name, "price_per_person": r.price} for r in s.rates]} for s in nearest],
        "note": "Rates with conditions in their name (group size, day) only apply when the party meets them.",
    }


def remember(ctx: ToolContext, key: str, value: str) -> dict:
    if key not in PREF_KEYS:
        return {"error": f"key must be one of {sorted(PREF_KEYS)}; use note for anything else"}
    db.set_pref(ctx.conn, ctx.handle, key, value)
    memory.note(ctx.handle, f"{key}: {value}", ctx.now)
    return {"saved": {key: value}}


def note(ctx: ToolContext, fact: str) -> dict:
    memory.note(ctx.handle, fact, ctx.now)
    return {"noted": True}


def search_memory(ctx: ToolContext, query: str) -> dict:
    return {"matches": memory.search(ctx.handle, query)}


def create_alert(ctx: ToolContext, venue_id: str, time_from: str, time_to: str, party_size: int,
                 day: str | None = None, weekday: str | None = None, max_price: float | None = None) -> dict:
    venue = ctx.catalog.venues.get(venue_id)
    if not venue:
        return {"error": f"unknown venue_id {venue_id}"}
    if not has_connector(venue):
        return {"error": "alerts only work for venues with live_availability"}
    weekday = weekday.lower()[:3] if weekday else None
    if bool(day) == bool(weekday) or (weekday and weekday not in alerts.DAYS):
        return {"error": "give either day (YYYY-MM-DD) or weekday (mon..sun)"}
    days = alerts.dates_for(day, weekday, ctx.now.date())
    if not days:
        return {"error": "that day is in the past"}
    try:
        open_now = alerts.find_matches(venue, days, time_from, time_to, party_size, max_price, ctx.now)
    except Exception:
        open_now = []
    if open_now:
        return {"created": False, "open_now": open_now[:3], "next": "It is open already: offer these slots instead of an alert."}
    alert_id = alerts.create(ctx.conn, ctx.conv_id, ctx.handle, venue_id, day, weekday, time_from, time_to, party_size, max_price)
    return {"created": True, "alert_id": f"slot-{alert_id}",
            "next": f"Tell the user you will text them when it opens. It is checked every {config.ALERT_INTERVAL_MINUTES} minutes."}


def find_events(ctx: ToolContext, keyword: str | None = None, from_day: str | None = None, to_day: str | None = None,
                kind: str | None = None) -> dict:
    try:
        start = date.fromisoformat(from_day) if from_day else ctx.now.date()
        end = date.fromisoformat(to_day) if to_day else None
    except ValueError:
        return {"error": "days must be YYYY-MM-DD"}
    if kind and kind not in events.KINDS:
        return {"error": f"kind must be one of {sorted(events.KINDS)}"}
    found = events.search(keyword, start, end, kind=kind)
    keep = ("event_id", "name", "start_date", "venue", "status", "onsale_at", "presales", "price_from", "url", "more_dates", "ticket_options")

    def slim(rows: list[dict]) -> list[dict]:
        return [{k: e[k] for k in keep if e.get(k) not in (None, [], "")} for e in rows]

    out = {"events": slim(found[:10]), "total_found": len(found)}
    if not found and end:  # nothing in the window: show what comes next instead of a dead end
        out["next_after_those_dates"] = slim(events.search(keyword, end, None, kind=kind)[:3])
    if not config.TICKETMASTER_API_KEY:
        out["note"] = "Only the hand-kept calendar was searched (no Ticketmaster key). Say you may not see every concert yet."
    return out


def create_event_alert(ctx: ToolContext, kind: str, event_id: str | None = None, keyword: str | None = None) -> dict:
    if kind == "onsale":
        event = events.get_event(event_id) if event_id else None
        if not event:
            return {"error": "give an event_id from find_events"}
        sale = events.next_sale(event, ctx.now)
        if not sale and event.get("status") == "onsale":
            return {"created": False, "on_sale_now": True, "url": event.get("url"), "next": "Tickets are on sale already: send the link."}
        alert_id = events.create_alert(ctx.conn, ctx.conv_id, ctx.handle, "onsale", event=event,
                                       fire_at=sale and sale[1], label=sale[0] if sale else "")
        when = f"{sale[0]} opens {sale[1]}" if sale else "no on-sale date announced yet; you check a few times a day and text when it is known or on sale"
        return {"created": True, "alert_id": f"event-{alert_id}", "next": f"Tell the user: {when}."}
    if kind == "new_show":
        if not keyword:
            return {"error": "give the artist, team or festival name as keyword"}
        if not config.TICKETMASTER_API_KEY:
            return {"error": "new-show alerts need the Ticketmaster key, which is not set up yet. Say this feature is not on yet."}
        seen = [e["event_id"] for e in events.search(keyword, ctx.now.date())]
        alert_id = events.create_alert(ctx.conn, ctx.conv_id, ctx.handle, "new_show", keyword=keyword, seen=seen)
        return {"created": True, "alert_id": f"event-{alert_id}", "already_listed": len(seen),
                "next": "Tell the user you will text when a new show for that name appears."}
    return {"error": "kind must be onsale or new_show"}


def list_alerts(ctx: ToolContext) -> dict:
    slots = ctx.conn.execute("SELECT * FROM alerts WHERE conv_id = ? AND status = 'active'", (ctx.conv_id,)).fetchall()
    shows = ctx.conn.execute("SELECT * FROM event_alerts WHERE conv_id = ? AND status = 'active'", (ctx.conv_id,)).fetchall()
    return {"alerts": [f"slot-{alerts.describe(r, ctx.catalog).lstrip('#')}" for r in slots] + [events.describe(r) for r in shows]}


def cancel_alert(ctx: ToolContext, alert_id: str) -> dict:
    kind, _, number = str(alert_id).partition("-")
    table = {"slot": "alerts", "event": "event_alerts"}.get(kind)
    if not table or not number.isdigit():
        return {"error": "alert_id looks like slot-3 or event-2; call list_alerts"}
    cur = ctx.conn.execute(f"UPDATE {table} SET status = 'cancelled' WHERE id = ? AND conv_id = ? AND status = 'active'",
                           (int(number), ctx.conv_id))
    ctx.conn.commit()
    return {"cancelled": cur.rowcount == 1}


def propose_booking(ctx: ToolContext, venue_id: str, starts_at: str, party_size: int, deal_id: str | None = None,
                    rate: str | None = None, notes: str = "") -> dict:
    venue = ctx.catalog.venues.get(venue_id)
    if not venue:
        return {"error": f"unknown venue_id {venue_id}"}
    when = _parse_local(ctx, starts_at)
    if when <= ctx.now:
        return {"error": "that time is in the past"}
    if party_size < 1 or party_size > 40:
        return {"error": "party_size must be between 1 and 40"}
    deal_note = None
    if deal_id:
        deal = next((d for d in ctx.catalog.deals_for(venue_id) if d["id"] == deal_id), None)
        if not deal:
            return {"error": f"deal {deal_id} is not offered by {venue['name']}"}
        ok, reason = deal_applies(deal, when, party_size, today=ctx.now.date())
        if not ok:
            return {"error": f"deal does not apply: {reason}"}
        deal_note = f"{deal['title']} ({deal.get('price', 'see venue')})"
    if has_connector(venue):  # never propose a slot the live feed does not show
        slot, found = executors.find_slot(venue, when, rate, party_size)
        if not slot:
            return {"error": "that time, rate or group size is not open on the live feed; call check_availability and offer real slots"}
        deal_note = None
    # One open proposal per conversation keeps "yes" unambiguous.
    ctx.conn.execute("UPDATE proposals SET status = 'superseded' WHERE conv_id = ? AND status = 'pending'", (ctx.conv_id,))
    found = found if has_connector(venue) else None
    card = None
    if found and payments.enabled():  # name the card that will be used, so there are no surprises
        try:
            method = payments.saved_method(ctx.conn, ctx.handle)
            card = method[1] if method else None
        except payments.PaymentError:
            card = None
    shown = executors.proposal_text(venue, when, party_size, found, deal_note, card)
    cur = ctx.conn.execute(
        "INSERT INTO proposals (conv_id, handle, venue_id, starts_at, party_size, deal_id, rate, shown, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ctx.conv_id, ctx.handle, venue_id, when.isoformat(), party_size, deal_id, found.name if found else rate, shown, notes, ctx.now.isoformat()),
    )
    ctx.conn.commit()
    return {"proposal_id": cur.lastrowid, "shown_to_user": shown,
            "next": "The system sends this exact text to the user. Any change (people, time, venue) needs a new propose_booking call."}


def confirm_booking(ctx: ToolContext, proposal_id: int) -> dict:
    if not AFFIRMATIVE.search(ctx.last_user_text):
        return {"error": "user has not said yes in their latest message; ask them to reply YES"}
    row = ctx.conn.execute(
        "SELECT * FROM proposals WHERE id = ? AND conv_id = ? AND status = 'pending'", (proposal_id, ctx.conv_id)
    ).fetchone()
    if not row:
        return {"error": "no pending proposal with that id in this conversation"}
    created = datetime.fromisoformat(row["created_at"])
    if ctx.now - created > timedelta(minutes=config.PROPOSAL_TTL_MINUTES):
        ctx.conn.execute("UPDATE proposals SET status = 'expired' WHERE id = ?", (proposal_id,))
        ctx.conn.commit()
        return {"error": "proposal expired; propose it again"}
    # The user may only approve what the system itself showed them, word for word.
    if not row["shown"] or row["shown"] not in db.last_assistant_message(ctx.conn, ctx.conv_id):
        return {"error": "the user has not been shown this exact booking; call propose_booking again with the details they want"}
    return executors.execute(ctx, row, ctx.catalog.venues[row["venue_id"]])


def cancel_booking(ctx: ToolContext) -> dict:
    """Cancel the user's latest booking in this chat and give back any held or paid amount."""
    from . import ops

    return ops.cancel_by_user(ctx.conn, ctx.catalog, ctx.handle, ctx.conv_id, ctx.notify_operator)


def my_bookings(ctx: ToolContext) -> dict:
    rows = ctx.conn.execute(
        "SELECT b.id, b.status, b.reference, b.amount_cents, p.venue_id, p.starts_at, p.party_size FROM bookings b "
        "JOIN proposals p ON p.id = b.proposal_id WHERE p.conv_id = ? AND p.handle = ? ORDER BY b.id DESC LIMIT 5",
        (ctx.conv_id, ctx.handle)).fetchall()
    words = {"paid_needs_human": "being completed (amount held, not charged)", "needs_human": "being completed",
             "requested": "requested from the venue", "link_sent": "link sent, finish on the venue page", "booked": "confirmed"}
    return {"bookings": [{"venue": ctx.catalog.venues.get(r["venue_id"], {}).get("name", r["venue_id"]), "when": r["starts_at"][:16],
                          "people": r["party_size"], "status": words.get(r["status"], r["status"]), "reference": r["reference"] or None,
                          "amount": f"${r['amount_cents'] / 100:.2f}" if r["amount_cents"] else None} for r in rows]}


def remove_card(ctx: ToolContext) -> dict:
    if not payments.enabled():
        return {"error": "payments are not turned on"}
    try:
        return {"removed": payments.remove_saved_methods(ctx.conn, ctx.handle)}
    except payments.PaymentError as exc:
        return {"error": str(exc)}


def cancel_proposal(ctx: ToolContext, proposal_id: int) -> dict:
    cur = ctx.conn.execute(
        "UPDATE proposals SET status = 'cancelled' WHERE id = ? AND conv_id = ? AND status = 'pending'", (proposal_id, ctx.conv_id)
    )
    ctx.conn.commit()
    return {"cancelled": cur.rowcount == 1}


HANDLERS = {
    "search_venues": search_venues,
    "get_venue": get_venue,
    "find_deals": find_deals,
    "check_availability": check_availability,
    "check_site": check_site,
    "suggest_ideas": suggest_ideas,
    "set_location": set_location,
    "remember": remember,
    "note": note,
    "search_memory": search_memory,
    "create_alert": create_alert,
    "find_events": find_events,
    "create_event_alert": create_event_alert,
    "list_alerts": list_alerts,
    "cancel_alert": cancel_alert,
    "propose_booking": propose_booking,
    "confirm_booking": confirm_booking,
    "cancel_proposal": cancel_proposal,
    "cancel_booking": cancel_booking,
    "my_bookings": my_bookings,
    "remove_card": remove_card,
}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


CATEGORY = {"type": "string", "description": "e.g. bowling, golf, driving_range, mini_golf, laser_tag, vr, escape_room, arcade"}
STARTS_AT = {"type": "string", "description": "Local start time, ISO 8601, e.g. 2026-10-07T16:00"}

SCHEMAS = [
    _fn("search_venues", "Search the venue catalog, nearest first when a location is known. Only venues returned here exist for you.",
        {"category": CATEGORY, "near": {"type": "string", "description": "Suburb or place named in this request. Leave empty to use the saved location."},
         "query": {"type": "string", "description": "Free text, e.g. a venue name"}}, []),
    _fn("get_venue", "Full details for one venue, including its current deals and booking route.",
        {"venue_id": {"type": "string"}}, ["venue_id"]),
    _fn("find_deals", "Deals that actually apply at a given time and party size, plus near misses (e.g. a cheaper slot an hour earlier).",
        {"starts_at": STARTS_AT, "party_size": {"type": "integer"}, "category": CATEGORY}, ["starts_at", "party_size"]),
    _fn("check_availability", "Live open slots and prices near a time, for venues with live_availability=true.",
        {"venue_id": {"type": "string"}, "day": {"type": "string", "description": "Local date, YYYY-MM-DD"},
         "around_time": {"type": "string", "description": "HH:MM, 24h"}, "party_size": {"type": "integer"}}, ["venue_id", "day"]),
    _fn("check_site", "ONLY when the user asks about one venue by name and it has no live_availability: open its booking site in a "
        "browser and read the open times. Slow (1 to 2 minutes); the answer is texted to the user afterwards. Never use it to browse options.",
        {"venue_id": {"type": "string"}, "day": {"type": "string", "description": "YYYY-MM-DD"},
         "around_time": {"type": "string", "description": "HH:MM, 24h"}, "party_size": {"type": "integer"}}, ["venue_id", "day"]),
    _fn("suggest_ideas", "The fastest way to find something bookable: venues near the user with a real open slot near a time, in one call. "
        "Use it for open requests (no category) and for a named activity (set category). You can go straight to propose_booking "
        "with the venue_id, open_slot and rate it returns.",
        {"day": {"type": "string", "description": "Local date, YYYY-MM-DD"}, "around_time": {"type": "string", "description": "HH:MM, 24h"},
         "party_size": {"type": "integer"}, "max_km": {"type": "number", "description": "How far they will travel"},
         "category": CATEGORY}, ["day"]),
    _fn("set_location", "Save where the user is: a suburb, an address, or 'lat,lon'.",
        {"place": {"type": "string"}}, ["place"]),
    _fn("remember", "Save one of the fixed facts the booking forms need.",
        {"key": {"type": "string", "enum": sorted(PREF_KEYS)}, "value": {"type": "string"}}, ["key", "value"]),
    _fn("note", "Write any other lasting fact about the user to memory: likes, friends, habits, venues they enjoyed.",
        {"fact": {"type": "string", "description": "One short sentence"}}, ["fact"]),
    _fn("search_memory", "Keyword search of everything remembered about this user.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("create_alert", "Watch a venue and text the user when a slot in the time window opens (or drops to a price). For venues with live_availability.",
        {"venue_id": {"type": "string"}, "day": {"type": "string", "description": "YYYY-MM-DD, for one date"},
         "weekday": {"type": "string", "description": "mon..sun, for the next such day"},
         "time_from": {"type": "string", "description": "HH:MM, 24h"}, "time_to": {"type": "string", "description": "HH:MM, 24h"},
         "party_size": {"type": "integer"}, "max_price": {"type": "number", "description": "Per person, optional"}},
        ["venue_id", "time_from", "time_to", "party_size"]),
    _fn("find_events", "Concerts, festivals and sport in the city: dates, on-sale and presale times, ticket link.",
        {"keyword": {"type": "string", "description": "A NAME only: an artist, team or festival. Leave empty for a general search."},
         "kind": {"type": "string", "enum": sorted(events.KINDS), "description": "Type of event. Use music for concerts and gigs."},
         "from_day": {"type": "string", "description": "YYYY-MM-DD"}, "to_day": {"type": "string", "description": "YYYY-MM-DD"}}, []),
    _fn("create_event_alert", "Ticket alerts. kind=onsale: text the user just before tickets for one event go on sale (needs event_id). "
        "kind=new_show: text the user when a new show for an artist or team is announced (needs keyword).",
        {"kind": {"type": "string", "enum": ["onsale", "new_show"]}, "event_id": {"type": "string"}, "keyword": {"type": "string"}}, ["kind"]),
    _fn("list_alerts", "The user's active alerts, of both kinds.", {}, []),
    _fn("cancel_alert", "Stop an alert.", {"alert_id": {"type": "string", "description": "As shown by list_alerts, e.g. slot-3 or event-2"}}, ["alert_id"]),
    _fn("propose_booking", "Create a booking proposal for the user to approve. Always do this before confirm_booking.",
        {"venue_id": {"type": "string"}, "starts_at": STARTS_AT, "party_size": {"type": "integer"},
         "deal_id": {"type": "string", "description": "Only if find_deals or get_venue returned it"},
         "rate": {"type": "string", "description": "Exact rate name from check_availability, if the venue has live availability"},
         "notes": {"type": "string"}}, ["venue_id", "starts_at", "party_size"]),
    _fn("confirm_booking", "Book a proposal. Only call when the user's latest message says yes to it.",
        {"proposal_id": {"type": "integer"}}, ["proposal_id"]),
    _fn("cancel_proposal", "Drop a pending proposal the user no longer wants (before they said yes).",
        {"proposal_id": {"type": "integer"}}, ["proposal_id"]),
    _fn("cancel_booking", "The user wants to cancel a booking they already said yes to. Cancels the latest one and returns any held or paid amount.", {}, []),
    _fn("my_bookings", "The user's recent bookings and their status.", {}, []),
    _fn("remove_card", "Delete the user's saved card from Stripe when they ask.", {}, []),
]


def run_tool(ctx: ToolContext, name: str, arguments: str) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        args = json.loads(arguments or "{}")
        return json.dumps(handler(ctx, **args), default=str)
    except Exception as exc:  # report bad arguments back to the model instead of crashing the turn
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
