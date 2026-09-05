"""One injectable source of wall-clock time and canonical UTC formatting."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(UTC)


class Clock(Protocol):
    """Return timezone-aware time; inject a clock at service boundaries."""

    def now(self) -> datetime:
        """Return the current wall-clock instant."""
        ...


class SystemClock:
    """Production wall clock, always expressed in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FrozenClock:
    """An immutable clock for deterministic tests and reproducible operations."""

    instant: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "instant", _as_utc(self.instant))

    def now(self) -> datetime:
        return self.instant


def format_utc(value: datetime) -> str:
    """Format an aware instant as ISO-8601 UTC with milliseconds and ``Z``."""
    return _as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def unix_milliseconds(value: datetime) -> int:
    """Return integer milliseconds since the Unix epoch without float rounding."""
    elapsed = _as_utc(value) - _UNIX_EPOCH
    return (elapsed.days * 86_400 + elapsed.seconds) * 1_000 + elapsed.microseconds // 1_000
