from __future__ import annotations

import json
import sys
from pathlib import Path

# When run as a script (e.g. by subprocess from aggregate.py), the script's
# directory is on sys.path but the project root is not; add it so "scrapers" resolves.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

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
