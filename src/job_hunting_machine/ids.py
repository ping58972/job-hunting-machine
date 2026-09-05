"""Centralized Architecture v2 identifiers; persistence belongs to later phases.

Generate an ID once at a write boundary and persist it. Reuse that persisted ID
during workflow replay. These helpers do not create tasks or database records.
"""

import re
import secrets
from collections.abc import Callable
from enum import StrEnum

from job_hunting_machine.clock import Clock, SystemClock, unix_milliseconds

ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_PATTERN = r"[0-9A-HJKMNP-TV-Z]{26}"
TASK_ID_PATTERN = rf"^TASK_{ULID_PATTERN}$"
APPLICATION_ID_PATTERN = rf"^APP_{ULID_PATTERN}$"
TASK_ID_REGEX = re.compile(TASK_ID_PATTERN)
APPLICATION_ID_REGEX = re.compile(APPLICATION_ID_PATTERN)


class IdKind(StrEnum):
    """The seven prefixes specified in Architecture v2 section 8."""

    TASK = "TASK"
    APPLICATION = "APP"
    JOB = "JOB"
    APPROVAL = "APR"
    ARTIFACT = "ART"
    CONTACT = "CNT"
    EVENT = "EVT"


class IdGenerator:
    """Create ULIDs with a 48-bit timestamp and 80 cryptographically random bits.

    IDs sort by their encoded millisecond, with random order within that same
    millisecond. There is no shared mutable counter or process-local uniqueness
    dependency. Clock rollback is reflected in the timestamp, not hidden.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._entropy = entropy

    def generate_ulid(self) -> str:
        milliseconds = unix_milliseconds(self._clock.now())
        if not 0 <= milliseconds < 1 << 48:
            raise ValueError("ULID timestamps must fit an unsigned 48-bit millisecond value")
        random_bytes = self._entropy(10)
        if not isinstance(random_bytes, bytes) or len(random_bytes) != 10:
            raise ValueError("The entropy source must return exactly 10 bytes")
        value = (milliseconds << 80) | int.from_bytes(random_bytes, "big")
        characters = ["0"] * 26
        for index in range(25, -1, -1):
            characters[index] = ULID_ALPHABET[value & 31]
            value >>= 5
        return "".join(characters)

    def new(self, kind: IdKind) -> str:
        """Generate a prefixed ID; arbitrary, unregistered prefixes are rejected."""
        if not isinstance(kind, IdKind):
            raise ValueError("An architecture-defined IdKind is required")
        return f"{kind.value}_{self.generate_ulid()}"

    def task_id(self) -> str:
        return self.new(IdKind.TASK)

    def application_id(self) -> str:
        return self.new(IdKind.APPLICATION)
