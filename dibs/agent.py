"""One conversational turn: history + profile in, tool loop, one text reply out."""

import json
import sqlite3
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from . import config, db, geo, memory
from .catalog import Catalog
from .llm import LLM, LLMUnavailable
from . import executors
from .tools import AFFIRMATIVE, SCHEMAS, ToolContext, confirm_booking, run_tool

SYSTEM = """You are Dibs, a texting concierge for experiences in {city}: bowling, golf, driving ranges, mini golf, laser tag, VR, escape rooms, arcades, and ticketed events (concerts, festivals, sport).
You find the best slot, the best off-peak deal, and get it booked. For ticketed events you find the event, give the ticket link, and set alerts. Nothing else: politely decline unrelated requests.

Now: {now} ({tz}).
This user: {handle}. Booking details on file: {prefs}.
What you remember about them:
{memory}
Open proposal in this chat: {pending}.

How you work:
- You only know venues, deals and prices that your tools return. Never invent a venue, price, deal or availability. If the catalog has nothing, say so plainly.
- For venues with live_availability, call check_availability and only offer slots and prices it returns. For other venues you cannot see availability at once. When the user picks such a venue or asks if a time is free there, call check_site (it reads the venue's booking site and texts the answer in a couple of minutes). If check_site is not possible, say the venue will confirm the time.
- After confirm_booking, follow its tell_user note. Only say "booked" when the tool result says the booking is complete. If it returns a booking_link, send that exact link on its own line.
- If confirm_booking returns needs_card, nothing is booked or charged yet: send the setup_link and ask them to reply YES after they save a card. Never ask for card details in the chat, and tell users not to text card numbers.
- "Cancel my booking": call cancel_booking and repeat its tell_user. "What have I booked?": my_bookings. "Remove my card": remove_card.
- A booking needs a name. If you do not have one saved, ask once and save it.
- This is a relaxed text chat, not a form. People start vague ("I want to do something tomorrow around 4"). Never ask them to send everything in one message.
- To give ideas you need four things: the day and rough time, where they are, how far they will travel, and how many people. First use what you remember. Then ask for what is missing in one short friendly message (where and how far go together in one question).
- Be fast: use as few tool calls as you can. suggest_ideas finds venues and real open slots in one call, so use it first, both for open requests and for a named activity (set category). Prefer live=true venues: they can be booked at once.
- When they have not named an activity, offer 3 ideas of different kinds, one line each: what it is, how far, the open time, the price. Then ask which sounds good.
- When they name the activity, day, time and group size ("book mini golf tomorrow at 4 for 2"), find the best live option and go straight to propose_booking in the same turn. Do not ask where they are first.
- Save how far they will travel with remember(max_travel_km) so you do not ask again.
- Offer at most 3 numbered options. If a deal applies at a nearby time (find_deals near_misses), mention the cheaper slot.
- To book: call propose_booking. The system then sends the exact summary to the user and handles their YES. If the user changes anything (people, time, venue), call propose_booking again with the new details: never describe a changed booking in your own words.
- Location: search_venues returns the nearest venues when a location is known. If none is known, ask which suburb they are in (or ask them to share a location pin) and call set_location. Offer 2 or 3 venues, nearest first, and say the distance.
- If the slot they want is not open, or they want a lower price, offer an alert (create_alert). Never promise to watch a venue without creating one.
- Ticketed events are city-wide: do not ask for a suburb or travel distance for them. Use find_events. If nothing is in the dates asked, offer the next ones it returns. For a general ask ("any concerts this month?") set kind and the dates and leave keyword empty; keyword is only for a name. List up to 5, one line each: name, date, venue. You never buy tickets and never join queues: you send the official link and set alerts. If tickets are not on sale yet, offer an on-sale alert. If they follow an artist or team, offer a new-show alert. If an event is sold out, say you cannot watch resale sites yet and suggest the official resale page of the ticket seller.
- Memory: save lasting facts with note (who they go with, what they liked, habits). Use remember only for name, usual_party_size and budget. Use search_memory when they refer to something from before ("my usual", "that place").
- Write times the way people text them (4pm, 4:02pm), never 16:02. Write activity names in plain words (mini golf, not mini_golf).
- Write like a friend texting: short lines, no markdown, no headings, no emoji spam. Under 600 characters.
{group_note}"""

GROUP_NOTE = "- This is a group chat. Messages are prefixed with the sender. Help the group converge on one plan; anyone in the chat can say yes."


def _pending(conn: sqlite3.Connection, catalog: Catalog, conv_id: str) -> str:
    row = conn.execute(
        "SELECT * FROM proposals WHERE conv_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", (conv_id,)
    ).fetchone()
    if not row:
        return "none"
    venue = catalog.venues.get(row["venue_id"], {"name": row["venue_id"]})
    return f"proposal_id={row['id']} {venue['name']} {row['starts_at']} x{row['party_size']} deal={row['deal_id']}"


def run_turn(
    conn: sqlite3.Connection,
    catalog: Catalog,
    llm: LLM,
    conv_id: str,
    handle: str,
    text: str,
    notify_operator: Callable[[str], None],
    is_group: bool = False,
    now: datetime | None = None,
    send_later: Callable[[str], None] | None = None,
) -> str:
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    prefs = db.get_prefs(conn, handle)
    past = db.history(conn, conv_id, config.HISTORY_TURNS)
    if "maps" in text.lower() or text.startswith("Shared location:"):  # a location pin or maps link
        coords = geo.extract_coords(text)
        if coords:
            db.set_pref(conn, handle, "location", {"name": "shared location", "lat": coords[0], "lon": coords[1]})
            prefs = db.get_prefs(conn, handle)
    db.add_message(conn, conv_id, handle, "user", text)

    system = SYSTEM.format(
        city=config.CITY, now=now.strftime("%A %d %B %Y %I:%M%p"), tz=config.TIMEZONE, handle=handle,
        prefs=json.dumps(prefs) if prefs else "none yet", pending=_pending(conn, catalog, conv_id), memory=memory.context(handle),
        group_note=GROUP_NOTE if is_group else "",
    )
    messages: list[dict] = [{"role": "system", "content": system}]
    for row in past:
        content = f"{row['handle']}: {row['content']}" if is_group and row["role"] == "user" else row["content"]
        messages.append({"role": row["role"], "content": content})
    messages.append({"role": "user", "content": f"{handle}: {text}" if is_group else text})

    ctx = ToolContext(conn=conn, catalog=catalog, handle=handle, conv_id=conv_id, last_user_text=text, now=now,
                      notify_operator=notify_operator, llm=llm, send_later=send_later)
    reply = ""
    # Fast path: a plain YES to an open proposal is handled by code. No model call, no chance of a wrong amount.
    open_proposal = conn.execute("SELECT * FROM proposals WHERE conv_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                                 (conv_id,)).fetchone()
    if open_proposal and AFFIRMATIVE.search(text):
        result = confirm_booking(ctx, open_proposal["id"])
        if "error" not in result:
            reply = executors.result_text(result, catalog.venues[open_proposal["venue_id"]], open_proposal)
            db.add_message(conn, conv_id, "dibs", "assistant", reply)
            return reply
    for _ in range(config.MAX_TOOL_ROUNDS):
        try:
            msg = llm.chat(messages, SCHEMAS)
        except LLMUnavailable:
            reply = "I'm having trouble thinking right now. Text me again in a minute."
            break
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = (msg.get("content") or "").strip()
            break
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for call in calls:
            result = run_tool(ctx, call["function"]["name"], call["function"].get("arguments", "{}"))
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            if call["function"]["name"] == "propose_booking" and '"shown_to_user"' in result:
                reply = json.loads(result)["shown_to_user"]  # the user sees the code's summary, word for word
        if reply:
            break
    if not reply:
        reply = "Sorry, I got tangled up there. Can you say that again?"
    db.add_message(conn, conv_id, "dibs", "assistant", reply)
    return reply
