"""Versioned stable instructions; context is separate and only hashes enter usage rows."""

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from job_hunting_machine.config import ConfigurationError
from job_hunting_machine.models.router import Settings
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


class Prompt(Settings):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field(
        pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[a-zA-Z0-9.-]+)?$", max_length=64
    )
    instructions: str = Field(min_length=1)

    @property
    def sha256(self) -> str:
        return digest(self.model_dump())

    def cache_key(self, schema: object, namespace: str = "jhm") -> str:
        return digest({"prompt": self.sha256, "schema": schema, "namespace": namespace})


class PromptRegistry(Settings):
    version: Literal[1]
    prompts: tuple[Prompt, ...]

    @model_validator(mode="after")
    def unique(self) -> "PromptRegistry":
        identities = {(p.name, p.version) for p in self.prompts}
        if len(identities) != len(self.prompts):
            raise ValueError("Duplicate prompt name/version")
        return self

    def get(self, name: str, version: str) -> Prompt:
        for prompt in self.prompts:
            if (prompt.name, prompt.version) == (name, version):
                return prompt
        raise ValueError("Prompt version is not registered")


def load_prompts(path: Path = PROJECT_ROOT / "config/prompts.yaml") -> PromptRegistry:
    try:
        value = yaml.safe_load(PathGuard().validate_write(path).read_text(encoding="utf-8"))
        return PromptRegistry.model_validate(value)
    except (ValueError, OSError, yaml.YAMLError) as error:
        raise ConfigurationError("Invalid root-local prompt registry") from error
