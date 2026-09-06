"""Validated immutable source locators and local evidence integrity."""

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from job_hunting_machine.security.paths import PathGuard


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_type: str = Field(min_length=1, max_length=64)
    source_reference: str = Field(min_length=1, max_length=2048)
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=4000)
    project_id: str | None = None
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    repository_path: str | None = None

    def validate_content(self) -> None:
        path = PathGuard().validate_write(self.path)
        if path.stat().st_size > 2_000_000:
            raise ValueError("evidence_too_large")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("evidence_hash_mismatch")
        lines = raw.decode("utf-8").splitlines()
        if self.line_end < self.line_start or self.line_end > len(lines):
            raise ValueError("invalid_evidence_lines")
        if self.quote not in "\n".join(lines[self.line_start - 1 : self.line_end]):
            raise ValueError("evidence_quote_mismatch")


def store_evidence(directory: Path, content: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    guard = PathGuard()
    guard.mkdir(directory, parents=True, exist_ok=True)
    path = directory / f"{digest}.txt"
    guard.write_bytes(path, content)
    return str(path), digest
