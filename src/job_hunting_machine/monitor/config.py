"""Validated Phase 11 scheduling, confidence, and rate-limit policy."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from job_hunting_machine.config import ConfigurationError
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


class MonitorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    max_gmail_messages_per_run: int = Field(default=25, ge=1, le=100)
    max_portal_checks_per_run: int = Field(default=10, ge=1, le=50)
    transition_confidence: float = Field(default=0.90, ge=0, le=1)
    destructive_confidence: float = Field(default=0.95, ge=0, le=1)
    submitted_checks_per_day: int = Field(default=1, ge=0, le=4)
    under_review_checks_per_day: int = Field(default=1, ge=0, le=4)
    interview_checks_per_day: int = Field(default=2, ge=0, le=8)
    retry_delays_seconds: tuple[float, ...] = (30.0, 120.0, 600.0, 1800.0)

    def checks_per_day(self, status: str) -> int:
        if status == "SUBMITTED":
            return self.submitted_checks_per_day
        if status == "UNDER_REVIEW":
            return self.under_review_checks_per_day
        if status in {
            "ASSESSMENT",
            "RECRUITER_SCREEN",
            "INTERVIEW",
            "FINAL_INTERVIEW",
            "OFFER",
        }:
            return self.interview_checks_per_day
        return 0


def load_monitor_settings(
    path: Path = PROJECT_ROOT / "config/monitor.yaml",
) -> MonitorSettings:
    try:
        return MonitorSettings.model_validate(
            yaml.safe_load(PathGuard().validate_write(path).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise ConfigurationError("Invalid root-local monitor configuration") from error
