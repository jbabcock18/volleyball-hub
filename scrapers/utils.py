from __future__ import annotations

import re
from datetime import date, datetime

from dateutil import parser

_MONTH_TOKEN = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
_MONTH_PATTERN = re.compile(
    rf"\b{_MONTH_TOKEN}[a-z]*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b",
    re.IGNORECASE,
)
_YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")
_ISO_DATE_PATTERN = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_DATE_RANGE_PATTERN = re.compile(
    rf"\b{_MONTH_TOKEN}[a-z]*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\s*(?:-|–|—|\bto\b)\s*(?:{_MONTH_TOKEN}[a-z]*\.?\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b",
    re.IGNORECASE,
)
_DATE_RANGE_CAPTURE_PATTERN = re.compile(
    rf"\b(?P<start_month>{_MONTH_TOKEN})[a-z]*\.?\s+"
    rf"(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|–|—|\bto\b)\s*"
    rf"(?:(?P<end_month>{_MONTH_TOKEN})[a-z]*\.?\s+)?"
    rf"(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s+(?P<year>20\d{{2}}))?\b",
    re.IGNORECASE,
)


def normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_first_date(text: str) -> date | None:
    if not text:
        return None

    iso_match = _ISO_DATE_PATTERN.search(text)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1))
        except ValueError:
            pass

    match = _MONTH_PATTERN.search(text)
    candidate = match.group(0) if match else text
    context_year: int | None = None
    if match:
        # Only inherit a year if it appears close to the matched date text.
        # This avoids picking footer years like "2000" from unrelated page text.
        trailing_window = text[match.end() : min(len(text), match.end() + 80)]
        trailing_years = _YEAR_PATTERN.findall(trailing_window)
        if trailing_years:
            context_year = int(trailing_years[-1])

    candidate_has_year = bool(_YEAR_PATTERN.search(candidate))
    if not candidate_has_year and context_year:
        candidate = f"{candidate}, {context_year}"

    try:
        parsed = parser.parse(candidate, fuzzy=True)
    except (ValueError, OverflowError):
        return None

    inferred_year = bool(_YEAR_PATTERN.search(candidate))
    event_date = parsed.date()
    today = datetime.now().date()

    if not inferred_year:
        try:
            event_date = event_date.replace(year=today.year)
        except ValueError:
            return None
        if event_date < today:
            try:
                event_date = event_date.replace(year=today.year + 1)
            except ValueError:
                return None

    return event_date


def has_date_range(text: str) -> bool:
    if not text:
        return False
    return bool(_DATE_RANGE_PATTERN.search(text))


def date_range_span_days(text: str) -> int | None:
    if not text:
        return None

    match = _DATE_RANGE_CAPTURE_PATTERN.search(text)
    if not match:
        return None

    start_month = match.group("start_month")
    start_day = match.group("start_day")
    end_month = match.group("end_month") or start_month
    end_day = match.group("end_day")

    year = match.group("year")
    if year:
        parsed_year = int(year)
    else:
        context_year_match = _YEAR_PATTERN.search(text)
        if context_year_match:
            parsed_year = int(context_year_match.group(1))
        else:
            fallback = extract_first_date(f"{start_month} {start_day}")
            if fallback is None:
                return None
            parsed_year = fallback.year

    try:
        start_date = parser.parse(f"{start_month} {int(start_day)}, {parsed_year}", fuzzy=False).date()
        end_date = parser.parse(f"{end_month} {int(end_day)}, {parsed_year}", fuzzy=False).date()
    except (ValueError, OverflowError):
        return None

    if end_date < start_date:
        try:
            end_date = end_date.replace(year=end_date.year + 1)
        except ValueError:
            return None

    return (end_date - start_date).days


def is_multiweek_date_range(text: str, min_days: int = 8) -> bool:
    span_days = date_range_span_days(text)
    return span_days is not None and span_days >= min_days


def tidy_title(value: str) -> str:
    value = normalize_ws(value)
    value = re.sub(r"\b(register|details|learn more|click here)\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\|", " ", value)
    return normalize_ws(value)
