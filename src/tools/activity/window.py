"""Time window parsing for activity retrieval.

The archive stores dates as fixed-width ISO-8601 UTC strings, so every boundary
is normalised to that exact shape. Relative inputs like ``24h`` are resolved
against the current UTC instant, which makes "the last 24 hours" mean the same
thing regardless of the caller's timezone or where a local calendar day happens
to fall.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from src.utils.datetime_parse import parse_iso_datetime_utc

ARCHIVE_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"

_RELATIVE = re.compile(r"^\s*(\d+)\s*([hdwm])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 604800, "m": 60}


class WindowError(ValueError):
    """Raised when a caller supplies a window that cannot be interpreted."""


def to_archive_string(moment: datetime) -> str:
    """Render an instant in the archive's canonical comparison format."""
    return moment.astimezone(UTC).strftime(ARCHIVE_DATE_FORMAT)


def resolve_window(
    since: str,
    until: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime | None]:
    """Resolve *since*/*until* into UTC instants.

    ``since`` accepts an ISO timestamp or a relative span (``24h``, ``7d``,
    ``90m``, ``2w``). ``until`` accepts an ISO timestamp only — a relative upper
    bound reads ambiguously ("24h ago" or "24h from now") and silently guessing
    would move the window under the caller.
    """
    reference = (now or datetime.now(UTC)).astimezone(UTC)

    relative = _RELATIVE.match(since or "")
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        if amount <= 0:
            raise WindowError(f"relative window must be positive, got {since!r}")
        start = reference - timedelta(seconds=amount * _UNIT_SECONDS[unit])
    else:
        parsed = parse_iso_datetime_utc(since)
        if parsed is None:
            raise WindowError(
                f"since must be an ISO timestamp or a relative span like '24h', got {since!r}"
            )
        start = parsed

    end: datetime | None = None
    if until:
        end = parse_iso_datetime_utc(until)
        if end is None:
            raise WindowError(f"until must be an ISO timestamp, got {until!r}")
        if end <= start:
            raise WindowError("until must be later than since")

    return start, end
