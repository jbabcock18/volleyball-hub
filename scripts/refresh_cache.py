from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers import collect  # noqa: E402

CACHE_PATH = ROOT / "data" / "tournaments.json"
LOCK_PATH = Path(os.getenv("REFRESH_LOCK_PATH", str(ROOT / "data" / "refresh.lock")))
ERROR_LOG_PATH = ROOT / "data" / "refresh_error.log"


def _utc_now_isoz() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    try:
        tournaments, errors = collect()
        payload = {
            "updated_at": _utc_now_isoz(),
            "errors": errors,
            "tournaments": [t.to_dict() for t in tournaments],
        }
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = CACHE_PATH.with_name(f"{CACHE_PATH.name}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(CACHE_PATH)
        return 0
    except Exception:  # noqa: BLE001
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ERROR_LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        return 1
    finally:
        LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
