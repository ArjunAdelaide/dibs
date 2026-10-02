import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dibs import db
from dibs.agent import run_turn
from dibs.catalog import Catalog, deal_applies
from dibs.channels.imessage import decode_attributed_body, should_answer
from dibs.tools import ToolContext, confirm_booking, find_deals, propose_booking
from scripts.fingerprint import detect

TZ = ZoneInfo("Australia/Adelaide")
NOW = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)  # a Monday
VENUE = {"id": "test-bowl", "name": "Test Bowl", "categories": ["bowling"], "area": "Norwood",
         "phone": "08 0000 0000", "lat": -34.92, "lon": 138.63, "verified": True}
DEAL = {"id": "cheap-weekday", "venue_id": "test-bowl", "title": "2 games for $20", "price": "$20pp",
        "conditions": {"days": ["mon", "tue", "wed"], "start": "12:00", "end": "17:00", "min_party": 2}, "expires": "2026-12-31"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep tests away from real memory files, real mail and the network."""
    monkeypatch.setattr("dibs.config.MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr("dibs.config.DRY_RUN", True)
    monkeypatch.setattr("dibs.config.SMTP_USER", "")
    monkeypatch.setattr("dibs.config.SMTP_PASSWORD", "")
    monkeypatch.setattr("dibs.config.TICKETMASTER_API_KEY", "")
    monkeypatch.setattr("dibs.config.PAYMENTS_ENABLED", False)
    monkeypatch.setattr("dibs.config.OPEN_ACCESS", False)
    monkeypatch.setattr("dibs.config.BLOCKLIST", set())
    monkeypatch.setattr("dibs.config.STRIPE_SECRET_KEY", "")


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    catalog = Catalog(venues={"test-bowl": VENUE}, deals=[DEAL])
    alerts: list[str] = []
    c = ToolContext(conn=conn, catalog=catalog, handle="+61400000001", conv_id="c1", last_user_text="",
                    now=NOW, notify_operator=alerts.append)
    c.alerts = alerts
    return c


def test_deal_conditions():
    wed_4pm = datetime(2026, 10, 7, 16, 0, tzinfo=TZ)
    assert deal_applies(DEAL, wed_4pm, 4) == (True, "applies")
    assert deal_applies(DEAL, wed_4pm.replace(hour=17), 4)[0] is False
    assert deal_applies(DEAL, wed_4pm, 1)[0] is False
    assert deal_applies(DEAL, datetime(2026, 10, 10, 14, 0, tzinfo=TZ), 4)[0] is False  # Saturday
    assert deal_applies(DEAL, datetime(2027, 1, 6, 14, 0, tzinfo=TZ), 4)[0] is False  # expired


def test_find_deals_reports_near_miss(ctx):
    out = find_deals(ctx, "2026-10-07T17:30", 4)
    assert out["applies"] == []
    assert out["near_misses"][0]["why_not"] == "only before 17:00"


def test_booking_needs_explicit_yes(ctx):
    proposal = propose_booking(ctx, "test-bowl", "2026-10-07T16:00", 4, deal_id="cheap-weekday")
    pid = proposal["proposal_id"]
    ctx.last_user_text = "hmm maybe, what about later?"
    assert "error" in confirm_booking(ctx, pid)
    assert ctx.alerts == []
    ctx.last_user_text = "yes book it"
    assert confirm_booking(ctx, pid)["route"] == "concierge"
    assert len(ctx.alerts) == 1
    assert "error" in confirm_booking(ctx, pid)  # cannot double-book


def test_propose_rejects_bad_deal_and_past(ctx):
    assert "error" in propose_booking(ctx, "test-bowl", "2026-10-10T16:00", 4, deal_id="cheap-weekday")
    assert "error" in propose_booking(ctx, "test-bowl", "2026-10-01T16:00", 4)
    assert "error" in propose_booking(ctx, "nope", "2026-10-07T16:00", 4)


def test_proposal_expires(ctx):
    pid = propose_booking(ctx, "test-bowl", "2026-10-07T16:00", 4)["proposal_id"]
    ctx.now = NOW + timedelta(hours=1)
    ctx.last_user_text = "yes"
    assert "expired" in confirm_booking(ctx, pid)["error"]


class ScriptedLLM:
    """Plays back tool calls, then a final text, like a model would."""

    def __init__(self, steps):
        self.steps = list(steps)

    def chat(self, messages, tools):
        step = self.steps.pop(0)
        if isinstance(step, str):
            return {"role": "assistant", "content": step}
        name, args = step
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{len(self.steps)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_agent_turns_end_to_end(ctx):
    alerts = []
    llm = ScriptedLLM([
        ("propose_booking", {"venue_id": "test-bowl", "starts_at": "2026-10-07T16:00", "party_size": 4}),
        "Test Bowl, Wed 4pm, 4 of you. Reply YES to book.",
        ("confirm_booking", {"proposal_id": 1}),
        "Locked in, confirmation coming shortly.",
    ])
    first = run_turn(ctx.conn, ctx.catalog, llm, "c1", "+61400000001", "bowling wed 4pm for 4", alerts.append, now=NOW)
    assert "YES" in first
    second = run_turn(ctx.conn, ctx.catalog, llm, "c1", "+61400000001", "yes", alerts.append, now=NOW)
    assert "Locked in" in second and len(alerts) == 1
    assert ctx.conn.execute("SELECT status FROM bookings").fetchone()["status"] == "needs_human"


def test_decode_attributed_body():
    text = "bowling at 4?"
    blob = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + bytes([len(text)]) + text.encode() + b"\x86"
    assert decode_attributed_body(blob) == text


def test_bridge_gating(monkeypatch):
    monkeypatch.setattr("dibs.config.ALLOWLIST", {"+61400000001"})
    assert should_answer("+61400000001", "hi", is_group=False)
    assert not should_answer("+61499999999", "hi", is_group=False)
    assert not should_answer("+61400000001", "bowling tonight?", is_group=True)
    assert should_answer("+61400000001", "dibs bowling tonight?", is_group=True)


def test_fingerprint_detect():
    assert detect('<script src="https://ecom.roller.app/widget.js"></script>') == "roller"
    assert detect('<a href="https://club.miclub.com.au/guests/bookings">Book</a>') == "miclub"
    assert detect("<html>no widget here</html>") is None


# --- live availability and booking routes ---

from pathlib import Path  # noqa: E402

from dibs import executors  # noqa: E402
from dibs.connectors import quick18  # noqa: E402
from dibs.tools import check_availability  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "quick18_matrix.html"
LIVE_VENUE = {"id": "mini", "name": "Mini Golf", "categories": ["mini_golf"], "payment": "online",
              "connector": {"type": "quick18", "base_url": "https://x.quick18.com"}}
EMAIL_VENUE = {"id": "lanes", "name": "Lanes", "categories": ["bowling"], "payment": "at_venue", "booking_email": "book@lanes.test"}


@pytest.fixture
def live(ctx, monkeypatch):
    slots = quick18.parse(FIXTURE.read_text(), "https://x.quick18.com")
    monkeypatch.setattr("dibs.tools.slots_for", lambda venue, day: slots)
    monkeypatch.setattr("dibs.executors.slots_for", lambda venue, day: slots)
    ctx.catalog.venues.update({"mini": LIVE_VENUE, "lanes": EMAIL_VENUE})
    return ctx


def test_quick18_parse():
    slots = quick18.parse(FIXTURE.read_text(), "https://x.quick18.com")
    assert [s.time for s in slots] == ["09:00", "09:10", "09:20"]
    first = slots[0].rates[0]
    assert (first.name, first.price) == ("SHANX Single", 18.0)
    assert first.url.startswith("https://x.quick18.com/teetimes/course/") and "&amp;" not in first.url
    assert all(r.price > 0 for s in slots for r in s.rates)  # N/A cells are dropped


def test_availability_tool(live):
    out = check_availability(live, "mini", "2026-10-07", around_time="09:10", party_size=2)
    assert out["live"] and "09:10" in [s["time"] for s in out["nearest"]]
    assert check_availability(live, "test-bowl", "2026-10-07")["live"] is False


def test_link_route_only_books_real_slots(live):
    assert "error" in propose_booking(live, "mini", "2026-10-07T16:00", 2)  # not on the feed
    pid = propose_booking(live, "mini", "2026-10-07T09:10", 2, rate="SHANX Single")["proposal_id"]
    live.last_user_text = "yes"
    out = confirm_booking(live, pid)
    assert out["route"] == "link" and "/teetime/" in out["booking_link"]
    assert live.conn.execute("SELECT status FROM bookings").fetchone()["status"] == "link_sent"


def test_email_route_needs_name_then_sends(live, capsys, monkeypatch):
    assert executors.pick_route(EMAIL_VENUE) == "concierge"  # no mailbox configured: never pretend to email
    monkeypatch.setattr("dibs.config.SMTP_USER", "bot@example.test")
    monkeypatch.setattr("dibs.config.SMTP_PASSWORD", "x")
    assert executors.pick_route(EMAIL_VENUE) == "email"
    pid = propose_booking(live, "lanes", "2026-10-07T16:00", 4)["proposal_id"]
    live.last_user_text = "yes"
    assert "name" in confirm_booking(live, pid)["error"]
    db.set_pref(live.conn, live.handle, "name", "Arjun")
    out = confirm_booking(live, pid)
    assert out["route"] == "email" and len(live.alerts) == 1
    printed = capsys.readouterr().out
    assert "book@lanes.test" in printed and "pay on arrival" in printed


def test_page_route_sends_booking_page(live):
    live.catalog.venues["page"] = {"id": "page", "name": "Page Venue", "categories": ["vr"], "booking_url": "https://venue.test/book"}
    pid = propose_booking(live, "page", "2026-10-07T16:00", 2)["proposal_id"]
    live.last_user_text = "yes"
    out = confirm_booking(live, pid)
    assert out["route"] == "page" and out["booking_link"] == "https://venue.test/book" and "NOT booked" in out["status"]


# --- connectors: MiClub ---

from dibs.connectors import miclub  # noqa: E402


def test_miclub_parse():
    slots = miclub.parse((Path(__file__).parent / "fixtures" / "miclub_timesheet.html").read_text(), "https://club.test/sheet")
    assert slots[0].time == "07:45" and slots[0].max_players == 2 and slots[0].strict_max
    assert (slots[0].rates[0].name, slots[0].rates[0].price) == ("Par 3 Course Mon-Thurs", 17.0)
    assert slots[0].fits(2) and not slots[0].fits(3)  # only 2 places left in that group


# --- location ---

from dibs import geo  # noqa: E402
from dibs.tools import search_venues  # noqa: E402


def test_coords_and_distance():
    assert geo.extract_coords("https://maps.apple.com/?ll=-34.9212\\,138.6307&q=Home") == (-34.9212, 138.6307)
    assert geo.extract_coords("no numbers here") is None
    assert 17 < geo.haversine_km(-34.9285, 138.6007, -34.77, 138.64) < 19


def test_search_sorts_by_saved_location(ctx):
    ctx.catalog.venues["far"] = {"id": "far", "name": "Far Bowl", "categories": ["bowling"], "lat": -34.70, "lon": 138.67}
    ctx.catalog.venues["nowhere"] = {"id": "nowhere", "name": "A No Coordinates", "categories": ["bowling"]}
    assert "no location known" in search_venues(ctx, category="bowling")["sorted_by"]
    db.set_pref(ctx.conn, ctx.handle, "location", {"name": "Norwood", "lat": -34.921, "lon": 138.632})
    out = search_venues(ctx, category="bowling")
    assert [v["venue_id"] for v in out["venues"]] == ["test-bowl", "far", "nowhere"]
    assert out["venues"][0]["distance_km"] < 1 < out["venues"][1]["distance_km"]


def test_shared_location_pin_is_saved(ctx):
    llm = ScriptedLLM(["Got your location."])
    run_turn(ctx.conn, ctx.catalog, llm, "c1", ctx.handle, "Shared location: -34.9212,138.6307", print, now=NOW)
    assert db.get_prefs(ctx.conn, ctx.handle)["location"]["lat"] == -34.9212


# --- alerts ---

from dibs import alerts  # noqa: E402
from dibs.tools import create_alert  # noqa: E402


def test_alert_fires_once_when_slot_opens(live, monkeypatch):
    monkeypatch.setattr("dibs.alerts.slots_for", lambda venue, day: [])
    out = create_alert(live, "mini", "09:00", "09:30", 2, day="2026-10-07")
    assert out["created"]
    sent = []
    assert alerts.check_due(live.conn, live.catalog, lambda conv, text: sent.append(text), now=NOW) == 0  # nothing open: stay quiet
    slots = quick18.parse(FIXTURE.read_text(), "https://x.quick18.com")
    monkeypatch.setattr("dibs.alerts.slots_for", lambda venue, day: slots)
    assert alerts.check_due(live.conn, live.catalog, lambda conv, text: sent.append(text), now=NOW) == 0  # not due yet
    assert alerts.check_due(live.conn, live.catalog, lambda conv, text: sent.append(text), now=NOW, force=True) == 1
    assert "9:00AM" in sent[0] and "Wed 7 Oct" in sent[0]
    assert alerts.check_due(live.conn, live.catalog, lambda conv, text: sent.append(text), now=NOW, force=True) == 0  # one message only


def test_alert_not_created_when_already_open_or_bad_input(live, monkeypatch):
    slots = quick18.parse(FIXTURE.read_text(), "https://x.quick18.com")
    monkeypatch.setattr("dibs.alerts.slots_for", lambda venue, day: slots)
    out = create_alert(live, "mini", "09:00", "09:30", 2, day="2026-10-07")
    assert out["created"] is False and out["open_now"]
    assert create_alert(live, "mini", "09:00", "09:30", 2, day="2026-10-07", max_price=5)["created"]  # price not met yet
    assert "error" in create_alert(live, "test-bowl", "09:00", "09:30", 2, day="2026-10-07")  # no live feed
    assert "error" in create_alert(live, "mini", "09:00", "09:30", 2)  # no day


# --- memory ---

from dibs import memory  # noqa: E402


def test_memory_notes_search_and_consolidate(ctx):
    memory.note(ctx.handle, "Goes bowling with Sam and Priya most Wednesdays", NOW)
    memory.note(ctx.handle, "Liked Test Bowl", NOW)
    assert "Sam and Priya" in memory.context(ctx.handle)
    assert any("Test Bowl" in hit for hit in memory.search(ctx.handle, "which bowl did I like"))
    llm = ScriptedLLM(['```json\n{"files": {"profile.md": "---\\nid: profile\\n---\\n- Bowls with Sam and Priya on Wednesdays.", "../evil.md": "x"}}\n```'])
    assert memory.consolidate(ctx.handle, llm) is True
    folder = memory.user_dir(ctx.handle)
    assert "Sam and Priya" in (folder / "profile.md").read_text()
    assert not (folder.parent / "evil.md").exists()  # file names from the model cannot leave the folder
    assert not list((folder / "log").glob("*.md")) and (folder / "log" / "done").exists()
    assert memory.consolidate(ctx.handle, llm) is False  # nothing new


def test_booking_is_written_to_memory(ctx):
    pid = propose_booking(ctx, "test-bowl", "2026-10-07T16:00", 4)["proposal_id"]
    ctx.last_user_text = "yes"
    confirm_booking(ctx, pid)
    assert "Test Bowl" in memory.context(ctx.handle)


# --- open-ended ideas ---

from dibs.tools import suggest_ideas  # noqa: E402


def test_suggest_ideas_needs_location_then_varies_kinds(live):
    assert "no location" in suggest_ideas(live, "2026-10-07", "09:10", 2)["error"]
    db.set_pref(live.conn, live.handle, "location", {"name": "Norwood", "lat": -34.921, "lon": 138.632})
    live.catalog.venues["mini"].update(lat=-34.93, lon=138.64)
    live.catalog.venues["lanes"].update(lat=-34.925, lon=138.63)
    live.catalog.venues["far"] = {"id": "far", "name": "Far Away Golf", "categories": ["golf"], "lat": -35.5, "lon": 138.6}
    out = suggest_ideas(live, "2026-10-07", "09:10", 2, max_km=10)
    names = [i["name"] for i in out["ideas"]]
    assert names[0] == "Mini Golf" and out["ideas"][0]["open_slot"] == "09:10" and out["ideas"][0]["live"]
    assert "Far Away Golf" not in names  # outside the travel distance
    assert len({i["kind"] for i in out["ideas"][:2]}) == 2  # different kinds first
    assert suggest_ideas(live, "2026-10-07", "20:00", 2, max_km=10)["ideas"][0]["name"] != "Mini Golf"  # nothing open near 8pm


# --- ticketed events ---

from dibs import events  # noqa: E402
from dibs.tools import cancel_alert, create_event_alert, find_events, list_alerts  # noqa: E402

# Shape follows the Ticketmaster Discovery API v2 documentation; not captured from a live call.
TM_EVENT = {
    "id": "G5vYZ9", "name": "Some Band: World Tour", "url": "https://www.ticketmaster.com.au/event/1",
    "dates": {"start": {"localDate": "2027-02-10"}, "status": {"code": "offsale"}},
    "sales": {"public": {"startDateTime": "2026-10-09T02:30:00Z"},
              "presales": [{"name": "Fan presale", "startDateTime": "2026-10-07T02:30:00Z"}]},
    "priceRanges": [{"min": 89.9, "max": 189.9}],
    "_embedded": {"venues": [{"name": "Adelaide Entertainment Centre"}]},
}


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    path = tmp_path / "events.json"
    path.write_text(json.dumps([
        {"id": "fest", "name": "Test Fest 2027", "tags": ["festival"], "start_date": "2027-02-19", "end_date": "2027-03-21",
         "status": "announced", "onsale_at": "2026-12-04", "presales": [{"name": "Bank presale", "start_at": "2026-12-03"}],
         "url": "https://fest.test"},
        {"id": "old", "name": "Old Fest", "start_date": "2026-01-01", "status": "onsale", "url": "https://old.test"},
        {"id": "tba", "name": "Footy Round 2027", "start_date": "2027-04-08", "status": "announced", "onsale_at": None, "url": "https://footy.test"},
    ]))
    monkeypatch.setattr("dibs.config.EVENTS_PATH", path)
    return path


def test_ticketmaster_event_shape():
    e = events.normalise_tm(TM_EVENT)
    assert e["event_id"] == "tm:G5vYZ9" and e["venue"] == "Adelaide Entertainment Centre" and e["price_from"] == 89.9
    assert e["onsale_at"].startswith("2026-10-09T13:00")  # 02:30 UTC is 1pm in Adelaide (daylight time)
    assert events.next_sale(e, NOW) == ("Fan presale", e["presales"][0]["start_at"])


def test_calendar_search_and_onsale_alert(ctx, calendar):
    found = find_events(ctx, keyword="fest")["events"]
    assert [e["name"] for e in found] == ["Test Fest 2027"]  # past events are left out
    out = create_event_alert(ctx, "onsale", event_id="cal:fest")
    assert out["created"] and "Bank presale" in out["next"]
    sent = []
    send = lambda conv, text: sent.append(text)  # noqa: E731
    assert events.check_due(ctx.conn, send, now=NOW) == 0  # two months early: stay quiet
    assert events.check_due(ctx.conn, send, now=datetime(2026, 12, 3, 7, 0, tzinfo=TZ)) == 0
    assert events.check_due(ctx.conn, send, now=datetime(2026, 12, 3, 8, 5, tzinfo=TZ)) == 1
    assert "Bank presale" in sent[0] and "https://fest.test" in sent[0]
    assert events.check_due(ctx.conn, send, now=datetime(2026, 12, 3, 9, 0, tzinfo=TZ)) == 0  # one message only


def test_onsale_alert_waits_for_a_date_then_fires(ctx, calendar):
    assert create_event_alert(ctx, "onsale", event_id="cal:tba")["created"]
    sent = []
    send = lambda conv, text: sent.append(text)  # noqa: E731
    assert events.check_due(ctx.conn, send, now=NOW) == 0
    data = json.loads(calendar.read_text())
    data[2]["status"] = "onsale"  # you update the calendar when tickets are released
    calendar.write_text(json.dumps(data))
    assert events.check_due(ctx.conn, send, now=NOW, force=True) == 1 and "on sale now" in sent[0]


def test_new_show_alert_and_alert_ids(ctx, calendar, monkeypatch):
    assert "error" in create_event_alert(ctx, "new_show", keyword="some band")  # no Ticketmaster key
    monkeypatch.setattr("dibs.config.TICKETMASTER_API_KEY", "k")
    listed = []
    monkeypatch.setattr("dibs.events.search_ticketmaster", lambda *a, **k: list(listed))
    out = create_event_alert(ctx, "new_show", keyword="some band")
    assert out["created"] and out["alert_id"] == "event-1"
    sent = []
    send = lambda conv, text: sent.append(text)  # noqa: E731
    assert events.check_due(ctx.conn, send, now=NOW, force=True) == 0
    listed.append(events.normalise_tm(TM_EVENT))
    assert events.check_due(ctx.conn, send, now=NOW, force=True) == 1 and "Some Band: World Tour" in sent[0]
    assert events.check_due(ctx.conn, send, now=NOW, force=True) == 0  # the same show is not sent twice
    assert any(a.startswith("event-1") for a in list_alerts(ctx)["alerts"])
    assert cancel_alert(ctx, "event-1")["cancelled"] and list_alerts(ctx)["alerts"] == []
    assert "error" in cancel_alert(ctx, "7")


# --- browser agent ---

import time as _time  # noqa: E402

from dibs import browser  # noqa: E402
from dibs.tools import check_site  # noqa: E402


def test_browser_guards_and_helpers():
    assert browser._site("booking.kingpinplay.com") == "kingpinplay.com"
    assert browser._site("booking.holeymoley.com.au") == "holeymoley.com.au"
    assert browser._parse_action('Sure! {"action": "click", "index": 3}')["index"] == 3
    assert browser._parse_action("no json")["action"] == "fail"
    assert browser.FINAL_BUTTON.search("Confirm & Pay") and browser.FINAL_BUTTON.search("CHECKOUT")
    assert not browser.FINAL_BUTTON.search("Afternoon") and not browser.FINAL_BUTTON.search("Select 2 adults")
    assert browser.BOT_CHECK.search("<title>Just a moment...</title>") and browser.PAYMENT.search('<input autocomplete="cc-number">')
    # a time the model reports must be written on the page, or it is dropped
    assert browser.times_on_page(["16:00", "4:30pm", "19:00"], "Sessions: 4:00 PM, 16:30, 5:00 PM") == ["4:00pm", "4:30pm"]


def test_check_site_reports_now_or_later(ctx, monkeypatch):
    ctx.catalog.venues["page"] = {"id": "page", "name": "Page Bowl", "categories": ["bowling"], "booking_url": "https://book.page.test/"}
    seen = {}

    def fake_browse(url, goal, llm, **kwargs):
        seen["goal"] = goal
        return browser.BrowseResult(status="done", result={"times": ["4:00pm", "4:30pm"], "price": "$20"})

    monkeypatch.setattr("dibs.browser.browse", fake_browse)
    out = check_site(ctx, "page", "2026-10-07", "16:00", 3)  # no way to text later: answer in this turn
    assert "4:00pm, 4:30pm" in out["report"] and "https://book.page.test/" in out["report"]
    assert "3 people" in seen["goal"] and "Wednesday 7 October 2026" in seen["goal"]
    later = []
    ctx.send_later = later.append
    assert check_site(ctx, "page", "2026-10-07")["started"] is True
    for _ in range(50):
        if later:
            break
        _time.sleep(0.05)
    assert "4:00pm, 4:30pm open at Page Bowl" in later[0]
    monkeypatch.setattr("dibs.browser.browse", lambda *a, **k: browser.BrowseResult(status="blocked"))
    ctx.send_later = None
    assert "bot check" in check_site(ctx, "page", "2026-10-07")["report"]
    assert "error" in check_site(ctx, "test-bowl", "2026-10-07")  # no booking site on file


# --- open access, limits, opt-out ---

from dibs.channels import imessage  # noqa: E402


def test_open_access_and_blocklist(monkeypatch):
    monkeypatch.setattr("dibs.config.ALLOWLIST", {"*"})
    monkeypatch.setattr("dibs.config.OPEN_ACCESS", True)
    monkeypatch.setattr("dibs.config.BLOCKLIST", {"+61488888888"})
    assert imessage.allowed("+61412345678") and imessage.allowed("someone@example.com")
    assert not imessage.allowed("+61488888888")  # blocked
    assert not imessage.allowed("12345")  # short codes and other senders are ignored


def test_stop_start_and_rate_limit(ctx, monkeypatch):
    h = ctx.handle
    assert imessage.gate(ctx.conn, h, "hello") is None
    alerts.create(ctx.conn, "c1", h, "test-bowl", "2026-10-07", None, "09:00", "10:00", 2, None)
    assert "won't hear from Dibs" in imessage.gate(ctx.conn, h, "STOP")
    assert ctx.conn.execute("SELECT status FROM alerts").fetchone()["status"] == "cancelled"
    assert imessage.gate(ctx.conn, h, "bowling?") == ""  # silent while stopped
    assert "Welcome back" in imessage.gate(ctx.conn, h, "start")
    monkeypatch.setattr("dibs.config.MAX_MSGS_PER_HOUR", 2)
    db.add_message(ctx.conn, "c1", h, "user", "one")
    db.add_message(ctx.conn, "c1", h, "user", "two")
    assert "limit" in imessage.gate(ctx.conn, h, "three")  # told once
    assert imessage.gate(ctx.conn, h, "four") == ""  # then silence


# --- payments (Stripe calls are faked: no network, no money) ---

from dibs import payments  # noqa: E402


@pytest.fixture
def paying(live, monkeypatch):
    monkeypatch.setattr("dibs.config.PAYMENTS_ENABLED", True)
    monkeypatch.setattr("dibs.config.STRIPE_SECRET_KEY", "sk_test_x")
    state = {"card": None, "charges": [], "refunds": []}
    monkeypatch.setattr("dibs.payments.saved_card", lambda conn, handle: state["card"])
    monkeypatch.setattr("dibs.payments.setup_link", lambda conn, handle: "https://checkout.stripe.test/setup")

    def fake_charge(conn, handle, amount_cents, description, proposal_id):
        if state.get("decline"):
            raise payments.PaymentError("the card was declined (card_declined)")
        state["charges"].append(amount_cents)
        return "pi_test_1"

    monkeypatch.setattr("dibs.payments.charge", fake_charge)
    monkeypatch.setattr("dibs.payments.refund", lambda intent: state["refunds"].append(intent))
    live.state = state
    return live


def test_payment_flow_card_then_charge(paying):
    out = propose_booking(paying, "mini", "2026-10-07T09:10", 4, rate="SHANX Single")
    assert "total $72.00 charged to your saved card" in out["summary"]  # 4 x $18, shown before the yes
    paying.last_user_text = "yes"
    first = confirm_booking(paying, out["proposal_id"])
    assert first["needs_card"] and "stripe" in first["setup_link"] and paying.state["charges"] == []
    assert paying.conn.execute("SELECT status FROM proposals").fetchone()["status"] == "pending"  # still open for the next yes
    paying.state["card"] = "pm_test"
    second = confirm_booking(paying, out["proposal_id"])
    assert second["route"] == "paid" and second["charged"] == "$72.00" and paying.state["charges"] == [7200]
    row = paying.conn.execute("SELECT status, amount_cents, payment_intent FROM bookings").fetchone()
    assert tuple(row) == ("paid_needs_human", 7200, "pi_test_1")
    assert "PAID booking" in paying.alerts[0] and "$72.00" in paying.alerts[0]
    assert "error" in confirm_booking(paying, out["proposal_id"])  # a second yes cannot charge again


def test_declined_card_books_nothing(paying):
    paying.state.update(card="pm_test", decline=True)
    pid = propose_booking(paying, "mini", "2026-10-07T09:10", 2, rate="SHANX Single")["proposal_id"]
    paying.last_user_text = "yes"
    out = confirm_booking(paying, pid)
    assert "payment failed" in out["error"] and paying.conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0


def test_live_key_is_refused_and_amount_is_capped(monkeypatch, ctx):
    monkeypatch.setattr("dibs.config.STRIPE_SECRET_KEY", "sk_live_x")
    with pytest.raises(payments.PaymentError):
        payments._stripe()
    monkeypatch.setattr("dibs.config.STRIPE_SECRET_KEY", "sk_test_x")
    with pytest.raises(payments.PaymentError):
        payments.charge(ctx.conn, ctx.handle, 10_000_000, "too much", 1)


def test_event_search_kinds_and_sessions(ctx, calendar, monkeypatch):
    monkeypatch.setattr("dibs.config.TICKETMASTER_API_KEY", "k")
    asked = {}

    def fake_tm(keyword=None, start=None, end=None, size=40, client=None, kind=None):
        asked.update(keyword=keyword, kind=kind)
        show = events.normalise_tm(TM_EVENT)
        return [show, {**show, "event_id": "tm:second-night", "start_date": "2027-02-11"}]

    monkeypatch.setattr("dibs.events.search_ticketmaster", fake_tm)
    out = find_events(ctx, kind="music")
    assert asked == {"keyword": None, "kind": "music"}
    assert len(out["events"]) == 1 and out["events"][0]["more_dates"] == 1  # two nights, one line
    assert "error" in find_events(ctx, kind="concerts")
