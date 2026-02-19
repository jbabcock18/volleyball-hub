from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from scrapers import Tournament, collect

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "data" / "tournaments.json"
REFRESH_LOCK_PATH = BASE_DIR / "data" / "refresh.lock"
REFRESH_SCRIPT_PATH = BASE_DIR / "scripts" / "refresh_cache.py"
REFRESH_LOCK_STALE_SECONDS = 60 * 60
REFRESH_TOKEN = "jackiscool"

# Keep runtime lookup aligned with Render build installs, without breaking local dev.
_project_playwright_path = BASE_DIR / ".playwright"
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and _project_playwright_path.exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_project_playwright_path)

app = Flask(__name__)


def _format_month_day_year(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _format_updated_at(value: str | None) -> str:
    if not value:
        return "Not yet scraped"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value

    hour = parsed.strftime("%I").lstrip("0") or "12"
    formatted = f"{parsed.strftime('%b')} {parsed.day}, {parsed.year} at {hour}:{parsed.strftime('%M')} {parsed.strftime('%p')}"
    if value.endswith("Z"):
        formatted += " UTC"
    return formatted


@app.template_filter("display_date")
def display_date(value: date | None) -> str:
    return _format_month_day_year(value) if value else "TBD"


def _serialize(tournaments: list[Tournament], errors: list[str]) -> dict:
    return {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "errors": errors,
        "tournaments": [t.to_dict() for t in tournaments],
    }


def _deserialize(payload: dict) -> tuple[list[Tournament], list[str], str | None]:
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
    return tournaments, payload.get("errors", []), payload.get("updated_at")


def save_cache(tournaments: list[Tournament], errors: list[str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(_serialize(tournaments, errors), indent=2), encoding="utf-8")


def load_cache() -> tuple[list[Tournament], list[str], str | None]:
    if not CACHE_PATH.exists():
        return [], [], None
    payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return _deserialize(payload)


def refresh_data() -> tuple[list[Tournament], list[str], str]:
    tournaments, errors = collect()
    payload = _serialize(tournaments, errors)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tournaments, errors, payload["updated_at"]


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_async_refresh() -> bool:
    override = os.getenv("ASYNC_REFRESH")
    if override is not None:
        return _is_truthy(override)
    return _is_truthy(os.getenv("RENDER"))


def _refresh_lock_is_stale() -> bool:
    if not REFRESH_LOCK_PATH.exists():
        return False
    try:
        age_seconds = time.time() - REFRESH_LOCK_PATH.stat().st_mtime
    except OSError:
        return False
    return age_seconds > REFRESH_LOCK_STALE_SECONDS


def _refresh_in_progress() -> bool:
    if REFRESH_LOCK_PATH.exists() and _refresh_lock_is_stale():
        REFRESH_LOCK_PATH.unlink(missing_ok=True)
        return False
    return REFRESH_LOCK_PATH.exists()


def _acquire_refresh_lock() -> bool:
    REFRESH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if REFRESH_LOCK_PATH.exists() and _refresh_lock_is_stale():
        REFRESH_LOCK_PATH.unlink(missing_ok=True)

    try:
        fd = os.open(REFRESH_LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False

    with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
        lock_file.write(f"{os.getpid()} {datetime.utcnow().isoformat()}Z\n")
    return True


def _start_background_refresh() -> bool:
    if not _acquire_refresh_lock():
        return False

    env = os.environ.copy()
    env["REFRESH_LOCK_PATH"] = str(REFRESH_LOCK_PATH)
    try:
        subprocess.Popen(
            [sys.executable, str(REFRESH_SCRIPT_PATH)],
            cwd=str(BASE_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        REFRESH_LOCK_PATH.unlink(missing_ok=True)
        return False

    return True


def _read_refresh_token_from_request() -> str | None:
    token = request.args.get("token") or request.headers.get("X-Refresh-Token")
    if token:
        return token

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        token = payload.get("token")
        if isinstance(token, str):
            return token

    form_token = request.form.get("token")
    return form_token if isinstance(form_token, str) else None


def require_refresh_token() -> None:
    supplied = _read_refresh_token_from_request()
    if not supplied or not hmac.compare_digest(supplied, REFRESH_TOKEN):
        abort(403, description="Invalid refresh token.")


@app.get("/")
def index():
    force_refresh = request.args.get("refresh") == "1"
    today = datetime.now().date()
    refresh_notice = None

    if force_refresh:
        require_refresh_token()
        if _should_async_refresh():
            started = _start_background_refresh()
            refresh_notice = "Refresh started. Reload in a minute for updated tournaments." if started else "Refresh already in progress."
            tournaments, errors, updated_at = load_cache()
        else:
            tournaments, errors, updated_at = refresh_data()
    else:
        tournaments, errors, updated_at = load_cache()
        if _refresh_in_progress():
            refresh_notice = "Refresh in progress. Data will update shortly."

    # Display only today/future tournaments while preserving full cached dataset.
    visible_tournaments = [t for t in tournaments if t.date and t.date >= today]
    sources = sorted({t.source for t in visible_tournaments})

    return render_template(
        "index.html",
        tournaments=visible_tournaments,
        errors=errors,
        updated_at=_format_updated_at(updated_at),
        sources=sources,
        refresh_notice=refresh_notice,
    )


@app.post("/refresh")
def refresh():
    require_refresh_token()
    if _should_async_refresh():
        _start_background_refresh()
        return redirect(url_for("index"))

    refresh_data()
    return redirect(url_for("index"))


@app.get("/api/tournaments")
def tournaments_api():
    force_refresh = request.args.get("refresh") == "1"
    refresh_status = "idle"
    if force_refresh:
        require_refresh_token()
        if _should_async_refresh():
            started = _start_background_refresh()
            refresh_status = "started" if started else "in_progress"
            tournaments, errors, updated_at = load_cache()
        else:
            tournaments, errors, updated_at = refresh_data()
            refresh_status = "completed"
    else:
        tournaments, errors, updated_at = load_cache()
        if _refresh_in_progress():
            refresh_status = "in_progress"
    return jsonify(
        {
            "updated_at": updated_at,
            "errors": errors,
            "tournaments": [t.to_dict() for t in tournaments],
            "refresh_status": refresh_status,
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=8000)
