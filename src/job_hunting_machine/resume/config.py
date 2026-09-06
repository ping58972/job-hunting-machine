"""Configuration for root-local LaTeX application documents."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


class ResumePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    resume_template: Path = PROJECT_ROOT / "source/NDanddank_resume.tex"
    cover_letter_template: Path = PROJECT_ROOT / "source/NDanddank_cover_letter.tex"
    resume_output_dir: Path = PROJECT_ROOT / "resumes"
    cover_letter_output_dir: Path = PROJECT_ROOT / "cover-letters"
    build_root: Path = PROJECT_ROOT / "data/latex-build"
    preferred_compiler: str = "latexmk"
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    required_pages: int = Field(default=1, ge=1, le=1)
    max_compression_rounds: int = Field(default=8, ge=0, le=30)
    sol_finalization: bool = True
    cover_letter: bool = False

    def validate_paths(self) -> None:
        guard = PathGuard()
        for template, error in (
            (self.resume_template, "source_resume_template_missing"),
            (self.cover_letter_template, "source_cover_letter_template_missing"),
        ):
            if not guard.validate_write(template).is_file():
                raise ValueError(error)
        for path in (self.resume_output_dir, self.cover_letter_output_dir, self.build_root):
            guard.validate_write(path)


def load_policy(path: Path = PROJECT_ROOT / "config/resume.yaml") -> ResumePolicy:
    value = yaml.safe_load(PathGuard().validate_write(path).read_text())
    for key in (
        "resume_template",
        "cover_letter_template",
        "resume_output_dir",
        "cover_letter_output_dir",
        "build_root",
    ):
        if key in value and not Path(value[key]).is_absolute():
            value[key] = PROJECT_ROOT / value[key]
    policy = ResumePolicy.model_validate(value)
    policy.validate_paths()
    return policy
