"""Shared repository infrastructure; the caller owns the transaction."""

import json
from collections.abc import Mapping

from sqlalchemy.orm import Session

from job_hunting_machine.clock import Clock, SystemClock, format_utc
from job_hunting_machine.ids import IdGenerator


class RecordNotFoundError(LookupError):
    """The requested durable record does not exist."""


class ReplayConflictError(ValueError):
    """An idempotency identity was reused for a different operation."""


class ConcurrentUpdateError(RuntimeError):
    """The caller's expected version or state is no longer current."""


class Repository:
    """Repositories flush but never commit; use ``Database.transaction()``."""

    def __init__(
        self,
        session: Session,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.session = session
        self.clock = clock if clock is not None else SystemClock()
        self.ids = ids if ids is not None else IdGenerator(self.clock)

    def timestamp(self) -> str:
        return format_utc(self.clock.now())


def canonical_json(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def required_text(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("A non-empty value is required")
    return value
