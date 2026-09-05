from datetime import UTC, datetime, timedelta, timezone

import pytest

from job_hunting_machine.clock import FrozenClock, SystemClock, format_utc, unix_milliseconds


def test_frozen_clock_normalizes_an_aware_instant_to_utc() -> None:
    instant = datetime(2026, 9, 5, 4, 23, 31, 123456, tzinfo=timezone(timedelta(hours=-4)))
    clock = FrozenClock(instant)
    assert clock.now() == datetime(2026, 9, 5, 8, 23, 31, 123456, tzinfo=UTC)
    assert clock.now().tzinfo is UTC
    assert format_utc(clock.now()) == "2026-09-05T08:23:31.123Z"


def test_system_clock_returns_current_aware_utc_time() -> None:
    before = datetime.now(UTC)
    actual = SystemClock().now()
    after = datetime.now(UTC)
    assert before <= actual <= after
    assert actual.tzinfo is UTC


def test_naive_datetimes_are_rejected() -> None:
    instant = datetime(2026, 9, 5)
    with pytest.raises(ValueError, match="timezone-aware"):
        FrozenClock(instant)
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc(instant)
    with pytest.raises(ValueError, match="timezone-aware"):
        unix_milliseconds(instant)


def test_unix_milliseconds_truncates_without_float_rounding() -> None:
    assert unix_milliseconds(datetime(1970, 1, 1, 0, 0, 0, 999999, tzinfo=UTC)) == 999
    assert unix_milliseconds(datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)) == -1
    assert unix_milliseconds(datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)) == (
        253402300799999
    )
