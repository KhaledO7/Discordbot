from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from zoneinfo import ZoneInfo


def parse_time_string(raw: str) -> Optional[time]:
    cleaned = raw.strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    return None


def format_time_with_zone(target_date: date, scrim_time: time, tz: ZoneInfo) -> str:
    start_dt = datetime.combine(target_date, scrim_time, tzinfo=tz)
    return start_dt.strftime("%I:%M %p %Z").lstrip("0")
