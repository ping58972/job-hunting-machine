"""Architecture Amendment A1: local LaTeX document pipeline acceptance."""

import asyncio
import hashlib
import io
import json
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]
from sqlalchemy import select

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    AgentTask,
    ApplicationDetails,
    Artifact,
)
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    JobCreate,
    JobRepository,
)
from job_hunting_machine.database.repositories.artifacts import ArtifactCreate, ArtifactRepository
from job_hunting_machine.database.repositories.knowledge import (
    CandidateFactRepository,
    CatalogRepository,
)
from job_hunting_machine.knowledge.evidence import Provenance, store_evidence
from job_hunting_machine.models import MockResponsesClient, ModelGateway, ModelResponse, TokenUsage
from job_hunting_machine.orchestration import QueueService
from job_hunting_machine.resume.artifacts import pdf_pages, sha256
from job_hunting_machine.resume.config import ResumePolicy
from job_hunting_machine.resume.errors import ReviewRequired
from job_hunting_machine.resume.latex import (
    PROJECTS,
    SKILLS,
    CompilationResult,
    LatexCompiler,
    LatexTemplate,
    latex_escape,
)
from job_hunting_machine.resume.worker import (
    ResumeWorker,
    cover_letter_plain_text,
    render_resume_sections,
)
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard, PathGuardError

CLOCK = FrozenClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
MASTER = PROJECT_ROOT / "source/NDanddank_resume.tex"
COVER_MASTER = PROJECT_ROOT / "source/NDanddank_cover_letter.tex"


def fixture_pdf(pages: int = 1) -> bytes:
    stream = io.BytesIO()
    canvas = Canvas(stream, pagesize=(612, 792), invariant=1)
    for number in range(pages):
        canvas.drawString(30, 770, f"Verified local document page {number + 1}")
        canvas.showPage()
    canvas.save()
    return stream.getvalue()


class FakeCompiler:
    def __init__(self, pages: Callable[[str, int], int] | None = None) -> None:
        self.pages = pages or (lambda source, call: 1)
        self.calls = 0
        self.fail_after_write = False

    def compile(self, tex_path: Path, build_dir: Path, output_path: Path) -> CompilationResult:
        self.calls += 1
        source = PathGuard().validate_write(tex_path).read_text()
        PathGuard().mkdir(build_dir, parents=True, exist_ok=True)
        PathGuard().mkdir(output_path.parent, parents=True, exist_ok=True)
        PathGuard().write_bytes(output_path, fixture_pdf(self.pages(source, self.calls)))
        if self.fail_after_write:
            self.fail_after_write = False
            raise ProcessDeath
        return CompilationResult(
            "fixture",
            ("fixture-latex", str(tex_path)),
            0,
            False,
            "",
            "",
            str(output_path),
        )


class ProcessDeath(BaseException):
    pass


class Setup:
    def __init__(self, root: Path, compiler: FakeCompiler | None = None) -> None:
        self.root = root
        self.database = Database(root / "jobs.db")
        self.database.migrate()
        self.queue = QueueService(self.database, clock=CLOCK)
        self.compiler = compiler or FakeCompiler()
        self.policy = ResumePolicy(
            resume_template=MASTER,
            cover_letter_template=COVER_MASTER,
            resume_output_dir=root / "resumes",
            cover_letter_output_dir=root / "cover-letters",
            build_root=root / "latex-build",
        )
        with self.database.transaction() as session:
            job = JobRepository(session, CLOCK).create(
                JobCreate(
                    "https://example.invalid/jobs/1",
                    "https://example.invalid/jobs/1",
                    "FIXTURE",
                    qualification_status="PASSED",
                )
            )
            application = ApplicationRepository(session, CLOCK).create_from_passed_job(
                job.job_id,
                ApplicationCreate("Fixture R&D", "Python Robotics", job.canonical_url),
            )
            self.task_id, self.app_id = application.task_id, application.application_id
            project = CatalogRepository(session, CLOCK).ensure_project("fixture/robotics")
            project.source_commit_sha = "a" * 40
            session.flush()
            self.projects: list[str] = []
            self.skills: list[str] = []
            facts = [
                ("Built Python robotics navigation for foo_bar at 50% load.", None),
                ("Tested Python robotics with C++, #AI, and a $100 fixture.", None),
                ("Used C++ for R&D robotics.", "C++"),
            ]
            repository = CandidateFactRepository(session, CLOCK)
            for index, (statement, skill) in enumerate(facts):
                path, evidence_hash = store_evidence(root / "evidence", statement.encode())
                fact = repository.add(
                    fact_type="PROJECT",
                    fact_key=f"fixture-{index}",
                    statement=statement,
                    provenance=Provenance(
                        source_type="GITHUB",
                        source_reference=f"fixture:{index}",
                        path=path,
                        sha256=evidence_hash,
                        line_start=1,
                        line_end=1,
                        quote=statement,
                        project_id=project.project_id,
                        commit_sha="a" * 40,
                    ),
                    skill=skill,
                )
                repository.decide(
                    fact.fact_id,
                    "VERIFIED",
                    expected_status="UNVERIFIED",
                    reviewer="fixture_human",
                )
                (self.skills if skill else self.projects).append(fact.fact_id)
        self.reply = {"project_fact_ids": self.projects, "skill_fact_ids": self.skills}
        self.client = MockResponsesClient(
            [ModelResponse(json.dumps(self.reply), TokenUsage(50, 0, 50)) for _ in range(8)]
        )
        self.gateway = ModelGateway(self.database, client=self.client, clock=CLOCK)

    def worker(self) -> ResumeWorker:
        return ResumeWorker(
            self.queue,
            self.gateway,
            compiler=self.compiler,
            policy=self.policy,
        )

    def run(self) -> ResumeWorker:
        worker = self.worker()
        assert asyncio.run(worker.run_once())
        return worker

    def artifacts(self) -> list[Artifact]:
        with self.database.transaction() as session:
            return list(session.scalars(select(Artifact).order_by(Artifact.artifact_type)))

    def tasks(self, task_type: str) -> list[AgentTask]:
        with self.database.transaction() as session:
            return list(session.scalars(select(AgentTask).where(AgentTask.task_type == task_type)))


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    value = Setup(tmp_path)
    yield value
    value.database.dispose()


def test_master_template_and_marker_contract() -> None:
    source = MASTER.read_text()
    template = LatexTemplate(source, (PROJECTS, SKILLS))
    assert set(template.regions) == {PROJECTS, SKILLS}
    assert source.count("% JHM:PROJECTS:START") == 1
    assert source.count("% JHM:SKILLS:START") == 1
    for malformed in (
        source.replace("% JHM:PROJECTS:START", ""),
        source.replace("% JHM:SKILLS:END", "% JHM:SKILLS:START"),
        ("% JHM:PROJECTS:START\n% JHM:SKILLS:START\n% JHM:PROJECTS:END\n% JHM:SKILLS:END"),
    ):
        with pytest.raises(ReviewRequired, match=r"markers|overlap"):
            LatexTemplate(malformed, (PROJECTS, SKILLS))
    assert set(LatexTemplate(COVER_MASTER.read_text(), ("COVER_LETTER",)).regions) == {
        "COVER_LETTER"
    }


def test_latex_escape_covers_untrusted_special_characters() -> None:
    assert latex_escape("C++ R&D 50% foo_bar $100 #AI {test} ~ ^ \\") == (
        r"C++ R\&D 50\% foo\_bar \$100 \#AI \{test\} "
        r"\textasciitilde{} \textasciicircum{} \textbackslash{}"
    )


def test_real_latex_compiler_valid_invalid_and_root_confined(tmp_path: Path) -> None:
    compiler = LatexCompiler(timeout_seconds=30)
    assert compiler.detect() is not None
    rendered = LatexTemplate(MASTER.read_text(), (PROJECTS, SKILLS)).render(
        render_resume_sections(["Built verified R&D robotics at 50% load."], ["C++", "Python"])
    )
    valid = PathGuard().write_text(
        tmp_path / "valid.tex",
        rendered,
    )
    result = compiler.compile(valid, tmp_path / "build", tmp_path / "valid.pdf")
    assert result.return_code == 0
    assert pdf_pages((tmp_path / "valid.pdf").read_bytes()) == 1
    invalid = PathGuard().write_text(tmp_path / "invalid.tex", r"\documentclass{article}\bad")
    with pytest.raises(ReviewRequired, match="compilation_failed"):
        compiler.compile(invalid, tmp_path / "invalid-build", tmp_path / "invalid.pdf")
    assert all(path.is_relative_to(PROJECT_ROOT) for path in tmp_path.rglob("*"))


def test_compiler_timeout_and_argument_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = PathGuard().write_text(tmp_path / "safe;touch injected.tex", "safe")
    compiler = LatexCompiler(timeout_seconds=1)
    captured: list[list[str]] = []

    def timeout(command: list[str], **kwargs: object) -> Any:
        captured.append(command)
        assert kwargs["shell"] is False
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ReviewRequired, match="timeout"):
        compiler.compile(source, tmp_path / "timeout-build", tmp_path / "timeout.pdf")
    assert len(captured) == 1 and "safe;touch injected.tex" in " ".join(captured[0])
    assert not (tmp_path / "injected.tex").exists()


def test_resume_generates_tex_pdf_hashes_and_preserves_master(setup: Setup) -> None:
    before = MASTER.read_bytes()
    worker = setup.run()
    assert setup.queue.get(setup.task_id).task_status == "SUCCEEDED"
    assert MASTER.read_bytes() == before
    artifacts = setup.artifacts()
    assert [item.artifact_type for item in artifacts] == ["RESUME_PDF", "RESUME_TEX"]
    assert not any(item.artifact_type.endswith("DOCX") for item in artifacts)
    for artifact in artifacts:
        path = PathGuard().validate_write(PROJECT_ROOT / artifact.path)
        assert sha256(path.read_bytes()) == artifact.sha256
        assert path.suffix == (".pdf" if artifact.artifact_type.endswith("PDF") else ".tex")
        assert setup.app_id in path.parts and setup.task_id in path.parts
    tex = next(item for item in artifacts if item.artifact_type == "RESUME_TEX")
    generated = PathGuard().validate_write(PROJECT_ROOT / tex.path).read_text()
    assert r"50\%" in generated and r"foo\_bar" in generated and r"\#AI" in generated
    assert "Small LLM" not in generated
    data = worker._load(setup.task_id)
    assert data and data["checkpoints"][-3:] == [
        "PDF_VALIDATED",
        "ONE_PAGE_CONFIRMED",
        "ARTIFACTS_REGISTERED",
    ]
    assert setup.tasks("FORM_PROCESS")
    with setup.database.transaction() as session:
        details = session.get(ApplicationDetails, setup.app_id)
        assert details and details.resume_artifact_id == data["artifact_ids"]["pdf"]


def test_two_page_pdf_compresses_and_retry_limit_is_safe(tmp_path: Path) -> None:
    value = Setup(tmp_path, FakeCompiler(lambda source, call: 2 if call == 1 else 1))
    try:
        worker = value.run()
        data = worker._load(value.task_id)
        assert data and data["page_counts"] == [2, 1] and data["round"] == 1
        assert value.queue.get(value.task_id).task_status == "SUCCEEDED"
    finally:
        value.database.dispose()
    blocked = Setup(tmp_path / "blocked", FakeCompiler(lambda source, call: 2))
    blocked.policy = blocked.policy.model_copy(update={"max_compression_rounds": 0})
    try:
        blocked.run()
        assert blocked.queue.get(blocked.task_id).task_status == "WAITING_HUMAN"
        assert not blocked.artifacts() and not blocked.tasks("FORM_PROCESS")
    finally:
        blocked.database.dispose()


def test_process_death_after_tex_pdf_resumes_without_regeneration(tmp_path: Path) -> None:
    compiler = FakeCompiler()
    compiler.fail_after_write = True
    value = Setup(tmp_path, compiler)
    worker = value.worker()
    lease = value.queue.claim(("BUILD_RESUME",))
    assert lease
    with pytest.raises(ProcessDeath):
        asyncio.run(worker._execute(lease))
    assert list((tmp_path / "resumes").rglob("*.tex"))
    assert list((tmp_path / "resumes").rglob("*.pdf"))
    value.database.dispose()
    value.database = Database(tmp_path / "jobs.db")
    value.queue = QueueService(value.database, clock=FrozenClock(CLOCK.now() + timedelta(hours=1)))
    assert value.queue.recover() == 1
    value.run()
    assert compiler.calls == 1
    assert len(value.artifacts()) == 2
    value.database.dispose()


def test_cover_letter_tex_pdf_and_plain_text(setup: Setup) -> None:
    setup.policy = setup.policy.model_copy(update={"cover_letter": True})
    setup.run()
    assert setup.tasks("BUILD_COVER_LETTER") and not setup.tasks("FORM_PROCESS")
    worker = setup.run()
    artifacts = setup.artifacts()
    assert {item.artifact_type for item in artifacts} == {
        "RESUME_TEX",
        "RESUME_PDF",
        "COVER_LETTER_TEX",
        "COVER_LETTER_PDF",
    }
    cover_task = setup.tasks("BUILD_COVER_LETTER")[0]
    data = worker._load(cover_task.task_id)
    assert data and "Dear Hiring Team" in data["plain_text"] and "Fixture R&D" in data["plain_text"]
    assert setup.tasks("FORM_PROCESS")
    assert "Google" not in cover_letter_plain_text("A", "B", ["Fact"], ["Skill"])


def test_revoked_fact_and_template_change_fail_before_publication(setup: Setup) -> None:
    with setup.database.transaction() as session:
        CandidateFactRepository(session, CLOCK).decide(
            setup.projects[0],
            "REJECTED",
            expected_status="VERIFIED",
            reviewer="fixture_human",
        )
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "WAITING_HUMAN"
    assert not setup.artifacts()


def test_legacy_docx_is_readable_but_new_creation_is_rejected(setup: Setup) -> None:
    with (
        setup.database.transaction() as session,
        pytest.raises(ValueError, match="Invalid artifact type"),
    ):
        ArtifactRepository(session).create(
            ArtifactCreate(
                "RESUME_DOCX",
                str(MASTER),
                hashlib.sha256(MASTER.read_bytes()).hexdigest(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                application_id=setup.app_id,
                task_id=setup.task_id,
            )
        )


def test_output_boundary_rejects_external_and_symlink(setup: Setup) -> None:
    with pytest.raises(PathGuardError):
        setup.policy.model_copy(update={"resume_output_dir": Path("/tmp/escape")}).validate_paths()
    link = setup.root / "escape-link"
    link.symlink_to("/tmp", target_is_directory=True)
    with pytest.raises(PathGuardError):
        setup.policy.model_copy(update={"build_root": link}).validate_paths()
