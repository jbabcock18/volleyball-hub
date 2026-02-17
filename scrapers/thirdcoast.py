from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from .models import Tournament
from .utils import extract_first_date, normalize_ws, tidy_title

SOURCE = "Third Coast VB"
URL = "https://thirdcoastvolleyball.com/tournaments/tournament-schedule/"
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_LONG_DATE_WITH_YEAR_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*-\s*\d{1,2}(?:st|nd|rd|th)?)?,?\s+\d{4}\b",
    re.IGNORECASE,
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _row_text(cell: Tag) -> str:
    return normalize_ws(cell.get_text(" ", strip=True))


def _is_header_row(date_text: str, name_text: str) -> bool:
    if not date_text or not name_text:
        return True
    if date_text.lower() == "date" and name_text.lower() == "name":
        return True
    return False


def _is_past_section(date_text: str, name_text: str) -> bool:
    combined = f"{date_text} {name_text}".lower()
    return any(
        needle in combined
        for needle in (
            "past tournament",
            "past tournaments",
            "past event",
            "past events",
            "past result",
            "past results",
            "previous tournament",
            "previous tournaments",
        )
    )


def _has_explicit_year(text: str) -> bool:
    return bool(_YEAR_RE.search(text or ""))


def _extract_year(text: str) -> int | None:
    match = _YEAR_RE.search(text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _row_link(row: Tag) -> str:
    for anchor in row.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        return urljoin(URL, href)
    return URL


def _detail_date_from_event_page(session: requests.Session, link: str) -> date | None:
    lower = link.lower()
    if "volleyballlife.com/event/" not in lower and "volleyballlife.com/events/" not in lower:
        return None

    try:
        response = session.get(link, timeout=30, headers=_HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[str] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        candidates.extend(_ISO_DATE_RE.findall(raw))
        candidates.extend(_LONG_DATE_WITH_YEAR_RE.findall(raw))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key in ("startDate", "date", "dateStart", "start_date"):
                value = payload.get(key)
                if isinstance(value, str):
                    candidates.append(value)

    body = normalize_ws(soup.get_text(" ", strip=True))
    candidates.extend(_ISO_DATE_RE.findall(body))
    candidates.extend(_LONG_DATE_WITH_YEAR_RE.findall(body))

    for candidate in candidates:
        parsed = extract_first_date(candidate)
        if parsed:
            return parsed
    return None


def scrape() -> list[Tournament]:
    session = requests.Session()
    response = session.get(URL, timeout=30, headers=_HEADERS)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("tr")

    tournaments: list[Tournament] = []
    seen: set[tuple[str, str]] = set()
    today = datetime.now().date()
    detail_date_cache: dict[str, date | None] = {}

    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        date_text = _row_text(cells[0])
        name_text = _row_text(cells[1])

        if _is_header_row(date_text, name_text):
            continue
        if _is_past_section(date_text, name_text):
            break

        title = tidy_title(name_text)
        if not title:
            continue

        link = _row_link(row)
        event_date = extract_first_date(date_text)
        if not event_date:
            continue

        row_text = f"{date_text} {name_text}"
        explicit_year = _has_explicit_year(row_text)
        if explicit_year:
            row_year = _extract_year(row_text)
            if row_year is not None and row_year < today.year:
                continue
        else:
            if link not in detail_date_cache:
                detail_date_cache[link] = _detail_date_from_event_page(session, link)
            detail_date = detail_date_cache[link]
            if detail_date:
                event_date = detail_date
            elif "volleyballlife.com/event/" in link.lower() or "volleyballlife.com/events/" in link.lower():
                # If a VolleyballLife row has no explicit year and detail lookup fails,
                # skip it to avoid accidentally including historical events as current-year.
                continue

        if event_date < today:
            continue

        key = (title.lower(), event_date.isoformat())
        if key in seen:
            continue
        seen.add(key)

        tournaments.append(
            Tournament(
                title=title,
                source=SOURCE,
                link=link,
                date=event_date,
                location="Houston, TX",
            )
        )

    return tournaments
