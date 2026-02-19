from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from . import atxbeach, beach210, beach512, sportsgarden, thirdcoast
from .models import Tournament


Scraper = Callable[[], list[Tournament]]
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_source_scraper.py"
_SOURCE_SPECS: tuple[tuple[str, str, Scraper], ...] = (
    ("512 Beach", "beach512", beach512.scrape),
    ("ATX Beach", "atxbeach", atxbeach.scrape),
    ("210 Beach Sideliners", "beach210", beach210.scrape),
    ("Sports Garden DFW", "sportsgarden", sportsgarden.scrape),
    ("Third Coast VB", "thirdcoast", thirdcoast.scrape),
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _use_subprocess_mode() -> bool:
    explicit = os.getenv("SCRAPE_USE_SUBPROCESS")
    if explicit is not None:
        return _truthy(explicit)
    return _truthy(os.getenv("RENDER"))


def _decode_tournaments(payload: dict) -> list[Tournament]:
    tournaments: list[Tournament] = []
    for row in payload.get("tournaments", []):
        raw_date = row.get("date")
        parsed_date = date.fromisoformat(raw_date) if raw_date else None
        tournaments.append(
            Tournament(
                title=row.get("title", ""),
                source=row.get("source", ""),
                link=row.get("link", ""),
                date=parsed_date,
                location=row.get("location"),
            )
        )
    return tournaments


def _collect_source_in_subprocess(source_key: str) -> list[Tournament]:
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), source_key],
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        message = detail or f"subprocess exited with status {completed.returncode}"
        raise RuntimeError(message)

    raw_output = completed.stdout.strip()
    if not raw_output:
        return []

    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid scraper payload: {exc}") from exc

    return _decode_tournaments(payload)


def collect() -> tuple[list[Tournament], list[str]]:
    tournaments: list[Tournament] = []
    errors: list[str] = []
    use_subprocess = _use_subprocess_mode()
    generic_titles = {
        "512 beach tournament",
        "atx beach tournament",
        "210 beach sideliners tournament",
        "sports garden dfw tournament",
    }

    for scraper_name, source_key, scraper in _SOURCE_SPECS:
        try:
            source_tournaments = (
                _collect_source_in_subprocess(source_key) if use_subprocess else scraper()
            )
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
