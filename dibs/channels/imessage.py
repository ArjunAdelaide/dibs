"""Free iMessage channel: this Mac's Messages app is the gateway.

Reads new messages from ~/Library/Messages/chat.db (read-only) and replies through
Messages.app with AppleScript. Needs Full Disk Access (to read chat.db) and
Automation permission for Messages, both granted by you in System Settings.

Safety: only handles in ALLOWLIST get replies, group chats need the trigger word,
history before the first run is never answered, and DRY_RUN=1 prints instead of sending.
"""

import re
import sqlite3
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import alerts, config, db, events, memory, ops, places
from ..agent import run_turn
from ..catalog import Catalog
from .. import llm as models

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
GROUP_STYLE = 43

QUERY = """
SELECT m.ROWID AS rowid, m.text, m.attributedBody, h.id AS handle, c.guid AS chat_guid, c.style AS style
FROM message m
JOIN handle h ON h.ROWID = m.handle_id
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.ROWID > ? AND m.is_from_me = 0
ORDER BY m.ROWID
"""

SEND_TO_CHAT = """on run argv
    tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)
end run"""

SEND_TO_HANDLE = """on run argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        send (item 1 of argv) to participant (item 2 of argv) of svc
    end tell
end run"""


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Newer macOS stores message text in an NSAttributedString blob instead of `text`."""
    if not blob:
        return None
    start = blob.find(b"NSString")
    if start == -1:
        return None
    i = start + len(b"NSString") + 5
    length = blob[i]
    i += 1
    if length == 0x81:
        length = int.from_bytes(blob[i:i + 2], "little")
        i += 2
    elif length == 0x82:
        length = int.from_bytes(blob[i:i + 3], "little")
        i += 3
    return blob[i:i + length].decode("utf-8", errors="replace")


ATTACHMENTS = """
SELECT a.filename FROM attachment a
JOIN message_attachment_join j ON j.attachment_id = a.ROWID
WHERE j.message_id = ?
"""


def shared_location(src: sqlite3.Connection, rowid: int) -> str | None:
    """A location pin arrives as a .loc.vcf attachment that holds a maps link."""
    for (filename,) in src.execute(ATTACHMENTS, (rowid,)).fetchall():
        if filename and filename.endswith(".loc.vcf"):
            try:
                card = Path(filename).expanduser().read_text(errors="replace")
            except OSError:
                continue
            match = re.search(r"ll=(-?\d+\.\d+)\\?,(-?\d+\.\d+)", card)
            if match:
                return f"Shared location: {match.group(1)},{match.group(2)}"
    return None


LOG_PATH = config.DB_PATH.parent / "bridge.log"


def log(text: str) -> None:
    """Print, and keep a copy in data/bridge.log so a problem can be looked up later."""
    print(text, flush=True)
    try:
        with LOG_PATH.open("a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def _osascript(script: str, *args: str) -> None:
    subprocess.run(["osascript", "-e", script, *args], check=True, capture_output=True, timeout=30)


def send_to_chat(chat_guid: str, text: str) -> None:
    if config.DRY_RUN:
        print(f"[dry-run -> {chat_guid}] {text}")
        return
    _osascript(SEND_TO_CHAT, text, chat_guid)


def send_to_handle(handle: str, text: str) -> None:
    if config.DRY_RUN or not handle:
        print(f"[dry-run -> {handle or 'operator'}] {text}")
        return
    _osascript(SEND_TO_HANDLE, text, handle)


def notify_operator(text: str) -> None:
    send_to_handle(config.OPERATOR_HANDLE, text)


SENDER = re.compile(r"^(\+\d{8,15}|[^@\s]+@[^@\s]+\.[a-z]{2,})$", re.I)  # a real phone number or email, not a short code


def allowed(handle: str) -> bool:
    if handle in config.BLOCKLIST:
        return False
    if config.OPEN_ACCESS:
        return bool(SENDER.match(handle))
    return handle in config.ALLOWLIST


def gate(conn: sqlite3.Connection, handle: str, text: str) -> str | None:
    """Opt-out and rate limits. Returns a reply to send instead of running the agent, '' for silence, or None to go on."""
    word = text.strip().lower()
    if word in ("stop", "unsubscribe"):
        db.kv_set(conn, f"stopped:{handle}", "1")
        conn.execute("UPDATE alerts SET status = 'cancelled' WHERE handle = ? AND status = 'active'", (handle,))
        conn.execute("UPDATE event_alerts SET status = 'cancelled' WHERE handle = ? AND status = 'active'", (handle,))
        conn.commit()
        return "Done. You won't hear from Dibs again, and your alerts are off. Text START to come back."
    if db.kv_get(conn, f"stopped:{handle}") == "1":
        if word != "start":
            return ""
        db.kv_set(conn, f"stopped:{handle}", "0")
        return "Welcome back. What do you feel like doing?"
    now = datetime.now(ZoneInfo("UTC"))
    hour = db.user_messages_since(conn, handle, (now - timedelta(hours=1)).isoformat(timespec="seconds"))
    day = db.user_messages_since(conn, handle, (now - timedelta(days=1)).isoformat(timespec="seconds"))
    if hour >= config.MAX_MSGS_PER_HOUR or day >= config.MAX_MSGS_PER_DAY:
        db.add_message(conn, f"limit;{handle}", handle, "user", "(over limit)")  # counted, so the notice goes out once
        return "You've hit the message limit for now. Try again in a little while." if hour == config.MAX_MSGS_PER_HOUR or day == config.MAX_MSGS_PER_DAY else ""
    return None


def should_answer(handle: str, text: str, is_group: bool) -> bool:
    if not allowed(handle):
        return False
    if is_group and config.GROUP_TRIGGER not in text.lower():
        return False
    return True


_last_alert_pass = 0.0


def background(conn: sqlite3.Connection, catalog: Catalog, llm) -> None:
    """Quiet jobs between messages: alert checks each minute, memory tidy-up once a day after 3am."""
    global _last_alert_pass
    if time.monotonic() - _last_alert_pass < 60:
        return
    _last_alert_pass = time.monotonic()
    try:
        sent = alerts.check_due(conn, catalog, send_to_chat) + events.check_due(conn, send_to_chat)
        if sent:
            log(f"(sent {sent} alert message(s))")
        refunded = ops.refund_stale_paid(conn, catalog, send_to_chat)
        if refunded:
            print(f"(refunded {refunded} paid booking(s) that were not completed in time)")
        now = datetime.now(ZoneInfo(config.TIMEZONE))
        if now.hour >= 3 and db.kv_get(conn, "memory_consolidated_on") != now.date().isoformat():
            db.kv_set(conn, "memory_consolidated_on", now.date().isoformat())
            print(f"(memory tidy-up: {memory.consolidate_all(models.for_job('memory'))} user(s) updated)")
    except Exception as exc:  # background work must never stop the chat
        log(f"(background job failed: {type(exc).__name__}: {exc})")


def run(poll_seconds: float = 1.0) -> None:
    if not config.ALLOWLIST:
        raise SystemExit("ALLOWLIST is empty. Add the handles that may talk to the bot in .env, or ALLOWLIST=* for anyone.")
    try:
        src = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        src.execute("SELECT MAX(ROWID) FROM message").fetchone()
    except sqlite3.Error:
        raise SystemExit("Cannot read the Messages database. Run with --check to see how to fix it.")
    src.row_factory = sqlite3.Row
    conn = db.connect()
    catalog = Catalog.load()
    places.load_into(catalog, conn)  # venues found earlier in other cities
    llm = models.for_job("chat")
    for job in models.JOBS:
        log(f"Model for {job}: {models.for_job(job).describe()}")

    last = db.kv_get(conn, "imessage_last_rowid")
    if last is None:  # first run: start from now, never reply to old history
        last = str(src.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])
        db.kv_set(conn, "imessage_last_rowid", last)
    who = "ANYONE (open access)" if config.OPEN_ACCESS else sorted(config.ALLOWLIST)
    log(f"Watching Messages (dry_run={config.DRY_RUN}, allowlist={who})")
    print("Waiting for a new iMessage. Press Control + C to stop.")

    while True:
        try:
            rows = src.execute(QUERY, (int(last),)).fetchall()
        except sqlite3.Error as exc:  # Messages is writing: look again in a second
            log(f"(could not read Messages this second: {exc})")
            rows = []
        for row in rows:
            last = str(row["rowid"])
            db.kv_set(conn, "imessage_last_rowid", last)
            try:
                handle_message(src, conn, catalog, llm, row)
            except Exception:  # one bad message must never stop the bridge for everyone else
                log(f"ERROR while answering {row['handle']}:\n{traceback.format_exc()}")
                try:
                    send_to_chat(row["chat_guid"], "Sorry, something went wrong on my side. Try that again in a minute.")
                except Exception:
                    log("ERROR: could not send the apology either")
        background(conn, catalog, llm)
        time.sleep(poll_seconds)


def handle_message(src: sqlite3.Connection, conn: sqlite3.Connection, catalog: Catalog, llm, row: sqlite3.Row) -> None:
    text = shared_location(src, row["rowid"]) or row["text"] or decode_attributed_body(row["attributedBody"])
    is_group = row["style"] == GROUP_STYLE
    if not text or not should_answer(row["handle"], text, is_group):
        # Say why, without the message text, so a silent bridge is easy to diagnose.
        why = "not in ALLOWLIST" if not allowed(row["handle"]) else "group message without the trigger word" if text else "no text"
        log(f"(ignored a message from {row['handle']}: {why})")
        return
    log(f"<- {row['handle']}: {text}")
    if config.OPERATOR_HANDLE and row["handle"] == config.OPERATOR_HANDLE and not is_group:
        answer = ops.operator_command(conn, catalog, send_to_chat, text)  # "booked 3 ref", "failed 3 why", "jobs"
        if answer is not None:
            log(f"-> (operator) {answer}")
            send_to_chat(row["chat_guid"], answer)
            return
    canned = gate(conn, row["handle"], text)
    if canned is not None:
        if canned:
            log(f"-> {canned}")
            send_to_chat(row["chat_guid"], canned)
        return
    guid = row["chat_guid"]
    holding = None
    if config.HOLDING_AFTER_SECONDS > 0:  # a slow turn gets a quick "one sec" so the user is not left waiting
        holding = threading.Timer(config.HOLDING_AFTER_SECONDS, send_to_chat, args=(guid, "One sec, checking that for you."))
        holding.start()
    started = time.monotonic()
    try:
        reply = run_turn(conn, catalog, llm, guid, row["handle"], text, notify_operator, is_group=is_group,
                         send_later=lambda later, guid=guid: (log(f"-> (later) {later}"), send_to_chat(guid, later)))
    finally:
        if holding:
            holding.cancel()
    log(f"-> ({time.monotonic() - started:.0f}s) {reply}")
    send_to_chat(guid, reply)


def check() -> None:
    """Preflight: say what is ready and what is missing. Reads no message text."""
    ok = True

    def report(good: bool, text: str) -> None:
        nonlocal ok
        ok = ok and good
        print(("OK   " if good else "FIX  ") + text)

    try:
        src = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        src.execute("SELECT MAX(ROWID) FROM message").fetchone()
        report(True, "This terminal can read the Messages database.")
    except sqlite3.Error:
        report(False, "This terminal cannot read the Messages database. Give Full Disk Access to the app that "
                      "runs this terminal (System Settings > Privacy & Security > Full Disk Access), then restart that app.")
    report(bool(config.LLM_API_KEY), "LLM_API_KEY is set." if config.LLM_API_KEY else "LLM_API_KEY is empty in .env.")
    report(bool(config.ALLOWLIST), ("ALLOWLIST=*: anyone can text the agent." if config.OPEN_ACCESS else f"ALLOWLIST has {len(config.ALLOWLIST)} tester(s).") if config.ALLOWLIST
           else "ALLOWLIST is empty in .env. Add the tester's number, for example +61412345678.")
    for handle in config.ALLOWLIST:
        if handle != "*" and not (handle.startswith("+") or "@" in handle):
            report(False, f"'{handle}' must be a number that starts with + (for example +61412345678) or an email.")
    print(("NOTE " + "OPERATOR_HANDLE is empty: booking alerts only print in this terminal.") if not config.OPERATOR_HANDLE
          else "OK   OPERATOR_HANDLE is set.")
    print("NOTE DRY_RUN=1: replies print here and are NOT sent." if config.DRY_RUN
          else "NOTE DRY_RUN=0: replies are SENT as real iMessages.")
    print("\nReady. Start the bridge without --check." if ok else "\nNot ready. Correct the FIX lines first.")


if __name__ == "__main__":
    import sys

    check() if "--check" in sys.argv else run()
