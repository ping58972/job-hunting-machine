"""Strict storage-boundary types for durable IDs and operational timestamps."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from job_hunting_machine.clock import format_utc
from job_hunting_machine.ids import IdKind, validate_id

_UTC_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")


class IdText(TypeDecorator[str]):
    """Store an existing architecture ID; never generate one implicitly."""

    impl = Text
    cache_ok = True

    def __init__(self, kind: IdKind) -> None:
        super().__init__()
        self.kind = kind

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return validate_id(value, self.kind)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        return self.process_bind_param(value, dialect)


class UTCText(TypeDecorator[str]):
    """Store canonical millisecond UTC strings as SQLite TEXT, returning str.

    Repositories use ``format_utc(clock.now())``. Reject rather than silently
    normalize noncanonical input at the persistence boundary. Calendar dates and
    job duration/date descriptions use ordinary Text columns instead.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or _UTC_PATTERN.fullmatch(value) is None:
            raise ValueError("Expected a canonical UTC timestamp: YYYY-MM-DDTHH:MM:SS.sssZ")
        try:
            normalized = format_utc(datetime.fromisoformat(value))
        except ValueError:
            raise ValueError("Invalid UTC timestamp") from None
        if normalized != value:
            raise ValueError("UTC timestamp must be canonical")
        return value

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        return self.process_bind_param(value, dialect)
