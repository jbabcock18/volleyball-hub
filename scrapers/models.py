from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any


@dataclass(slots=True)
class Tournament:
    title: str
    source: str
    link: str
    date: date | None = None
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["date"] = self.date.isoformat() if self.date else None
        return payload
