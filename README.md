# Texas Beach Volleyball Tournaments

A Flask app that scrapes multiple Texas beach-volleyball facilities and consolidates tournament data into one dashboard.

## What this app does
- Scrapes tournament data from facility-specific sources.
- Normalizes each record to: `title`, `date`, `source`, `location`, `link`.
- Deduplicates records by source + title + date.
- Stores the latest scrape in `data/tournaments.json`.
- Shows only tournaments dated **today or later** in the web UI.
- Exposes a JSON API with cached scrape data.

## Sources
- `https://512beach.com/events`
- `https://atxbeach.volleyballlife.com/events`
- `https://210beach.volleyballlife.com/events`
- `https://cvb.volleyballlife.com/events`
- `https://thirdcoastvolleyball.com/tournaments/tournament-schedule/`

## Current behavior notes
- VolleyballLife facility scrapers use Playwright and network response parsing to capture event listings robustly.
- League filtering is applied for VolleyballLife facilities; multi-week date ranges are treated as league-style events and excluded from tournament output.
- The UI hides past tournaments.
- Cached/API data may still contain past tournaments if they were scraped.

## Requirements
- Python 3.11+ (3.13 is fine)
- `pip`
- Playwright Chromium runtime

## Setup
1. Create and activate a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate
```
2. Install Python dependencies:
```bash
pip install -r requirements.txt
```
3. Install Playwright browser runtime:
```bash
playwright install chromium
```
4. Refresh token is currently hardcoded in app code as `jackiscool`.

## Run locally
```bash
python app.py
```

Open:
- `http://127.0.0.1:8000`

## Deploy on Render
- Build command:
```bash
pip install -r requirements.txt && PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright python -m playwright install chromium
```
- Start command:
```bash
gunicorn --timeout 600 app:app
```
- Environment variables:
  - `PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright`
  - `SCRAPE_USE_SUBPROCESS=1` (recommended on low-memory instances)
- Important: putting `PLAYWRIGHT_BROWSERS_PATH=...` in the build command does not persist to runtime.
  Add it in Render service Environment settings as a real env var too.
- You can use the included `render.yaml` blueprint to avoid manual setup drift.

## Refreshing data
- First deploy note: the app serves cached data only. Trigger one manual refresh after deploy to populate cache.
- Force a fresh scrape in browser:
  - `http://127.0.0.1:8000/?refresh=1&token=jackiscool`
- Programmatic refresh endpoint:
  - `POST /refresh` with one of:
    - query string: `?token=jackiscool`
    - header: `X-Refresh-Token: jackiscool`
    - JSON body: `{"token":"jackiscool"}`

## API
- `GET /api/tournaments`

Response includes:
- `updated_at`
- `errors`
- `tournaments`

## Data file
- Cache path: `data/tournaments.json`

## Troubleshooting
- If results look stale, use `/?refresh=1&token=jackiscool`.
- If VolleyballLife scrapers fail, reinstall Chromium:
```bash
playwright install chromium
```
- If Render times out during refresh, keep `gunicorn --timeout 600 app:app`.
- If Render logs show `Executable doesn't exist ... ms-playwright`, your deploy skipped browser install. Re-run deploy with the build command above.
- If cache is corrupted or outdated, remove it and refresh:
```bash
rm data/tournaments.json
```
