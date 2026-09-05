"""JSON console logging with deliberately narrow, non-sensitive event fields.

Use static developer-defined event/agent/status tokens, never candidate data or
secrets. Free-form messages, message arguments, exception text, stack traces and
unrecognized ``extra`` fields are not serialized. This is not a general secret
detector: callers must not put secrets into the allowed event-token fields.
"""

import json
import logging
import math
import re
from threading import Lock
from typing import TextIO

from job_hunting_machine.clock import Clock, SystemClock, format_utc
from job_hunting_machine.ids import APPLICATION_ID_REGEX, TASK_ID_REGEX

_TOKEN_REGEX = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
_CONFIG_LOCK = Lock()
_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
LOGGER_NAME = "job_hunting_machine"


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN_REGEX.fullmatch(value) else None


def _identifier(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


class JsonFormatter(logging.Formatter):
    """Emit a stable schema, with null for missing or invalid context fields."""

    def __init__(self, clock: Clock | None = None) -> None:
        super().__init__()
        self._clock = clock if clock is not None else SystemClock()

    def format(self, record: logging.LogRecord) -> str:
        event = _token(getattr(record, "event", None))
        if event is None and not record.args:
            event = _token(record.msg)
        error_class = _token(getattr(record, "error_class", None))
        if record.exc_info is not None and record.exc_info[0] is not None:
            error_class = _token(record.exc_info[0].__name__)
        payload = {
            "timestamp": format_utc(self._clock.now()),
            "level": record.levelname,
            "task_id": _identifier(getattr(record, "task_id", None), TASK_ID_REGEX),
            "application_id": _identifier(
                getattr(record, "application_id", None), APPLICATION_ID_REGEX
            ),
            "agent": _token(getattr(record, "agent", None)),
            "event": event or "unstructured_event",
            "status": _token(getattr(record, "status", None)),
            "duration": _duration(getattr(record, "duration", None)),
            "error_class": error_class,
        }
        return json.dumps(payload, allow_nan=False, ensure_ascii=True, separators=(",", ":"))


def configure_logging(
    level: str = "INFO",
    stream: TextIO | None = None,
    clock: Clock | None = None,
) -> logging.Logger:
    """Replace only package handlers with one JSON stream handler (stderr default).

    This function owns the package logger's configuration. It does not modify the
    root logger or third-party loggers. Repeated calls do not duplicate output.
    ``duration`` is expressed in seconds. No file sink is opened here.
    """
    normalized_level = level.upper()
    if normalized_level not in _LEVELS:
        raise ValueError("Unsupported logging level")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(clock))
    logger = logging.getLogger(LOGGER_NAME)
    with _CONFIG_LOCK:
        for previous in logger.handlers[:]:
            logger.removeHandler(previous)
            previous.close()
        logger.addHandler(handler)
        logger.setLevel(normalized_level)
        logger.propagate = False
        logger.disabled = False
    return logger
