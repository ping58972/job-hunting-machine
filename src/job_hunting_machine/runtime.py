"""Runtime intent; interpreting LIVE is reserved for an explicitly gated runner."""

from enum import StrEnum


class RuntimeMode(StrEnum):
    """The exact modes defined in Architecture v2 sections 2 and 67."""

    DRY_RUN = "DRY_RUN"
    STAGING = "STAGING"
    LIVE = "LIVE"
