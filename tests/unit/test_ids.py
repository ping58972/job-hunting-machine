import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from job_hunting_machine.clock import FrozenClock, unix_milliseconds
from job_hunting_machine.ids import (
    APPLICATION_ID_PATTERN,
    TASK_ID_PATTERN,
    ULID_ALPHABET,
    IdGenerator,
    IdKind,
    validate_id,
)


def test_task_and_application_ids_match_architecture_regex() -> None:
    generator = IdGenerator()
    assert re.fullmatch(TASK_ID_PATTERN, generator.task_id())
    assert re.fullmatch(APPLICATION_ID_PATTERN, generator.application_id())


@pytest.mark.parametrize("kind", list(IdKind))
def test_all_architecture_prefixes_are_centralized(kind: IdKind) -> None:
    result = IdGenerator().new(kind)
    assert re.fullmatch(rf"{kind.value}_[0-9A-HJKMNP-TV-Z]{{26}}", result)


def test_ulid_has_exact_timestamp_and_entropy_layout() -> None:
    instant = datetime(2026, 9, 5, 8, 23, 31, 123456, tzinfo=UTC)
    entropy = bytes.fromhex("0123456789abcdef0123")
    requested_sizes: list[int] = []

    def fixed_entropy(size: int) -> bytes:
        requested_sizes.append(size)
        return entropy

    ulid = IdGenerator(FrozenClock(instant), fixed_entropy).generate_ulid()
    decoded = 0
    for character in ulid:
        decoded = decoded * 32 + ULID_ALPHABET.index(character)
    assert decoded >> 80 == unix_milliseconds(instant)
    assert (decoded & ((1 << 80) - 1)).to_bytes(10, "big") == entropy
    assert requested_sizes == [10]
    assert ulid[0] in "01234567"


def test_ulid_epoch_zero_vector() -> None:
    generator = IdGenerator(FrozenClock(datetime(1970, 1, 1, tzinfo=UTC)), bytes)
    assert generator.generate_ulid() == "0" * 26


def test_ulids_sort_by_creation_millisecond() -> None:
    instant = datetime(2026, 9, 5, tzinfo=UTC)
    earlier = IdGenerator(FrozenClock(instant), lambda size: b"\xff" * size).generate_ulid()
    later = IdGenerator(FrozenClock(instant + timedelta(milliseconds=1)), bytes).generate_ulid()
    assert earlier < later


def test_parallel_ids_do_not_depend_on_a_shared_counter() -> None:
    generator = IdGenerator(FrozenClock(datetime(2026, 9, 5, tzinfo=UTC)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: generator.task_id(), range(1000)))
    assert len(set(results)) == 1000


def test_pre_epoch_clock_is_rejected() -> None:
    generator = IdGenerator(FrozenClock(datetime(1969, 12, 31, tzinfo=UTC)))
    with pytest.raises(ValueError, match="48-bit"):
        generator.generate_ulid()


@pytest.mark.parametrize("size", [0, 9, 11])
def test_wrong_entropy_length_is_rejected(size: int) -> None:
    generator = IdGenerator(entropy=lambda _: bytes(size))
    with pytest.raises(ValueError, match="exactly 10 bytes"):
        generator.generate_ulid()


def test_validate_id_preserves_an_existing_identifier() -> None:
    original = IdGenerator().task_id()
    assert validate_id(original, IdKind.TASK) == original


@pytest.mark.parametrize(
    "identifier",
    [
        "TASK_" + "0" * 25,
        "TASK_" + "0" * 27,
        "APP_" + "0" * 26,
        "TASK_" + "I" * 26,
        "TASK_" + "8" + "0" * 25,
        "TASK_" + "0" * 26 + "\n",
        "task_" + "0" * 26,
    ],
)
def test_persisted_id_validation_rejects_noncanonical_values(identifier: str) -> None:
    with pytest.raises(ValueError):
        validate_id(identifier, IdKind.TASK)
