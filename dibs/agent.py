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
from .tools import AFFIRMATIVE, ToolContext, confirm_booking, run_tool, schemas

SYSTEM = """You are Dibs, a texting concierge for experiences: bowling, golf, driving ranges, mini golf, laser tag, VR, escape rooms, arcades, climbing, karting, and ticketed events (concerts, festivals, sport).
You work anywhere in the world. Your home city is {city}: there you have a hand-checked venue list, live slots and payments. Elsewhere you find venues and events near the user and send the venue's booking page; live slots exist only where a venue's booking system is one you can read. Never tell a user you only cover {city}.
You find the best slot, the best off-peak deal, and get it booked. For ticketed events you find the event, give the ticket link, and set alerts. Nothing else: politely decline unrelated requests.

Now, where this user is: {now} ({tz}).
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
- If the slot they want is not open, or they want a lower price, offer an alert (create_alert). Never promise to watch a venue without creating one. When an alert finds a slot, the system texts the booking ready for a YES.
- "Every Saturday morning" or "each week": create_alert with every_week=true.
- "Just book it without asking" or "auto-book up to $50": set_auto_book. "Stop auto-book": set_auto_book(0). Never claim auto-book is on unless the saved details below show auto_book_cents above 0.
- Ticketed events are city-wide: do not ask for a suburb or travel distance for them. They are searched around where the user is; if you do not know which city they are in, ask once. If the user names another city, pass it as city. Use find_events. If nothing is in the dates asked, offer the next ones it returns. For a general ask ("any concerts this month?") set kind and the dates and leave keyword empty; keyword is only for a name. List up to 5, one line each: name, date, venue. You never buy tickets and never join queues: you send the official link and set alerts. If tickets are not on sale yet, offer an on-sale alert. If they follow an artist or team, offer a new-show alert. If an event is sold out, say you cannot watch resale sites yet and suggest the official resale page of the ticket seller.
- Memory: save lasting facts with note (who they go with, what they liked, habits). Use remember only for name, usual_party_size and budget. Use search_memory when they refer to something from before ("my usual", "that place").
- Write times the way people text them (4pm, 4:02pm), never 16:02. Write activity names in plain words (mini golf, not mini_golf).
- Write like a friend texting: short lines, no markdown, no headings, no emoji spam. Under 600 characters.
{group_note}"""

GOLF = """
GOLF MODE. Right now Dibs books golf and nothing else: tee times at public courses, par 3 courses and driving ranges.
- If asked for another activity or for event tickets, say Dibs does golf for now and other experiences come later. Do not book them.
- When they did not name a course, offer up to 3 tee times at different courses, nearest first, one line each: course, distance, time, price a player. When they named the course, go straight to propose_booking.
- A tee time takes 1 to 4 players. Ask how many players if you do not know. Ask 9 or 18 holes only when the course offers both: the rate names say which is which, and you pass the rate you pick to propose_booking.
- Twilight and weekday rates are the cheap ones: when one fits the time asked, offer it.
- "My usual Saturday game" is a weekly booking: create_alert with every_week=true.
- When they pick a tee time more than a few hours away, you may add the forecast in a few words (get_weather). Do not call it for every option.
- Talk like someone who plays: tee time, round, nine, eighteen, twilight. Keep it short.
"""

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
    prefs = db.get_prefs(conn, handle)
    user_tz = (prefs.get("location") or {}).get("tz") or config.TIMEZONE
    now = now or datetime.now(ZoneInfo(user_tz))
    past = db.history(conn, conv_id, config.HISTORY_TURNS)
    if "maps" in text.lower() or text.startswith("Shared location:"):  # a location pin or maps link
        coords = geo.extract_coords(text)
        if coords:
            user_tz = geo.timezone_at(*coords) or config.TIMEZONE
            db.set_pref(conn, handle, "location", {"name": "shared location", "lat": coords[0], "lon": coords[1], "tz": user_tz})
            prefs = db.get_prefs(conn, handle)
            now = now.astimezone(ZoneInfo(user_tz))
    db.add_message(conn, conv_id, handle, "user", text)

    system = SYSTEM.format(
        city=config.CITY, now=now.strftime("%A %d %B %Y %I:%M%p"), tz=user_tz, handle=handle,
        prefs=json.dumps(prefs) if prefs else "none yet", pending=_pending(conn, catalog, conv_id), memory=memory.context(handle),
        group_note=GROUP_NOTE if is_group else "",
    ) + (GOLF if config.FOCUS == "golf" else "")
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
    pending_auto = (db.kv_get(conn, f"pending_autobook:{conv_id}") or "").partition("|")
    if pending_auto[2] and AFFIRMATIVE.search(text) and pending_auto[2] in db.last_assistant_message(conn, conv_id):
        db.set_pref(conn, handle, "auto_book_cents", int(pending_auto[0]))
        db.kv_set(conn, f"pending_autobook:{conv_id}", "")
        reply = (f"Auto-book is on, up to ${int(pending_auto[0]) / 100:.2f} a booking. "
                 "I'll tell you every time I use it. Text \"stop auto-book\" to turn it off.")
        db.add_message(conn, conv_id, "dibs", "assistant", reply)
        return reply
    if open_proposal and AFFIRMATIVE.search(text):
        result = confirm_booking(ctx, open_proposal["id"])
        if "error" not in result:
            reply = executors.result_text(result, catalog.venues[open_proposal["venue_id"]], open_proposal)
            db.add_message(conn, conv_id, "dibs", "assistant", reply)
            return reply
    for _ in range(config.MAX_TOOL_ROUNDS):
        try:
            msg = llm.chat(messages, schemas())
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
            if call["function"]["name"] in ("propose_booking", "set_auto_book") and '"shown_to_user"' in result:
                reply = json.loads(result)["shown_to_user"]  # the user sees the code's summary, word for word
        if reply:
            break
    if not reply:  # the model kept calling tools: make it answer with what it has, without tools
        try:
            messages.append({"role": "user", "content": "(system) Stop using tools. Answer the user now in one short text with what you "
                                                        "know. If you are not sure what they mean, ask one short question."})
            reply = (llm.chat(messages, []).get("content") or "").strip()
        except LLMUnavailable:
            reply = ""
    if not reply:
        reply = "Sorry, I lost track there. What would you like to book?"
    db.add_message(conn, conv_id, "dibs", "assistant", reply)
    return reply
