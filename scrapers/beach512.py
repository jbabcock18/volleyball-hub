from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup, Tag

from .models import Tournament
from .utils import extract_first_date, normalize_ws, tidy_title

SOURCE = "512 Beach"
URL = "https://512beach.com/events"
_EVENT_PATH_RE = re.compile(r"^/events/(\d+)$", re.IGNORECASE)
_CTA_RE = re.compile(r"\b(register|more details|details|learn more)\b", re.IGNORECASE)
_NON_TITLE_RE = re.compile(
    r"\b(view event|register|more details|details|learn more|early\s*,?\s*regular\s*,?\s*&?\s*late\s*registration|registration|pricing|deadline|ticket)\b",
    re.IGNORECASE,
)
_TITLE_HINT_RE = re.compile(
    r"\b(men'?s|women'?s|coed|avp|blind draw|byo|stop|series|tournament|triple crown|revco|spring|summer|fall)\b",
    re.IGNORECASE,
)
_DATE_LIKE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_PLAYWRIGHT_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
]


def _canonical_event_link(link: str) -> str:
    parsed = urlparse(link)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _optimize_page(page) -> None:
    page.set_default_timeout(30000)

    def _handle_route(route):
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
            return
        route.continue_()

    page.route("**/*", _handle_route)


def _looks_like_event_link(link: str) -> bool:
    path = urlparse(link).path.rstrip("/")
    return bool(_EVENT_PATH_RE.fullmatch(path))


def _event_id(link: str) -> int:
    path = urlparse(link).path.rstrip("/")
    match = _EVENT_PATH_RE.fullmatch(path)
    if not match:
        return 0
    return int(match.group(1))


def _is_generic_title(title: str) -> bool:
    normalized = normalize_ws(title).lower()
    if not normalized:
        return True
    generic_phrases = (
        "fiveonetwo beach",
        "512 beach volleyball",
        "all things beach volleyball in austin",
        "austin beach volleyball",
    )
    return any(phrase in normalized for phrase in generic_phrases)


def _clean_title(raw: str) -> str:
    title = tidy_title(raw)
    title = re.sub(r"\s*[-|•]\s*512 Beach.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"^\s*tournament:\s*", "", title, flags=re.IGNORECASE)
    title = title.strip(" *|-:\t")
    title = normalize_ws(title)
    return title


def _score_title(title: str) -> int:
    if not title:
        return -10_000

    normalized = title.lower()
    if _is_generic_title(title):
        return -1000

    score = 0
    if _NON_TITLE_RE.search(normalized):
        score -= 200
    if _TITLE_HINT_RE.search(normalized):
        score += 40
    if 4 <= len(title) <= 100:
        score += 10
    else:
        score -= 10
    if _DATE_LIKE_RE.search(normalized):
        score -= 8
    if re.search(r"\b\d{4}\b", normalized):
        score -= 3
    if re.search(r"[a-z]", normalized):
        score += 3

    return score


def _select_best_title(candidates: list[str]) -> str:
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _clean_title(candidate)
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)

    if not deduped:
        return ""

    scored = sorted(deduped, key=_score_title, reverse=True)
    return scored[0] if _score_title(scored[0]) >= 8 else ""


def _extract_event_links_from_html(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()

    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        raw_link = urljoin(URL, href)
        text = anchor.get_text(" ", strip=True)
        if not (_looks_like_event_link(raw_link) or _CTA_RE.search(text)):
            continue

        link = _canonical_event_link(raw_link)
        if _looks_like_event_link(link):
            links.add(link)

    for match in re.findall(r"https?://512beach\.com/events/\d+\b|/events/\d+\b", html, flags=re.IGNORECASE):
        link = _canonical_event_link(urljoin(URL, match))
        if _looks_like_event_link(link):
            links.add(link)

    return links


def _extract_json_ld_blocks(soup: BeautifulSoup) -> list[dict]:
    blocks: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            blocks.extend(item for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict):
            blocks.append(payload)
    return blocks


def _extract_title_from_detail(soup: BeautifulSoup) -> str:
    candidates: list[str] = []

    for selector, attr in (
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
        ("h1", None),
        ("h2", None),
        ("h3", None),
        ("h4", None),
        ("title", None),
    ):
        tag = soup.select_one(selector)
        if not tag:
            continue
        raw = tag.get(attr, "") if attr else tag.get_text(" ", strip=True)
        candidates.append(raw)

    for block in _extract_json_ld_blocks(soup):
        value = block.get("name")
        if isinstance(value, str):
            candidates.append(value)

    return _select_best_title(candidates)


def _extract_date_from_detail(soup: BeautifulSoup):
    for block in _extract_json_ld_blocks(soup):
        for key in ("startDate", "dateStart", "start_date"):
            value = block.get(key)
            if isinstance(value, str):
                date_value = extract_first_date(value)
                if date_value:
                    return date_value

    for node in soup.find_all(string=re.compile(r"\bdate\b", re.IGNORECASE)):
        parent = node.parent
        if not isinstance(parent, Tag):
            continue
        snippet = normalize_ws(parent.get_text(" ", strip=True))
        if not snippet or len(snippet) > 220:
            continue
        date_value = extract_first_date(snippet)
        if date_value:
            return date_value

    body_text = normalize_ws(soup.get_text(" ", strip=True))
    return extract_first_date(body_text)


def _links_from_sitemap(session: requests.Session) -> set[str]:
    links: set[str] = set()
    for site_url in ("https://512beach.com/sitemap.xml", "https://512beach.com/sitemap_index.xml"):
        try:
            response = session.get(site_url, timeout=30, headers=_REQUEST_HEADERS)
            response.raise_for_status()
        except requests.RequestException:
            continue

        text = response.text
        for match in re.findall(r"https?://512beach\.com/events/\d+\b|/events/\d+\b", text, flags=re.IGNORECASE):
            link = _canonical_event_link(urljoin(URL, match))
            if _looks_like_event_link(link):
                links.add(link)

        try:
            root = ElementTree.fromstring(text)
            for node in root.findall(".//{*}loc"):
                link = _canonical_event_link(normalize_ws(node.text or ""))
                if _looks_like_event_link(link):
                    links.add(link)
        except ElementTree.ParseError:
            for loc in re.findall(r"<loc>(.*?)</loc>", text, flags=re.IGNORECASE | re.DOTALL):
                link = _canonical_event_link(normalize_ws(unescape(loc)))
                if _looks_like_event_link(link):
                    links.add(link)

    return links


def _scrape_detail(session: requests.Session, link: str) -> Tournament | None:
    try:
        response = session.get(link, timeout=30, headers=_REQUEST_HEADERS)
        response.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    title = _extract_title_from_detail(soup)
    date_value = _extract_date_from_detail(soup)
    if not title:
        return None

    return Tournament(
        title=title,
        source=SOURCE,
        link=link,
        date=date_value,
        location="Austin, TX",
    )


def _links_from_playwright() -> set[str]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return set()

    links: set[str] = set()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_PLAYWRIGHT_LAUNCH_ARGS)
            page = browser.new_page()
            _optimize_page(page)
            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_selector('a[href*="/events/"]', timeout=12000)
                except PlaywrightTimeoutError:
                    page.wait_for_timeout(2500)

                hrefs = page.evaluate(
                    """
                    () => [...document.querySelectorAll('a[href]')]
                      .map((a) => a.href || a.getAttribute('href') || '')
                    """
                )
                for href in hrefs:
                    if not isinstance(href, str):
                        continue
                    link = _canonical_event_link(urljoin(URL, href))
                    if _looks_like_event_link(link):
                        links.add(link)

                html = page.content()
                for match in re.findall(r"https?://512beach\\.com/events/\\d+\\b|/events/\\d+\\b", html, flags=re.IGNORECASE):
                    link = _canonical_event_link(urljoin(URL, match))
                    if _looks_like_event_link(link):
                        links.add(link)
            finally:
                browser.close()
    except Exception:  # noqa: BLE001
        return set()

    return links


def _scrape_details_with_playwright(links: list[str]) -> list[Tournament]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    tournaments: list[Tournament] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_PLAYWRIGHT_LAUNCH_ARGS)
            page = browser.new_page()
            _optimize_page(page)
            try:
                for link in links:
                    try:
                        page.goto(link, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(1000)
                    except Exception:  # noqa: BLE001
                        continue
                    extracted = page.evaluate(
                        """
                        () => {
                          const candidates = [
                            document.title || '',
                            document.querySelector('meta[property="og:title"]')?.content || '',
                            document.querySelector('meta[name="twitter:title"]')?.content || '',
                            document.querySelector('h1')?.textContent || '',
                            document.querySelector('h2')?.textContent || '',
                            document.querySelector('h3')?.textContent || '',
                            document.querySelector('h4')?.textContent || '',
                          ].map((s) => (s || '').trim()).filter(Boolean);
                          const body = (document.body?.innerText || '').trim();
                          const jsonLd = [...document.querySelectorAll('script[type="application/ld+json"]')]
                            .map((s) => s.textContent || '')
                            .filter(Boolean);
                          return { candidates, body, jsonLd };
                        }
                        """
                    )

                    candidates = list(extracted.get("candidates", []))
                    for raw_json in extracted.get("jsonLd", []):
                        try:
                            payload = json.loads(raw_json)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict) and isinstance(payload.get("name"), str):
                            candidates.append(payload["name"])

                    title = _select_best_title(candidates)
                    if not title:
                        continue

                    body = normalize_ws(extracted.get("body", ""))
                    tournaments.append(
                        Tournament(
                            title=title,
                            source=SOURCE,
                            link=link,
                            date=extract_first_date(body),
                            location="Austin, TX",
                        )
                    )
            finally:
                browser.close()
    except Exception:  # noqa: BLE001
        return []

    return tournaments


def _sorted_event_links(links: set[str]) -> list[str]:
    return sorted(links, key=lambda link: _event_id(link), reverse=True)


def scrape() -> list[Tournament]:
    session = requests.Session()

    event_links: set[str] = set()
    try:
        response = session.get(URL, timeout=30, headers=_REQUEST_HEADERS)
        response.raise_for_status()
        event_links |= _extract_event_links_from_html(response.text)
    except requests.RequestException:
        pass

    if not event_links:
        event_links |= _links_from_sitemap(session)
    if not event_links:
        event_links |= _links_from_playwright()

    tournaments: list[Tournament] = []
    ordered_links = _sorted_event_links(event_links)
    for link in ordered_links:
        tournament = _scrape_detail(session, link)
        if tournament:
            tournaments.append(tournament)

    if not tournaments and ordered_links:
        tournaments = _scrape_details_with_playwright(ordered_links)

    return tournaments
