"""Detect which booking platform each venue's website uses.

The platform decides how a booking gets executed later (partner API, browser
agent, or phone), so this is the map of where to integrate first.

    python scripts/fingerprint.py           # venues without a platform yet
    python scripts/fingerprint.py --all
"""

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dibs import config  # noqa: E402
from dibs.connectors import miclub  # noqa: E402

SIGNATURES = {
    "roller": [r"roller\.app", r"rollerdigital", r"ecom\.roller", r"roller\.software"],
    "rezdy": [r"rezdy\.com"],
    "bookeo": [r"bookeo\.com"],
    "fareharbor": [r"fareharbor\.com"],
    "checkfront": [r"checkfront\.com"],
    "resova": [r"resova\.(com|io|eu|us)"],
    "xola": [r"xola\.com"],
    "peek": [r"peek\.com"],
    "chronogolf": [r"chronogolf", r"lightspeedgolf"],
    "miclub": [r"miclub\.com\.au"],
    "golfnow": [r"golfnow\."],
    "quick18": [r"quick18\.com"],
    "teeitup": [r"teeitup\.com", r"golfgenius"],
    "trybooking": [r"trybooking\.com"],
    "humanitix": [r"humanitix\.com"],
    "eventbrite": [r"eventbrite\."],
    "simplybook": [r"simplybook\.(me|it)"],
    "square": [r"squareup\.com/appointments", r"square\.site"],
    "bookwhen": [r"bookwhen\.com"],
}
BOOK_LINK = re.compile(r'href="([^"]+)"[^>]*>([^<]{0,60}(book|reserve|tee time)[^<]{0,60})<', re.IGNORECASE)


def detect(html: str) -> str | None:
    hits = Counter()
    for platform, patterns in SIGNATURES.items():
        for pattern in patterns:
            hits[platform] += len(re.findall(pattern, html, re.IGNORECASE))
    best = hits.most_common(1)
    return best[0][0] if best and best[0][1] > 0 else None


def find_booking_url(html: str, base: str) -> str | None:
    match = BOOK_LINK.search(html)
    return urljoin(base, match.group(1)) if match else None


EMAIL = re.compile(r"mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,})")
MICLUB_CALENDAR = re.compile(r'href="([^"]*ViewPublicCalendar\.msp[^"]*)"')


def discover_miclub(client: httpx.Client, html: str, base: str) -> dict | None:
    """Find a club's public tee sheet and its fee groups. Clubs with a CAPTCHA are left alone."""
    link = MICLUB_CALENDAR.search(html)
    if not link:
        return None
    resp = client.get(urljoin(base, link.group(1).replace("&amp;", "&")))
    groups = miclub.fee_groups(resp.text)
    resource = re.search(r"booking_?[rR]esource_?[iI]d=(\d+)", str(resp.url))
    if not groups or not resource or "publicCaptchaEnabled = true" in resp.text:
        return None
    return {"type": "miclub", "base_url": f"{resp.url.scheme}://{resp.url.host}", "resource_id": resource.group(1),
            "fee_group_ids": [g["id"] for g in groups], "fee_group_names": [g["name"] for g in groups]}


def main() -> None:
    redo = "--all" in sys.argv
    path = config.VENUES_PATH
    venues = json.loads(path.read_text())
    client = httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0 (dibs-dev fingerprint)"})
    counts = Counter()
    for venue in venues:
        site = venue.get("website")
        if not site or (venue.get("booking_platform") and not redo):
            continue
        try:
            resp = client.get(site)
            html = resp.text
        except httpx.HTTPError as exc:
            print(f"  ! {venue['name']}: {type(exc).__name__}")
            continue
        platform = detect(html)
        booking_url = find_booking_url(html, str(resp.url))
        # Many sites hide the widget one click deep: check the booking page too.
        if not platform and booking_url:
            try:
                platform = detect(client.get(booking_url).text)
            except httpx.HTTPError:
                pass
        quick18 = re.search(r"https://[a-z0-9-]+\.quick18\.com", html + (booking_url or ""))
        if platform == "quick18" and quick18 and "connector" not in venue:  # "connector": null in venues.json opts a venue out
            venue["connector"] = {"type": "quick18", "base_url": quick18.group(0)}
        if platform == "miclub" and "connector" not in venue:
            try:
                pages = html + (client.get(booking_url).text if booking_url else "")
                found = discover_miclub(client, pages, booking_url or str(resp.url))
                if found:
                    venue["connector"] = found
            except httpx.HTTPError:
                pass
        emails = sorted(set(EMAIL.findall(html)))
        if emails and not venue.get("contact_email"):
            venue["contact_email"] = emails[0]  # a lead only: confirm before you copy it to booking_email
        venue["booking_platform"] = platform
        venue["booking_url"] = venue.get("booking_url") or booking_url
        counts[platform or "none_found"] += 1
        print(f"  {venue['name']}: {platform or '-'}  live={'yes' if venue.get('connector') else 'no'}  {venue.get('contact_email') or ''}")
        time.sleep(1)
    path.write_text(json.dumps(venues, indent=2, ensure_ascii=False) + "\n")
    print("Platforms:", dict(counts))


if __name__ == "__main__":
    main()
