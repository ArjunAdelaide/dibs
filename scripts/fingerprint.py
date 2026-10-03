"""Detect which booking platform each venue in data/venues.json uses.

    python scripts/fingerprint.py           # venues without a platform yet
    python scripts/fingerprint.py --all
"""

import json
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dibs import config  # noqa: E402
from dibs.fingerprint import detect, inspect_site  # noqa: E402, F401  (detect is re-exported for the tests)


def main() -> None:
    redo = "--all" in sys.argv
    path = config.VENUES_PATH
    venues = json.loads(path.read_text())
    client = httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0 (dibs-dev fingerprint)"})
    counts = Counter()
    for venue in venues:
        if not venue.get("website") or (venue.get("booking_platform") and not redo):
            continue
        if not inspect_site(client, venue):
            print(f"  ! {venue['name']}: could not read the site")
            continue
        counts[venue["booking_platform"] or "none_found"] += 1
        print(f"  {venue['name']}: {venue['booking_platform'] or '-'}  live={'yes' if venue.get('connector') else 'no'}  {venue.get('contact_email') or ''}")
        time.sleep(1)
    path.write_text(json.dumps(venues, indent=2, ensure_ascii=False) + "\n")
    print("Platforms:", dict(counts))


if __name__ == "__main__":
    main()
