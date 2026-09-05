"""Explicit, validated configuration with no filesystem writes or implicit discovery."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, ValidationError

from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ConfigurationError(ValueError):
    """Configuration failed validation; diagnostics never include input values."""


class RuntimeSettings(BaseModel):
    """Immutable validated intent; constructing settings does not start anything."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    runtime_mode: RuntimeMode = RuntimeMode.DRY_RUN
    log_level: LogLevel = "INFO"

    @property
    def project_root(self) -> Path:
        """The architecture boundary cannot be changed by environment or YAML."""
        return PROJECT_ROOT


_ENV_FIELDS = {"JHM_RUNTIME_MODE": "runtime_mode", "JHM_LOG_LEVEL": "log_level"}


def _yaml_values(path: Path, *, allowed: str) -> dict[str, object]:
    try:
        checked = PathGuard().validate_write(path)
        parsed: object = yaml.safe_load(checked.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        raise ConfigurationError("Cannot read configuration YAML inside project root.") from exc
    if not isinstance(parsed, dict) or set(parsed) != {allowed}:
        raise ConfigurationError(f"Configuration YAML must contain only the {allowed} field.")
    return {allowed: parsed[allowed]}


def _environment_values(values: Mapping[str, str | None]) -> dict[str, object]:
    if any(key.startswith("JHM_") and key not in _ENV_FIELDS for key in values):
        raise ConfigurationError("Unknown JHM_ configuration key; check the documented variables.")
    return {_ENV_FIELDS[key]: value for key, value in values.items() if key in _ENV_FIELDS}


def load_settings(
    *,
    runtime_file: Path | None = PROJECT_ROOT / "config/runtime.yaml",
    logging_file: Path | None = PROJECT_ROOT / "config/logging.yaml",
    env_file: Path | None = PROJECT_ROOT / ".env",
    environ: Mapping[str, str] | None = None,
) -> RuntimeSettings:
    """Load defaults < YAML < explicit root-local .env < process environment.

    Passing None disables a file source. YAML files must exist when selected;
    .env is optional. Relative paths are interpreted against PROJECT_ROOT,
    never the current directory. Dotenv interpolation is deliberately disabled.
    """
    values: dict[str, object] = {}
    if runtime_file is not None:
        values.update(_yaml_values(runtime_file, allowed="runtime_mode"))
    if logging_file is not None:
        values.update(_yaml_values(logging_file, allowed="log_level"))
    if env_file is not None:
        try:
            checked_env = PathGuard().validate_write(env_file)
            if checked_env.exists():
                values.update(
                    _environment_values(
                        dotenv_values(checked_env, encoding="utf-8", interpolate=False)
                    )
                )
        except (OSError, ValueError, RuntimeError) as exc:
            raise ConfigurationError("Cannot load valid root-local .env configuration.") from exc
    values.update(_environment_values(os.environ if environ is None else environ))
    try:
        return RuntimeSettings.model_validate(values)
    except ValidationError as exc:
        raise ConfigurationError(
            "Invalid runtime mode or log level; use the exact values in .env.example and README."
        ) from exc
