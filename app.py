from __future__ import annotations

import hmac
import json
from datetime import date, datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from scrapers import Tournament, collect

BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = BASE_DIR / "data" / "tournaments.json"
REFRESH_TOKEN = "jackiscool"

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

    if force_refresh:
        require_refresh_token()

    if force_refresh or not CACHE_PATH.exists():
        tournaments, errors, updated_at = refresh_data()
    else:
        tournaments, errors, updated_at = load_cache()
        if not tournaments:
            tournaments, errors, updated_at = refresh_data()

    # Display only today/future tournaments while preserving full cached dataset.
    visible_tournaments = [t for t in tournaments if t.date and t.date >= today]
    sources = sorted({t.source for t in visible_tournaments})

    return render_template(
        "index.html",
        tournaments=visible_tournaments,
        errors=errors,
        updated_at=_format_updated_at(updated_at),
        sources=sources,
    )


@app.post("/refresh")
def refresh():
    require_refresh_token()
    refresh_data()
    return redirect(url_for("index"))


@app.get("/api/tournaments")
def tournaments_api():
    if not CACHE_PATH.exists():
        tournaments, errors, updated_at = refresh_data()
        return jsonify(
            {
                "updated_at": updated_at,
                "errors": errors,
                "tournaments": [t.to_dict() for t in tournaments],
            }
        )

    tournaments, errors, updated_at = load_cache()
    if not tournaments:
        tournaments, errors, updated_at = refresh_data()
    return jsonify(
        {
            "updated_at": updated_at,
            "errors": errors,
            "tournaments": [t.to_dict() for t in tournaments],
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=8000)
