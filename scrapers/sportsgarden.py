from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse, urlunparse

from .models import Tournament
from .utils import extract_first_date, is_multiweek_date_range, normalize_ws, tidy_title

SOURCE = "Sports Garden DFW"
URL = "https://cvb.volleyballlife.com/events"
LOCATION = "Dallas-Fort Worth, TX"
_TOURNAMENT_PREFIX = "tournament |"
_LEAGUE_PREFIX = "league |"
_EVENT_PATH_RE = re.compile(r"^/events?/(\d+)$", re.IGNORECASE)
_NON_TITLE_RE = re.compile(
    r"\b(view event|view tournament|register|details|more details|learn more|information|pricing|deadline)\b",
    re.IGNORECASE,
)
_TITLE_HINT_RE = re.compile(
    r"\b(tournament|men'?s|women'?s|coed|avp|blind draw|byo|revco|stop|series|triple crown|purse|spring|summer|fall|open|classic)\b",
    re.IGNORECASE,
)
_DATE_LIKE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_PLAYWRIGHT_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
]


class ScrapeDependencyError(RuntimeError):
    pass


def _optimize_page(page) -> None:
    page.set_default_timeout(30000)

    def _handle_route(route):
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
            return
        route.continue_()

    page.route("**/*", _handle_route)


def _canonical_link(link: str) -> str:
    parsed = urlparse(link)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _is_event_link(link: str) -> bool:
    parsed = urlparse(link)
    path = parsed.path.rstrip("/")
    return parsed.netloc.lower() == urlparse(URL).netloc.lower() and bool(_EVENT_PATH_RE.fullmatch(path))


def _is_tournament_label(label: str) -> bool:
    normalized = normalize_ws(label).lower()
    return normalized.startswith(_TOURNAMENT_PREFIX) or normalized == "tournament"


def _is_league_label(label: str) -> bool:
    normalized = normalize_ws(label).lower()
    return normalized.startswith(_LEAGUE_PREFIX) or normalized == "league"


def _is_tournament_title(value: str) -> bool:
    return normalize_ws(value).lower().startswith("tournament:")


def _clean_title(raw: str) -> str:
    title = tidy_title(raw)
    title = re.sub(r"\s*[-|•]\s*VolleyballLife.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*[-|•]\s*Sports Garden DFW.*$", "", title, flags=re.IGNORECASE)
    title = title.strip(" *|-:\t")
    return normalize_ws(title)


def _score_title(title: str) -> int:
    if not title:
        return -10_000

    normalized = title.lower()
    score = 0
    if _NON_TITLE_RE.search(normalized):
        score -= 220
    if "league" in normalized and "tournament" not in normalized:
        score -= 120
    if _TITLE_HINT_RE.search(normalized):
        score += 35
    if normalized.startswith("tournament:"):
        score += 10
    if 4 <= len(title) <= 140:
        score += 8
    else:
        score -= 10
    if _DATE_LIKE_RE.search(normalized):
        score -= 7
    if re.search(r"[a-z]", normalized):
        score += 3
    return score


def _select_best_title(candidates: list[str]) -> str:
    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        cleaned = _clean_title(candidate)
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)

    if not deduped:
        return ""

    ranked = sorted(deduped, key=_score_title, reverse=True)
    return ranked[0] if _score_title(ranked[0]) >= 8 else ""


def _iter_json_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_objects(child)


def _parse_json_ld_date(json_ld_texts: list[str]):
    for raw in json_ld_texts:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _iter_json_objects(payload):
            for key in ("startDate", "dateStart", "start_date"):
                value = obj.get(key)
                if isinstance(value, str):
                    parsed = extract_first_date(value)
                    if parsed:
                        return parsed
    return None


def _parse_json_ld_titles(json_ld_texts: list[str]) -> list[str]:
    names: list[str] = []
    for raw in json_ld_texts:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _iter_json_objects(payload):
            value = obj.get("name")
            if isinstance(value, str):
                names.append(value)
    return names


def _json_ld_has_multiweek_range(json_ld_texts: list[str]) -> bool:
    for raw in json_ld_texts:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for obj in _iter_json_objects(payload):
            start_raw = obj.get("startDate") or obj.get("dateStart") or obj.get("start_date")
            end_raw = obj.get("endDate") or obj.get("dateEnd") or obj.get("end_date")
            if isinstance(start_raw, str) and isinstance(end_raw, str):
                start_date = extract_first_date(start_raw)
                end_date = extract_first_date(end_raw)
                if start_date and end_date:
                    if end_date < start_date:
                        try:
                            end_date = end_date.replace(year=end_date.year + 1)
                        except ValueError:
                            pass
                    if (end_date - start_date).days >= 8:
                        return True

            for key in ("startDate", "endDate", "date", "eventDate"):
                value = obj.get(key)
                if isinstance(value, str) and is_multiweek_date_range(value):
                    return True
    return False


def _extract_dom_items(page) -> list[dict[str, str]]:
    return page.evaluate(
        """
        () => {
          const rows = [];
          const links = [...document.querySelectorAll('a[href*="/event/"], a[href*="/events/"]')];
          for (const a of links) {
            const href = a.getAttribute('href') || '';
            const absolute = a.href || href;
            const card = a.closest('[class*="event"], [class*="tournament"], article, li, section, tr, div') || a.parentElement;
            const context = card ? (card.innerText || '') : (a.textContent || '');
            const labels = card
              ? [...card.querySelectorAll('div.text-caption.text-grey')].map((el) => (el.textContent || '').trim()).filter(Boolean)
              : [];

            rows.push({
              href: absolute || href,
              text: (a.textContent || '').trim(),
              context: context.trim(),
              label: labels.find(Boolean) || '',
            });
          }
          return rows;
        }
        """
    )


def _extract_href_fallback(html: str) -> list[dict[str, str]]:
    hrefs = set(re.findall(r"https?://[^\"']+/events?/\d+[^\"'#? ]*|/events?/\d+[^\"'#? ]*", html))
    return [{"href": href, "text": "", "context": "", "label": ""} for href in hrefs]


def _first_str(obj: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str):
            cleaned = normalize_ws(value)
            if cleaned:
                return cleaned
    return ""


def _extract_api_items_from_payload(payload) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    expected_username = urlparse(URL).netloc.split(".")[0].lower()
    event_indicator_keys = {
        "startDate",
        "endDate",
        "teamCount",
        "divisionNames",
        "statusId",
        "urlTag",
        "locations",
        "dates",
        "sanctionedBy",
        "ibvl",
        "isPublic",
        "coordinates",
    }
    for obj in _iter_json_objects(payload):
        if not isinstance(obj, dict):
            continue

        indicator_count = sum(1 for key in event_indicator_keys if key in obj)
        if indicator_count < 2:
            continue

        organization = obj.get("organization")
        if isinstance(organization, dict):
            username = normalize_ws(str(organization.get("username", ""))).lower()
            if username and username != expected_username:
                continue

        raw_url = _first_str(
            obj,
            ("url", "eventUrl", "event_url", "link", "permalink", "href", "publicUrl"),
        )
        raw_id = obj.get("eventId", obj.get("event_id", obj.get("id")))

        candidate_link = ""
        if raw_url and ("/event/" in raw_url or "/events/" in raw_url):
            candidate_link = urljoin(URL, raw_url)
        elif indicator_count >= 2 and isinstance(raw_id, int) and raw_id > 100:
            candidate_link = f"https://{urlparse(URL).netloc}/event/{raw_id}"
        elif indicator_count >= 2 and isinstance(raw_id, str) and raw_id.isdigit() and len(raw_id) >= 3:
            candidate_link = f"https://{urlparse(URL).netloc}/event/{raw_id}"

        if not candidate_link:
            continue

        link = _canonical_link(candidate_link)
        if not _is_event_link(link):
            continue

        title = _first_str(obj, ("name", "title", "eventName", "eventTitle"))
        label = _first_str(obj, ("category", "type", "eventType", "listingType", "classification", "kind"))
        host = _first_str(obj, ("host", "organization", "club", "orgName", "eventHost"))
        start = _first_str(obj, ("startDate", "start_date", "date", "eventDate"))
        context = " | ".join(part for part in (title, start, host, label) if part)

        rows.append({"href": link, "text": title, "context": context, "label": label})
    return rows


def _merge_items(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for group in groups:
        for item in group:
            href = item.get("href", "")
            if not href:
                continue
            link = _canonical_link(urljoin(URL, href))
            if not _is_event_link(link):
                continue

            existing = merged.get(link)
            if not existing:
                merged[link] = {
                    "href": link,
                    "text": item.get("text", ""),
                    "context": item.get("context", ""),
                    "label": item.get("label", ""),
                }
                continue

            if len(normalize_ws(item.get("text", ""))) > len(normalize_ws(existing.get("text", ""))):
                existing["text"] = item.get("text", "")
            if len(normalize_ws(item.get("context", ""))) > len(normalize_ws(existing.get("context", ""))):
                existing["context"] = item.get("context", "")
            if len(normalize_ws(item.get("label", ""))) > len(normalize_ws(existing.get("label", ""))):
                existing["label"] = item.get("label", "")
    return list(merged.values())


def _click_pagination_controls(page) -> bool:
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


def _crawl_listing(page, max_rounds: int = 50) -> None:
    previous = 0
    stable_rounds = 0
    for _ in range(max_rounds):
        current = int(
            page.evaluate(
                """
                () => new Set(
                  [...document.querySelectorAll('a[href*="/event/"], a[href*="/events/"]')]
                    .map((a) => a.href || a.getAttribute('href') || '')
                ).size
                """
            )
        )

        clicked = _click_pagination_controls(page)
        if clicked:
            page.wait_for_timeout(1000)

        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(700)

        if current > previous:
            previous = current
            stable_rounds = 0
        else:
            stable_rounds += 1

        if stable_rounds >= 8 and not clicked:
            break


def _extract_labels_from_text(text: str) -> list[str]:
    labels: list[str] = []
    if not text:
        return labels

    for match in re.findall(
        r"\b(?:Tournament|League)\s*\|\s*[^|\n]{2,90}\s*\|\s*(?:Adult|Adults|Junior|Juniors)\b",
        text,
        flags=re.IGNORECASE,
    ):
        labels.append(normalize_ws(match))

    seen: set[str] = set()
    ordered: list[str] = []
    for label in labels:
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(label)
    return ordered


def _extract_detail(page, link: str) -> tuple[list[str], str, list[str], list[str]]:
    page.goto(link, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1200)
    data = page.evaluate(
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

          const labels = [...document.querySelectorAll('div.text-caption.text-grey')]
            .map((el) => (el.textContent || '').trim())
            .filter(Boolean);

          const body = (document.body?.innerText || '').trim();
          const jsonLd = [...document.querySelectorAll('script[type="application/ld+json"]')]
            .map((s) => s.textContent || '')
            .filter(Boolean);

          return { titleCandidates, labels, body, jsonLd };
        }
        """
    )
    return (
        data.get("titleCandidates", []),
        normalize_ws(data.get("body", "")),
        data.get("jsonLd", []),
        data.get("labels", []),
    )


def scrape() -> list[Tournament]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeDependencyError(
            "Playwright is required for Sports Garden DFW scraping. Install dependencies and run 'playwright install chromium'."
        ) from exc

    list_items: list[dict[str, str]] = []
    api_items: list[dict[str, str]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=_PLAYWRIGHT_LAUNCH_ARGS)
        page = browser.new_page()
        _optimize_page(page)

        def on_response(response):
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            low_url = response.url.lower()
            if not any(token in low_url for token in ("/event", "/events", "graphql", "search", "calendar", "list", "summary", "summaries")):
                return
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                return
            api_items.extend(_extract_api_items_from_payload(payload))

        page.on("response", on_response)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector('a[href*="/event/"], a[href*="/events/"]', timeout=20000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(3000)

            _crawl_listing(page)
            dom_items = _extract_dom_items(page)
            fallback_items = _extract_href_fallback(page.content())
            list_items = _merge_items(dom_items, api_items, fallback_items)
        finally:
            browser.close()

    tournaments: list[Tournament] = []
    seen_links: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=_PLAYWRIGHT_LAUNCH_ARGS)
        detail_page = browser.new_page()
        _optimize_page(detail_page)
        try:
            for item in list_items:
                href = item.get("href", "")
                if not href:
                    continue

                link = _canonical_link(urljoin(URL, href))
                if not _is_event_link(link) or link in seen_links:
                    continue
                seen_links.add(link)

                list_context = normalize_ws(item.get("context", ""))
                list_label = normalize_ws(item.get("label", ""))
                list_title = _select_best_title([item.get("text", ""), list_context])

                try:
                    detail_candidates, detail_body, detail_json_ld, detail_labels = _extract_detail(detail_page, link)
                except Exception:  # noqa: BLE001
                    detail_candidates, detail_body, detail_json_ld, detail_labels = [], "", [], []

                labels = [list_label] + [normalize_ws(lbl) for lbl in detail_labels if normalize_ws(lbl)]
                labels.extend(_extract_labels_from_text(list_context))
                labels.extend(_extract_labels_from_text(detail_body))
                labels = list(dict.fromkeys(lbl for lbl in labels if lbl))

                # User rule: multi-week date ranges indicate leagues, not tournaments.
                if (
                    is_multiweek_date_range(list_context)
                    or is_multiweek_date_range(detail_body)
                    or _json_ld_has_multiweek_range(detail_json_ld)
                ):
                    continue

                title_candidates = [list_title] + detail_candidates + _parse_json_ld_titles(detail_json_ld)
                title = _select_best_title(title_candidates)
                if not title:
                    continue

                has_tournament_label = any(_is_tournament_label(lbl) for lbl in labels)
                has_league_label = any(_is_league_label(lbl) for lbl in labels)
                has_tournament_title = _is_tournament_title(title) or any(_is_tournament_title(c) for c in title_candidates)

                if has_league_label and not (has_tournament_label or has_tournament_title):
                    continue
                if not (has_tournament_label or has_tournament_title):
                    if re.search(r"\bleague\b", title, flags=re.IGNORECASE):
                        continue
                    if "league |" in detail_body.lower() and "tournament |" not in detail_body.lower():
                        continue

                date_value = _parse_json_ld_date(detail_json_ld)
                if date_value is None:
                    date_value = extract_first_date(list_context)
                if date_value is None and detail_body:
                    date_value = extract_first_date(detail_body)

                tournaments.append(
                    Tournament(
                        title=title,
                        source=SOURCE,
                        link=link,
                        date=date_value,
                        location=LOCATION,
                    )
                )
        finally:
            browser.close()

    return tournaments
