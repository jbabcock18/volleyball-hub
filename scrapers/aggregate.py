from __future__ import annotations

import gc
from collections.abc import Callable

from . import atxbeach, beach210, beach512, sportsgarden, thirdcoast
from .models import Tournament


Scraper = Callable[[], list[Tournament]]


def collect() -> tuple[list[Tournament], list[str]]:
    tournaments: list[Tournament] = []
    errors: list[str] = []
    generic_titles = {
        "512 beach tournament",
        "atx beach tournament",
        "210 beach sideliners tournament",
        "sports garden dfw tournament",
    }

    for scraper_name, scraper in (
        ("512 Beach", beach512.scrape),
        ("ATX Beach", atxbeach.scrape),
        ("210 Beach Sideliners", beach210.scrape),
        ("Sports Garden DFW", sportsgarden.scrape),
        ("Third Coast VB", thirdcoast.scrape),
    ):
        try:
            source_tournaments = scraper()
            if not source_tournaments:
                errors.append(f"{scraper_name}: parsed 0 tournaments (source markup may have changed).")
            tournaments.extend(source_tournaments)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{scraper_name}: {exc}")
        finally:
            # Proactively reclaim memory between source scrapes for small hosts.
            gc.collect()

    deduped: dict[tuple[str, str, str], Tournament] = {}
    for t in tournaments:
        normalized_title = t.title.strip()
        if not normalized_title or normalized_title.lower() in generic_titles:
            errors.append(f"{t.source}: missing tournament name for {t.link}")
            continue
        if t.date is None:
            continue

        key = (t.source.lower(), normalized_title.lower(), t.date.isoformat())
        deduped[key] = t

    ordered = sorted(
        deduped.values(),
        key=lambda t: (t.date is None, t.date.isoformat() if t.date else "9999-12-31", t.title.lower()),
    )

    return ordered, errors
