"""Explicit Phase 8 browser gates."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_hunting_machine.config import ConfigurationError
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_mode: RuntimeMode = RuntimeMode.DRY_RUN
    allow_live: bool = False
    headless: bool = True

    @property
    def mutation_enabled(self) -> bool:
        return self.runtime_mode in {RuntimeMode.STAGING, RuntimeMode.LIVE}


class FormPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    supported_mutation_ats: tuple[str, ...]
    inspect_only_ats: tuple[str, ...]
    max_pages: int = Field(default=10, ge=1, le=50)
    approval_ttl_seconds: int = Field(default=86400, ge=60, le=86400)

    @model_validator(mode="after")
    def ats_partition(self) -> "FormPolicy":
        expected = {
            "GREENHOUSE",
            "LEVER",
            "ASHBY",
            "WORKDAY",
            "SMARTRECRUITERS",
            "ICIMS",
            "GENERIC",
        }
        supported = set(self.supported_mutation_ats)
        inspected = set(self.inspect_only_ats)
        if supported & inspected or supported | inspected != expected:
            raise ValueError("ATS policy must define one complete, non-overlapping partition")
        return self


def load_form_policy(path: Path = PROJECT_ROOT / "config/form.yaml") -> FormPolicy:
    try:
        value = yaml.safe_load(PathGuard().validate_write(path).read_text(encoding="utf-8"))
        return FormPolicy.model_validate(value)
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise ConfigurationError("Invalid root-local form policy configuration") from error


def browser_settings(
    runtime_mode: RuntimeMode,
    *,
    live_flag: bool = False,
    environ: Mapping[str, str] | None = None,
) -> BrowserSettings:
    values = os.environ if environ is None else environ
    allow_live = (
        runtime_mode is RuntimeMode.LIVE
        and live_flag
        and values.get("FORM_BROWSER_ALLOW_LIVE") == "1"
    )
    if runtime_mode is RuntimeMode.LIVE and not allow_live:
        raise ValueError("live_form_browser_requires_all_opt_ins")
    if live_flag and runtime_mode is not RuntimeMode.LIVE:
        raise ValueError("live_flag_requires_live_runtime")
    return BrowserSettings(runtime_mode=runtime_mode, allow_live=allow_live)
