import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from io import StringIO

import pytest

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.observability.logging import LOGGER_NAME, configure_logging


@pytest.fixture(autouse=True)
def restore_package_logger() -> Iterator[None]:
    logger = logging.getLogger(LOGGER_NAME)
    old_handlers = logger.handlers[:]
    old_level, old_propagate, old_disabled = logger.level, logger.propagate, logger.disabled
    for handler in old_handlers:
        logger.removeHandler(handler)
    yield
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    for handler in old_handlers:
        logger.addHandler(handler)
    logger.setLevel(old_level)
    logger.propagate, logger.disabled = old_propagate, old_disabled


def test_structured_log_contains_architecture_fields() -> None:
    stream = StringIO()
    clock = FrozenClock(datetime(2026, 9, 5, 8, 23, 31, 123456, tzinfo=UTC))
    logger = configure_logging(stream=stream, clock=clock)
    logger.info(
        "foundation_ready",
        extra={
            "task_id": "TASK_01K4Y9DKQF0S1NHV0A84NJBYPM",
            "application_id": "APP_01K4Y9DM28A06YF81V66VQPDHJ",
            "agent": "foundation",
            "status": "SUCCEEDED",
            "duration": 0.125,
        },
    )
    assert json.loads(stream.getvalue()) == {
        "timestamp": "2026-09-05T08:23:31.123Z",
        "level": "INFO",
        "task_id": "TASK_01K4Y9DKQF0S1NHV0A84NJBYPM",
        "application_id": "APP_01K4Y9DM28A06YF81V66VQPDHJ",
        "agent": "foundation",
        "event": "foundation_ready",
        "status": "SUCCEEDED",
        "duration": 0.125,
        "error_class": None,
    }


def test_exception_text_and_unknown_extra_are_never_serialized() -> None:
    stream = StringIO()
    logger = configure_logging(stream=stream)
    try:
        raise ValueError("private-exception-message")
    except ValueError:
        logger.exception(
            "operation_failed",
            extra={"password": "private-password", "email": "private@example.com"},
            stack_info=True,
        )
    output = stream.getvalue()
    payload = json.loads(output)
    assert payload["error_class"] == "ValueError"
    assert payload["event"] == "operation_failed"
    assert "private" not in output
    assert "Traceback" not in output
    assert "Stack" not in output


def test_freeform_and_interpolated_messages_are_not_serialized() -> None:
    stream = StringIO()
    logger = configure_logging(stream=stream)
    logger.info("password is private-password")
    logger.info("private_password_%s", "private-value")
    logger.info("private %s", "private-value", extra={"event": "safe_event"})
    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [item["event"] for item in payloads] == [
        "unstructured_event",
        "unstructured_event",
        "safe_event",
    ]
    assert "private" not in stream.getvalue()


@pytest.mark.parametrize("duration", [True, -1, float("nan"), float("inf"), 10**400, "2"])
def test_invalid_context_is_dropped(duration: object) -> None:
    stream = StringIO()
    logger = configure_logging(stream=stream)
    logger.info(
        "safe_event",
        extra={
            "task_id": "candidate@example.com",
            "application_id": "invalid",
            "agent": "contains private data",
            "status": {"password": "private-password"},
            "duration": duration,
        },
    )
    payload = json.loads(stream.getvalue())
    for field in ("task_id", "application_id", "agent", "status", "duration"):
        assert payload[field] is None
    assert "private" not in stream.getvalue()


def test_reconfiguration_is_repeatable_and_leaves_root_logger_unchanged() -> None:
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    old_stream, new_stream = StringIO(), StringIO()
    configure_logging(stream=old_stream)
    logger = configure_logging(level="warning", stream=new_stream)
    logger.info("hidden_event")
    logging.getLogger(f"{LOGGER_NAME}.child").warning("visible_event")
    assert old_stream.getvalue() == ""
    assert len(new_stream.getvalue().splitlines()) == 1
    assert json.loads(new_stream.getvalue())["event"] == "visible_event"
    assert root.handlers == previous_handlers
    assert root.level == previous_level


def test_invalid_logging_level_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported logging level"):
        configure_logging(level="LIVE")
