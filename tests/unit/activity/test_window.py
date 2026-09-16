"""Window parsing, including the day and timezone boundary case (TG-014)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.tools.activity.window import (
    WindowError,
    resolve_window,
    to_archive_string,
)


def test_relative_hours_resolve_against_now():
    now = datetime(2026, 9, 16, 17, 45, 0, tzinfo=UTC)
    start, end = resolve_window("24h", now=now)
    assert start == now - timedelta(hours=24)
    assert end is None


@pytest.mark.parametrize(
    ("spec", "delta"),
    [
        ("90m", timedelta(minutes=90)),
        ("7d", timedelta(days=7)),
        ("2w", timedelta(weeks=2)),
        (" 24H ", timedelta(hours=24)),
    ],
)
def test_relative_units(spec, delta):
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
    start, _ = resolve_window(spec, now=now)
    assert start == now - delta


def test_crossing_local_midnight_stays_a_24_hour_span():
    """A window asked for just after local midnight must not become a calendar day.

    Moscow is UTC+3, so 00:30 MSK on the 17th is 21:30 UTC on the 16th. The
    window has to reach back a real 24 hours, into the previous UTC day.
    """
    moscow_after_midnight = datetime(
        2026, 9, 17, 0, 30, tzinfo=timezone(timedelta(hours=3))
    )
    start, _ = resolve_window("24h", now=moscow_after_midnight)
    assert start == datetime(2026, 9, 15, 21, 30, tzinfo=UTC)
    assert to_archive_string(start) == "2026-09-15T21:30:00+00:00"


def test_non_utc_iso_input_is_normalised_to_utc():
    start, _ = resolve_window("2026-09-16T12:00:00+03:00")
    assert to_archive_string(start) == "2026-09-16T09:00:00+00:00"


def test_archive_string_is_fixed_width():
    rendered = to_archive_string(datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC))
    assert rendered == "2026-01-02T03:04:05+00:00"
    assert len(rendered) == 25


def test_until_must_follow_since():
    with pytest.raises(WindowError):
        resolve_window("2026-09-16T10:00:00+00:00", "2026-09-16T09:00:00+00:00")


def test_relative_until_is_rejected_rather_than_guessed():
    with pytest.raises(WindowError):
        resolve_window("7d", "24h")


def test_garbage_since_is_rejected():
    with pytest.raises(WindowError):
        resolve_window("last tuesday")


def test_zero_span_is_rejected():
    with pytest.raises(WindowError):
        resolve_window("0h")
