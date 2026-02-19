from __future__ import annotations

import json
import sys

from scrapers import atxbeach, beach210, beach512, sportsgarden, thirdcoast


SCRAPERS = {
    "beach512": beach512.scrape,
    "atxbeach": atxbeach.scrape,
    "beach210": beach210.scrape,
    "sportsgarden": sportsgarden.scrape,
    "thirdcoast": thirdcoast.scrape,
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run_source_scraper.py <source_key>", file=sys.stderr)
        return 2

    source_key = sys.argv[1].strip().lower()
    scraper = SCRAPERS.get(source_key)
    if scraper is None:
        print(f"unknown source_key: {source_key}", file=sys.stderr)
        return 2

    tournaments = scraper()
    payload = {"tournaments": [t.to_dict() for t in tournaments]}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
