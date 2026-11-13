"""Date-range parsing: absolute (2026-01-31) or relative (30d, 12w, 6m, 1y) values."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

_RELATIVE = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)
_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def parse_date(value: str | None, end_of_day: bool = False, now: datetime | None = None) -> datetime | None:
    """Local-time datetime, or None for an empty value. `end_of_day` makes absolute dates inclusive."""
    value = (value or "").strip()
    if not value:
        return None
    now = now or datetime.now().astimezone()
    if m := _RELATIVE.match(value):
        return now - timedelta(days=int(m.group(1)) * _DAYS[m.group(2).lower()])
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Can't understand date '{value}'. Use YYYY-MM-DD or e.g. 30d, 12w, 6m, 1y.") from None
    return datetime.combine(day, time.max if end_of_day else time.min).astimezone()


def describe_range(since: datetime | None, until: datetime | None) -> str:
    if not since and not until:
        return "all time"
    start = since.date().isoformat() if since else "the beginning"
    end = until.date().isoformat() if until else "today"
    return f"{start} → {end}"
