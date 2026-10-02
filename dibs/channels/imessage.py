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
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import alerts, config, db, events, memory
from ..agent import run_turn
from ..catalog import Catalog
from ..llm import OpenAICompatLLM

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


def should_answer(handle: str, text: str, is_group: bool) -> bool:
    if handle not in config.ALLOWLIST:
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
            print(f"(sent {sent} alert message(s))")
        now = datetime.now(ZoneInfo(config.TIMEZONE))
        if now.hour >= 3 and db.kv_get(conn, "memory_consolidated_on") != now.date().isoformat():
            db.kv_set(conn, "memory_consolidated_on", now.date().isoformat())
            print(f"(memory tidy-up: {memory.consolidate_all(llm)} user(s) updated)")
    except Exception as exc:  # background work must never stop the chat
        print(f"(background job failed: {type(exc).__name__}: {exc})")


def run(poll_seconds: float = 2.0) -> None:
    if not config.ALLOWLIST:
        raise SystemExit("ALLOWLIST is empty. Add the test handles that may talk to the bot in .env.")
    try:
        src = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        src.execute("SELECT MAX(ROWID) FROM message").fetchone()
    except sqlite3.Error:
        raise SystemExit("Cannot read the Messages database. Run with --check to see how to fix it.")
    src.row_factory = sqlite3.Row
    conn = db.connect()
    catalog = Catalog.load()
    llm = OpenAICompatLLM()

    last = db.kv_get(conn, "imessage_last_rowid")
    if last is None:  # first run: start from now, never reply to old history
        last = str(src.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])
        db.kv_set(conn, "imessage_last_rowid", last)
    print(f"Watching Messages (dry_run={config.DRY_RUN}, allowlist={sorted(config.ALLOWLIST)})")
    print("Waiting for a new iMessage. Press Control + C to stop.")

    while True:
        for row in src.execute(QUERY, (int(last),)).fetchall():
            last = str(row["rowid"])
            db.kv_set(conn, "imessage_last_rowid", last)
            text = shared_location(src, row["rowid"]) or row["text"] or decode_attributed_body(row["attributedBody"])
            is_group = row["style"] == GROUP_STYLE
            if not text or not should_answer(row["handle"], text, is_group):
                # Say why, without the message text, so a silent bridge is easy to diagnose.
                why = "not in ALLOWLIST" if row["handle"] not in config.ALLOWLIST else "group message without the trigger word" if text else "no text"
                print(f"(ignored a message from {row['handle']}: {why})")
                continue
            print(f"<- {row['handle']}: {text}")
            print("   (thinking...)")
            guid = row["chat_guid"]
            reply = run_turn(conn, catalog, llm, guid, row["handle"], text, notify_operator, is_group=is_group,
                             send_later=lambda later, guid=guid: (print(f"-> (later) {later}"), send_to_chat(guid, later)))
            print(f"-> {reply}")
            send_to_chat(row["chat_guid"], reply)
        background(conn, catalog, llm)
        time.sleep(poll_seconds)


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
    report(bool(config.ALLOWLIST), f"ALLOWLIST has {len(config.ALLOWLIST)} tester(s)." if config.ALLOWLIST
           else "ALLOWLIST is empty in .env. Add the tester's number, for example +61412345678.")
    for handle in config.ALLOWLIST:
        if not (handle.startswith("+") or "@" in handle):
            report(False, f"'{handle}' must be a number that starts with + (for example +61412345678) or an email.")
    print(("NOTE " + "OPERATOR_HANDLE is empty: booking alerts only print in this terminal.") if not config.OPERATOR_HANDLE
          else "OK   OPERATOR_HANDLE is set.")
    print("NOTE DRY_RUN=1: replies print here and are NOT sent." if config.DRY_RUN
          else "NOTE DRY_RUN=0: replies are SENT as real iMessages.")
    print("\nReady. Start the bridge without --check." if ok else "\nNot ready. Correct the FIX lines first.")


if __name__ == "__main__":
    import sys

    check() if "--check" in sys.argv else run()
