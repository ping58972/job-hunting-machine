"""Root-confined LaTeX templates, escaping, compilation, and validation."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Protocol

from job_hunting_machine.resume.errors import ReviewRequired
from job_hunting_machine.security.paths import PathGuard

PROJECTS = "PROJECTS"
SKILLS = "SKILLS"
COVER_LETTER = "COVER_LETTER"


def latex_escape(value: str) -> str:
    """Escape plain text once, without touching template-authored LaTeX."""
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ReviewRequired("latex_plain_text_control_character")
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "#": r"\#",
        "$": r"\$",
        "%": r"\%",
        "&": r"\&",
        "_": r"\_",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in value)


@dataclass(frozen=True, slots=True)
class MarkerRegion:
    name: str
    start: int
    content_start: int
    content_end: int
    end: int


class LatexTemplate:
    """Validate and replace explicit, non-overlapping machine-owned regions."""

    def __init__(self, source: str, required: tuple[str, ...]) -> None:
        self.source = source
        self.required = required
        self.regions = self._regions()

    def _regions(self) -> dict[str, MarkerRegion]:
        regions: dict[str, MarkerRegion] = {}
        for name in self.required:
            start_marker = f"% JHM:{name}:START"
            end_marker = f"% JHM:{name}:END"
            if self.source.count(start_marker) != 1 or self.source.count(end_marker) != 1:
                raise ReviewRequired(f"latex_template_{name.casefold()}_markers_invalid")
            start = self.source.index(start_marker)
            content_start = start + len(start_marker)
            end = self.source.index(end_marker)
            if end <= content_start:
                raise ReviewRequired(f"latex_template_{name.casefold()}_markers_misordered")
            regions[name] = MarkerRegion(name, start, content_start, end, end + len(end_marker))
        ordered = sorted(regions.values(), key=lambda item: item.start)
        if any(left.end > right.start for left, right in pairwise(ordered)):
            raise ReviewRequired("latex_template_regions_overlap")
        return regions

    def render(self, replacements: dict[str, str]) -> str:
        if set(replacements) != set(self.required):
            raise ReviewRequired("latex_template_replacement_scope_invalid")
        rendered = self.source
        for region in sorted(self.regions.values(), key=lambda item: item.start, reverse=True):
            content = "\n" + replacements[region.name].strip("\n") + "\n"
            rendered = rendered[: region.content_start] + content + rendered[region.content_end :]
        return rendered


@dataclass(frozen=True, slots=True)
class CompilationResult:
    backend: str
    command: tuple[str, ...]
    return_code: int
    timed_out: bool
    stdout: str
    stderr: str
    pdf_path: str | None


class Compiler(Protocol):
    def compile(self, tex_path: Path, build_dir: Path, output_path: Path) -> CompilationResult: ...


class LatexCompiler:
    """Central safe process boundary for supported local LaTeX compilers."""

    SUPPORTED = ("latexmk", "pdflatex", "tectonic")

    def __init__(self, *, preferred: str = "latexmk", timeout_seconds: int = 60) -> None:
        if preferred not in self.SUPPORTED:
            raise ValueError("unsupported_latex_compiler")
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("invalid_latex_timeout")
        self.preferred = preferred
        self.timeout_seconds = timeout_seconds

    def detect(self) -> tuple[str, str] | None:
        order = (self.preferred, *(item for item in self.SUPPORTED if item != self.preferred))
        for backend in order:
            executable = shutil.which(backend)
            if executable:
                return backend, executable
        return None

    @staticmethod
    def _command(backend: str, executable: str, source: Path, build: Path) -> list[str]:
        common = ["-interaction=nonstopmode", "-halt-on-error", "-file-line-error"]
        if backend == "latexmk":
            return [executable, "-pdf", *common, f"-outdir={build}", str(source)]
        if backend == "pdflatex":
            return [executable, *common, f"-output-directory={build}", str(source)]
        return [executable, "--keep-logs", "--outdir", str(build), str(source)]

    def compile(self, tex_path: Path, build_dir: Path, output_path: Path) -> CompilationResult:
        guard = PathGuard()
        source = guard.validate_write(tex_path)
        build = guard.mkdir(build_dir, parents=True, exist_ok=True)
        destination = guard.validate_write(output_path)
        guard.mkdir(destination.parent, parents=True, exist_ok=True)
        if not source.is_file():
            raise ReviewRequired("latex_source_missing")
        detected = self.detect()
        if detected is None:
            raise ReviewRequired("no_supported_latex_compiler")
        backend, executable = detected
        staged = guard.write_bytes(build / source.name, source.read_bytes())
        command = self._command(backend, executable, staged, build)
        try:
            completed = subprocess.run(
                command,
                cwd=build,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
            )
            result = CompilationResult(
                backend,
                tuple(command),
                completed.returncode,
                False,
                completed.stdout[-20_000:],
                completed.stderr[-20_000:],
                None,
            )
        except subprocess.TimeoutExpired as error:
            result = CompilationResult(
                backend,
                tuple(command),
                -1,
                True,
                str(error.stdout or "")[-20_000:],
                str(error.stderr or "")[-20_000:],
                None,
            )
        built_pdf = guard.validate_write(build / f"{staged.stem}.pdf")
        if result.return_code == 0 and not result.timed_out and built_pdf.is_file():
            guard.write_bytes(destination, built_pdf.read_bytes())
            result = CompilationResult(
                result.backend,
                result.command,
                result.return_code,
                result.timed_out,
                result.stdout,
                result.stderr,
                str(destination),
            )
        metadata = {
            **asdict(result),
            "command": [Path(result.command[0]).name, *result.command[1:]],
        }
        guard.write_text(build / "compilation.json", json.dumps(metadata, sort_keys=True, indent=2))
        if result.timed_out:
            raise ReviewRequired("latex_compilation_timeout")
        if result.return_code != 0 or result.pdf_path is None:
            raise ReviewRequired("latex_compilation_failed")
        return result
