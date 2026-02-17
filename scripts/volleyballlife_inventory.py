from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from dateutil import parser as date_parser
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE_URL = "https://volleyballlife.com/events"
EVENT_PATH_RE = re.compile(r"^/events?/(\d+)$", re.IGNORECASE)
EVENT_URL_RE = re.compile(r"https?://[^\s\"']+/events?/\d+|/events?/\d+", re.IGNORECASE)
SURFACE_KEYWORDS = {
    "beach": "beach",
    "sand": "beach",
    "grass": "grass",
    "indoor": "indoor",
    "wood": "indoor",
    "hardcourt": "hardcourt",
    "hard court": "hardcourt",
    "outdoor": "outdoor",
}


def normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def canonical_event_link(link: str) -> str:
    parsed = urlparse(link)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def event_id_from_link(link: str) -> str:
    path = urlparse(link).path.rstrip("/")
    match = EVENT_PATH_RE.fullmatch(path)
    return match.group(1) if match else ""


def is_event_link(link: str) -> bool:
    path = urlparse(link).path.rstrip("/")
    return bool(EVENT_PATH_RE.fullmatch(path))


def first_nonempty(*values: str) -> str:
    for value in values:
        value = normalize_ws(value)
        if value:
            return value
    return ""


def string_from_value(value: Any) -> str:
    if isinstance(value, str):
        return normalize_ws(value)
    if isinstance(value, dict):
        return first_nonempty(
            string_from_value(value.get("name")),
            string_from_value(value.get("title")),
            string_from_value(value.get("label")),
            string_from_value(value.get("city")),
        )
    if isinstance(value, list):
        for item in value:
            candidate = string_from_value(item)
            if candidate:
                return candidate
    return ""


def parse_date(value: str) -> str | None:
    value = normalize_ws(value)
    if not value:
        return None
    try:
        parsed = date_parser.parse(value, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    return parsed.date().isoformat()


def parse_json_safely(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def iter_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def extract_event_rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}

    for obj in iter_json_objects(payload):
        if not isinstance(obj, dict):
            continue

        event_indicator_keys = {
            "startDate",
            "endDate",
            "teamCount",
            "divisionNames",
            "statusId",
            "urlTag",
            "locations",
            "dates",
            "surfaceId",
            "genderId",
            "ageTypeId",
            "divisionId",
            "sportId",
        }
        indicator_count = sum(1 for key in event_indicator_keys if key in obj)

        raw_link = ""
        for key in ("url", "eventUrl", "event_url", "href", "link", "permalink", "publicUrl"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                raw_link = value
                break

        raw_id = obj.get("eventId", obj.get("event_id", obj.get("id")))

        candidate_link = ""
        if raw_link:
            candidate_link = canonical_event_link(urljoin(BASE_URL, raw_link))
        if not candidate_link and indicator_count >= 2 and isinstance(raw_id, int) and raw_id > 100:
            candidate_link = f"https://volleyballlife.com/event/{raw_id}"
        if not candidate_link and indicator_count >= 2 and isinstance(raw_id, str) and raw_id.isdigit() and len(raw_id) >= 3:
            candidate_link = f"https://volleyballlife.com/event/{raw_id}"

        if not candidate_link:
            continue
        candidate_link = canonical_event_link(candidate_link)
        if not is_event_link(candidate_link):
            continue

        event_id = event_id_from_link(candidate_link)
        name = first_nonempty(
            string_from_value(obj.get("name")),
            string_from_value(obj.get("title")),
            string_from_value(obj.get("eventName")),
            string_from_value(obj.get("eventTitle")),
        )
        start = first_nonempty(
            string_from_value(obj.get("startDate")),
            string_from_value(obj.get("date")),
            string_from_value(obj.get("eventDate")),
            string_from_value(obj.get("start_date")),
        )
        host = first_nonempty(
            string_from_value(obj.get("host")),
            string_from_value(obj.get("organization")),
            string_from_value(obj.get("club")),
            string_from_value(obj.get("orgName")),
            string_from_value(obj.get("eventHost")),
        )
        city = first_nonempty(string_from_value(obj.get("city")), string_from_value(obj.get("addressCity")))
        state = first_nonempty(
            string_from_value(obj.get("state")),
            string_from_value(obj.get("addressState")),
            string_from_value(obj.get("stateCode")),
        )
        listing_type = first_nonempty(
            string_from_value(obj.get("category")),
            string_from_value(obj.get("type")),
            string_from_value(obj.get("eventType")),
            string_from_value(obj.get("listingType")),
            string_from_value(obj.get("classification")),
            string_from_value(obj.get("kind")),
        )
        surface_id = obj.get("surfaceId")
        division_id = obj.get("divisionId")
        age_type_id = obj.get("ageTypeId")
        gender_id = obj.get("genderId")
        sport_id = obj.get("sportId")
        team_count = obj.get("teamCount")
        status_id = obj.get("statusId")

        location_names = []
        if isinstance(obj.get("locations"), list):
            for loc in obj["locations"]:
                loc_name = string_from_value(loc)
                if loc_name:
                    location_names.append(loc_name)
        primary_location = location_names[0] if location_names else ""

        existing = rows.get(candidate_link, {"event_id": event_id, "url": candidate_link})
        if name and not existing.get("name"):
            existing["name"] = name
        if start and not existing.get("start_raw"):
            existing["start_raw"] = start
            existing["start_date"] = parse_date(start)
        if host and not existing.get("host"):
            existing["host"] = host
        if city and not existing.get("city"):
            existing["city"] = city
        if state and not existing.get("state"):
            existing["state"] = state
        if primary_location and not existing.get("primary_location"):
            existing["primary_location"] = primary_location
        if location_names and not existing.get("location_names"):
            existing["location_names"] = location_names
        if listing_type and not existing.get("listing_type"):
            existing["listing_type"] = listing_type
        if surface_id is not None and existing.get("surface_id") is None:
            existing["surface_id"] = surface_id
        if division_id is not None and existing.get("division_id") is None:
            existing["division_id"] = division_id
        if age_type_id is not None and existing.get("age_type_id") is None:
            existing["age_type_id"] = age_type_id
        if gender_id is not None and existing.get("gender_id") is None:
            existing["gender_id"] = gender_id
        if sport_id is not None and existing.get("sport_id") is None:
            existing["sport_id"] = sport_id
        if team_count is not None and existing.get("team_count") is None:
            existing["team_count"] = team_count
        if status_id is not None and existing.get("status_id") is None:
            existing["status_id"] = status_id
        rows[candidate_link] = existing

    return list(rows.values())


def extract_event_links_from_html(html: str) -> set[str]:
    links: set[str] = set()
    for match in EVENT_URL_RE.findall(html):
        candidate = canonical_event_link(urljoin(BASE_URL, match))
        if is_event_link(candidate):
            links.add(candidate)
    return links


def click_pagination_controls(page) -> bool:
    return bool(
        page.evaluate(
            """
            () => {
              const candidates = [...document.querySelectorAll('button, a[role="button"], a, [role="button"]')];
              const wants = ['load more', 'show more', 'more events', 'next', 'older'];
              for (const el of candidates) {
                const text = (el.textContent || '').trim().toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();
                const likely = wants.some((needle) => text.includes(needle) || aria.includes(needle)) || cls.includes('next');
                if (!likely) continue;

                const style = window.getComputedStyle(el);
                const visible = style && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
                if (!visible) continue;
                if (el.getAttribute('aria-disabled') === 'true' || el.disabled) continue;
                el.click();
                return true;
              }
              return false;
            }
            """
        )
    )


def scroll_all_containers(page) -> None:
    page.evaluate(
        """
        () => {
          window.scrollBy(0, window.innerHeight * 1.5);
          const nodes = [...document.querySelectorAll('*')];
          for (const el of nodes) {
            const style = window.getComputedStyle(el);
            if (!style) continue;
            const y = style.overflowY || style.overflow;
            if (!(y.includes('auto') || y.includes('scroll'))) continue;
            if (el.scrollHeight <= el.clientHeight + 20) continue;
            el.scrollTop = el.scrollHeight;
          }
        }
        """
    )


def collect_listing_links(max_rounds: int) -> tuple[set[str], list[dict[str, Any]], list[dict[str, Any]]]:
    links: set[str] = set()
    captured_payloads: list[dict[str, Any]] = []
    payload_rows_by_link: dict[str, dict[str, Any]] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(response):
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                return

            extracted_rows = extract_event_rows_from_payload(payload)
            if extracted_rows:
                for row in extracted_rows:
                    link = row.get("url")
                    if not isinstance(link, str):
                        continue
                    links.add(link)
                    current = payload_rows_by_link.get(link, {"event_id": row.get("event_id"), "url": link})
                    for key, value in row.items():
                        if key in {"event_id", "url"}:
                            continue
                        if value and not current.get(key):
                            current[key] = value
                    payload_rows_by_link[link] = current
                captured_payloads.append(
                    {
                        "url": response.url,
                        "method": response.request.method,
                        "resource_type": response.request.resource_type,
                        "payload": payload,
                    }
                )

        page.on("response", on_response)

        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector('a[href*="/event/"], a[href*="/events/"]', timeout=20000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(3000)

            previous = 0
            stable_rounds = 0
            for _ in range(max_rounds):
                hrefs = page.evaluate(
                    """
                    () => [...document.querySelectorAll('a[href*="/event/"], a[href*="/events/"]')]
                      .map((a) => a.href || a.getAttribute('href') || '')
                    """
                )
                for href in hrefs:
                    if isinstance(href, str):
                        candidate = canonical_event_link(urljoin(BASE_URL, href))
                        if is_event_link(candidate):
                            links.add(candidate)

                html = page.content()
                links.update(extract_event_links_from_html(html))

                clicked = click_pagination_controls(page)
                if clicked:
                    page.wait_for_timeout(1200)

                scroll_all_containers(page)
                page.wait_for_timeout(800)

                current = len(links)
                if current > previous:
                    previous = current
                    stable_rounds = 0
                else:
                    stable_rounds += 1

                if stable_rounds >= 10 and not clicked:
                    break
        finally:
            browser.close()

    return links, captured_payloads, list(payload_rows_by_link.values())


def extract_surface(*parts: str) -> str | None:
    blob = " ".join(normalize_ws(part).lower() for part in parts if part)
    for needle, mapped in SURFACE_KEYWORDS.items():
        if needle in blob:
            return mapped
    return None


def extract_host_from_labels(labels: list[str]) -> str | None:
    for label in labels:
        pieces = [normalize_ws(piece) for piece in label.split("|")]
        if not pieces:
            continue
        low = [piece.lower() for piece in pieces]
        if len(pieces) >= 2 and low[0] in {"tournament", "league"}:
            return pieces[1]
        if len(pieces) >= 2 and low[1] in {"adult", "adults", "junior", "juniors"}:
            return pieces[0]
    return None


def summarize_payload_keys(payloads: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in payloads:
        payload = item.get("payload")
        for obj in iter_json_objects(payload):
            if not isinstance(obj, dict):
                continue
            for key in obj.keys():
                counter[key] += 1
    return dict(counter.most_common())


def extract_event_metadata(link: str, page) -> dict[str, Any] | None:
    try:
        page.goto(link, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1000)
    except Exception:  # noqa: BLE001
        return None

    payload = page.evaluate(
        """
        () => {
          const titleCandidates = [
            document.title || '',
            document.querySelector('meta[property="og:title"]')?.content || '',
            document.querySelector('meta[name="twitter:title"]')?.content || '',
            document.querySelector('h1')?.textContent || '',
            document.querySelector('h2')?.textContent || '',
            document.querySelector('h3')?.textContent || '',
          ].map((s) => (s || '').trim()).filter(Boolean);

          const labels = [...document.querySelectorAll('div.text-caption.text-grey, [class*="caption"], [class*="chip"]')]
            .map((el) => (el.textContent || '').trim())
            .filter(Boolean);

          const addresses = [
            ...[...document.querySelectorAll('[itemprop="streetAddress"], [itemprop="address"], [class*="address"], [class*="Address"]')]
              .map((el) => (el.textContent || '').trim())
              .filter(Boolean),
            ...[...document.querySelectorAll('a[href*="maps.google"], a[href*="google.com/maps"]')]
              .map((el) => (el.textContent || '').trim())
              .filter(Boolean)
          ];

          const body = (document.body?.innerText || '').trim();
          const jsonLd = [...document.querySelectorAll('script[type="application/ld+json"]')]
            .map((s) => s.textContent || '')
            .filter(Boolean);

          return { titleCandidates, labels, addresses, body, jsonLd };
        }
        """
    )

    title_candidates = [normalize_ws(value) for value in payload.get("titleCandidates", []) if normalize_ws(value)]
    labels = [normalize_ws(value) for value in payload.get("labels", []) if normalize_ws(value)]
    addresses = [normalize_ws(value) for value in payload.get("addresses", []) if normalize_ws(value)]
    body = normalize_ws(payload.get("body", ""))

    json_ld_raw = payload.get("jsonLd", [])
    json_ld_objects: list[dict[str, Any]] = []
    for raw in json_ld_raw:
        parsed = parse_json_safely(raw)
        if parsed is None:
            continue
        for obj in iter_json_objects(parsed):
            if isinstance(obj, dict):
                json_ld_objects.append(obj)

    json_ld_names = [normalize_ws(str(obj.get("name", ""))) for obj in json_ld_objects if obj.get("name")]
    json_ld_names = [value for value in json_ld_names if value]

    name = first_nonempty(*json_ld_names, *(title_candidates[:4]))
    if not name:
        return None

    date_candidates: list[str] = []
    for obj in json_ld_objects:
        for key in ("startDate", "date", "dateStart", "start_date"):
            value = obj.get(key)
            if isinstance(value, str):
                date_candidates.append(value)
    date_candidates.extend(labels)
    date_candidates.append(body)

    event_date = None
    for candidate in date_candidates:
        event_date = parse_date(candidate)
        if event_date:
            break

    location = first_nonempty(*(addresses[:3]))
    host = extract_host_from_labels(labels)
    surface = extract_surface(name, " ".join(labels), body)

    city = None
    state = None
    city_state_match = re.search(r"\b([A-Za-z .'-]+),\s*([A-Z]{2})\b", location or body)
    if city_state_match:
        city = normalize_ws(city_state_match.group(1))
        state = normalize_ws(city_state_match.group(2))

    json_ld_key_counter: Counter[str] = Counter()
    for obj in json_ld_objects:
        for key in obj.keys():
            json_ld_key_counter[key] += 1

    return {
        "event_id": event_id_from_link(link),
        "url": link,
        "name": name,
        "date": event_date,
        "host": host,
        "court_type": surface,
        "location": location or None,
        "city": city,
        "state": state,
        "labels": labels,
        "title_candidates": title_candidates,
        "json_ld_keys": sorted(json_ld_key_counter.keys()),
    }


def summarize_metadata(events: list[dict[str, Any]], payloads: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ["name", "date", "host", "court_type", "location", "city", "state"]
    coverage: dict[str, dict[str, Any]] = {}
    total = len(events)

    for field in fields:
        count = sum(1 for event in events if event.get(field))
        coverage[field] = {
            "count": count,
            "pct": round((count / total) * 100, 2) if total else 0,
        }

    label_counter: Counter[str] = Counter()
    json_ld_key_counter: Counter[str] = Counter()
    host_counter: Counter[str] = Counter()
    surface_counter: Counter[str] = Counter()

    for event in events:
        for label in event.get("labels", []):
            label_counter[label] += 1
        for key in event.get("json_ld_keys", []):
            json_ld_key_counter[key] += 1
        host = event.get("host")
        if host:
            host_counter[host] += 1
        surface = event.get("court_type")
        if surface:
            surface_counter[surface] += 1

    common_fields_for_all = [field for field, meta in coverage.items() if meta["count"] == total and total > 0]

    return {
        "total_events": total,
        "common_fields_for_all_events": common_fields_for_all,
        "field_coverage": coverage,
        "top_hosts": host_counter.most_common(50),
        "surface_breakdown": surface_counter.most_common(),
        "top_labels": label_counter.most_common(100),
        "top_json_ld_keys": json_ld_key_counter.most_common(100),
        "top_api_payload_keys": list(summarize_payload_keys(payloads).items())[:120],
    }


def summarize_listing_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    fields = [
        "name",
        "start_raw",
        "start_date",
        "host",
        "city",
        "state",
        "primary_location",
        "listing_type",
        "surface_id",
        "division_id",
        "age_type_id",
        "gender_id",
        "sport_id",
        "team_count",
        "status_id",
    ]
    coverage: dict[str, dict[str, Any]] = {}
    for field in fields:
        count = sum(1 for row in rows if row.get(field))
        coverage[field] = {"count": count, "pct": round((count / total) * 100, 2) if total else 0}

    type_counter: Counter[str] = Counter()
    for row in rows:
        listing_type = row.get("listing_type")
        if listing_type:
            type_counter[normalize_ws(str(listing_type))] += 1

    return {
        "total_listing_rows": total,
        "field_coverage": coverage,
        "top_listing_types": type_counter.most_common(100),
    }


def run(max_rounds: int, output_dir: Path, limit_details: int | None = None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    links, payloads, listing_rows = collect_listing_links(max_rounds=max_rounds)
    ordered_links = sorted(links, key=lambda value: int(event_id_from_link(value) or 0), reverse=True)

    if limit_details is not None:
        ordered_links = ordered_links[:limit_details]

    link_records = [{"event_id": event_id_from_link(link), "url": link} for link in sorted(links)]
    core_tournaments = [
        row for row in listing_rows if row.get("name") and row.get("start_date")
    ]
    core_tournaments = sorted(
        core_tournaments,
        key=lambda row: (row.get("start_date", "9999-12-31"), row.get("name", "").lower()),
    )
    events: list[dict[str, Any]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for link in ordered_links:
                event = extract_event_metadata(link, page)
                if event:
                    events.append(event)
        finally:
            browser.close()

    detail_summary = summarize_metadata(events, payloads)
    listing_summary = summarize_listing_rows(listing_rows)
    summary = {
        "discovered_link_count": len(links),
        "core_tournament_count": len(core_tournaments),
        "detail_record_count": len(events),
        "detail_limit_applied": limit_details,
        "detail_metadata_summary": detail_summary,
        "listing_payload_summary": listing_summary,
    }

    listing_path = output_dir / "volleyballlife_all_tournaments.json"
    links_path = output_dir / "volleyballlife_event_links.json"
    listing_rows_path = output_dir / "volleyballlife_listing_rows.json"
    core_tournaments_path = output_dir / "volleyballlife_core_tournaments.json"
    summary_path = output_dir / "volleyballlife_metadata_summary.json"

    listing_path.write_text(json.dumps(events, indent=2), encoding="utf-8")
    links_path.write_text(json.dumps(link_records, indent=2), encoding="utf-8")
    listing_rows_path.write_text(json.dumps(listing_rows, indent=2), encoding="utf-8")
    core_tournaments_path.write_text(json.dumps(core_tournaments, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Discovered listing links: {len(links)}")
    print(f"Detail records written: {len(events)}")
    print(f"Wrote: {links_path}")
    print(f"Wrote: {listing_rows_path}")
    print(f"Wrote: {core_tournaments_path}")
    print(f"Wrote: {listing_path}")
    print(f"Wrote: {summary_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover all VolleyballLife events and profile available metadata.")
    parser.add_argument("--max-rounds", type=int, default=90, help="Max listing crawl rounds (scroll/click cycles).")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
        help="Directory where output JSON files are written.",
    )
    parser.add_argument(
        "--limit-details",
        type=int,
        default=None,
        help="Optional cap for detail pages to fetch (for quick tests).",
    )
    args = parser.parse_args()

    run(max_rounds=args.max_rounds, output_dir=args.output_dir, limit_details=args.limit_details)


if __name__ == "__main__":
    main()
