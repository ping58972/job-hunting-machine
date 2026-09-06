"""Bounded Phase 7 policy; enabling cloud access is a separate explicit decision."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


class ResumePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    template: Path = PROJECT_ROOT / "source/NDanddank_resume.gdoc"
    output_root: Path = PROJECT_ROOT
    max_compression_rounds: int = Field(default=8, ge=0, le=30)
    sol_finalization: bool = True
    cover_letter: bool = False
    template_fact_id: str | None = None

    def validate_paths(self) -> None:
        guard = PathGuard()
        template = guard.validate_write(self.template)
        guard.validate_write(self.output_root)
        if not template.is_file():
            raise ValueError("source_resume_template_missing")


def load_policy(path: Path = PROJECT_ROOT / "config/resume.yaml") -> ResumePolicy:
    value = yaml.safe_load(PathGuard().validate_write(path).read_text())
    policy = ResumePolicy.model_validate(value)
    policy.validate_paths()
    return policy
