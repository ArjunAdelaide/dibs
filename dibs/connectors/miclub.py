"""MiClub public tee sheet reader (used by most Adelaide golf clubs).

Config: {"type": "miclub", "base_url": "https://club.miclub.com.au",
         "resource_id": "3000000", "fee_group_ids": ["833207"]}

Read-only. Some clubs put a CAPTCHA in front of the sheet; this reader does not
try to pass it, and such clubs simply report no live availability.
"""

import re
from datetime import date

import httpx

from .base import Rate, Slot

ROW = re.compile(r'<div id="row-\d+" class="row row-time(.*?)(?=<div id="row-\d+" class="row row-time|\Z)', re.S)
TIME = re.compile(r"<h3>\s*(\d{1,2}):(\d{2})\s*(am|pm)\s*</h3>", re.I)
FEE = re.compile(r'<span class="price">\s*\$([\d.,]+)\s*</span>\s*([^<]+?)\s*</li>', re.S)
FEE_GROUP = re.compile(r'feeGroupRow feeGroupId-(\d+)[^>]*>\s*<div[^>]*row-heading">\s*<h3>(.*?)</h3>', re.S)


def sheet_url(cfg: dict, fee_group_id: str, day: date) -> str:
    return (f"{cfg['base_url'].rstrip('/')}/guests/bookings/ViewPublicTimesheet.msp"
            f"?bookingResourceId={cfg['resource_id']}&selectedDate={day.isoformat()}&feeGroupId={fee_group_id}")


def parse(page: str, url: str) -> list[Slot]:
    slots = []
    for row in ROW.findall(page):
        when = TIME.search(row)
        free = row.count("cell cell-available")
        if not when or not free:
            continue
        hour = int(when.group(1)) % 12 + (12 if when.group(3).lower() == "pm" else 0)
        rates = [Rate(re.sub(r"\s+", " ", name), float(price.replace(",", "")), url) for price, name in FEE.findall(row)]
        if rates:
            slots.append(Slot(time=f"{hour:02d}:{when.group(2)}", max_players=free, strict_max=True, rates=rates))
    return slots


def fee_groups(calendar_page: str) -> list[dict]:
    """Fee groups listed on a club's public calendar page (used by the fingerprint script)."""
    return [{"id": gid, "name": re.sub(r"\s+", " ", name).strip()} for gid, name in FEE_GROUP.findall(calendar_page)]


def fetch_slots(cfg: dict, day: date, client: httpx.Client | None = None) -> list[Slot]:
    client = client or httpx.Client(follow_redirects=True, timeout=30, headers={"User-Agent": "Mozilla/5.0 (dibs)"})
    by_time: dict[str, Slot] = {}
    for gid in cfg["fee_group_ids"]:
        url = sheet_url(cfg, gid, day)
        resp = client.get(url)
        resp.raise_for_status()
        for slot in parse(resp.text, url):
            if slot.time in by_time:
                by_time[slot.time].rates.extend(slot.rates)
            else:
                by_time[slot.time] = slot
    return sorted(by_time.values(), key=lambda s: s.time)
