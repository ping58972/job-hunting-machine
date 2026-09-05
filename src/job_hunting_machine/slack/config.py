"""Deny-by-default Slack scope; secrets are process environment only."""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_hunting_machine.config import ConfigurationError
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

SlackID = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{1,63}$")]


class SlackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    team_id: SlackID | None = None
    app_id: SlackID | None = None
    authorized_user_ids: frozenset[SlackID] = frozenset()
    allowed_channel_ids: frozenset[SlackID] = frozenset()
    notification_channel_id: SlackID | None = None
    approval_ttl_seconds: int = Field(default=86400, gt=0, le=86400)

    @model_validator(mode="after")
    def channel(self) -> "SlackSettings":
        if (
            self.notification_channel_id
            and self.notification_channel_id not in self.allowed_channel_ids
        ):
            raise ValueError("Notification channel must be allowlisted")
        return self

    def authorize(self, *, team: str, app: str, channel: str, user: str) -> None:
        if (
            team != self.team_id
            or app != self.app_id
            or channel not in self.allowed_channel_ids
            or user not in self.authorized_user_ids
        ):
            raise SlackInputError("unauthorized_slack_source")


class SlackInputError(ValueError):
    """Static, safe rejection token; never include raw payload values."""


def load_slack_settings(path: Path = PROJECT_ROOT / "config/slack.yaml") -> SlackSettings:
    try:
        return SlackSettings.model_validate(
            yaml.safe_load(PathGuard().validate_write(path).read_text())
        )
    except (ValueError, OSError, yaml.YAMLError) as error:
        raise ConfigurationError("Invalid root-local Slack configuration") from error
