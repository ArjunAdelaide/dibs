"""Quick18 tee sheet reader. The public search page lists every open slot,
each rate with its price, and a direct link that opens that exact slot.

Read-only: it never submits a reservation.
"""

import html
import re
from datetime import date

import httpx

from .base import Rate, Slot

ROW = re.compile(r'<td class="mtrxTeeTimes">\s*(\d{1,2}):(\d{2})<div class="be_tee_time_ampm">(AM|PM)</div>(.*?)</tr>', re.S)
HEADER = re.compile(r'<th class="matrixHdrSched">(.*?)</th>', re.S)
CELL = re.compile(r'<td class="matrixsched[^"]*">(.*?)</td>', re.S)
PRICE = re.compile(r'mtrxPrice">\s*\$([\d.,]+)')
LINK = re.compile(r'href="([^"]+)"')
PLAYERS = re.compile(r'matrixPlayers">\s*(\d+)(?:\s*to\s*(\d+))?\s*player')


def parse(page: str, base_url: str) -> list[Slot]:
    names = [html.unescape(re.sub(r"\s+", " ", h)).strip() for h in HEADER.findall(page)]
    slots = []
    for hour, minute, ampm, rest in ROW.findall(page):
        h = int(hour) % 12 + (12 if ampm == "PM" else 0)
        slot = Slot(time=f"{h:02d}:{minute}")
        players = PLAYERS.search(rest)
        if players:
            slot.min_players = int(players.group(1))
            slot.max_players = int(players.group(2) or players.group(1))
        for name, cell in zip(names, CELL.findall(rest)):
            price, link = PRICE.search(cell), LINK.search(cell)
            if price and link:
                slot.rates.append(Rate(name, float(price.group(1).replace(",", "")),
                                       base_url.rstrip("/") + html.unescape(link.group(1))))
        if slot.rates:
            slots.append(slot)
    return slots


def fetch_slots(cfg: dict, day: date, client: httpx.Client | None = None) -> list[Slot]:
    base_url = cfg["base_url"]
    client = client or httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": "Mozilla/5.0 (dibs)"})
    resp = client.get(f"{base_url.rstrip('/')}/teetimes/searchmatrix", params={"teedate": day.strftime("%Y%m%d")})
    resp.raise_for_status()
    return parse(resp.text, base_url)
