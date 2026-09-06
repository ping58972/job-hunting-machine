"""Phase 7 acceptance: synthetic native Docs, real PDFs, SQLite and ModelGateway mocks."""

import asyncio
import copy
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]
from sqlalchemy import select

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    AgentTask,
    ApplicationDetails,
    ApplicationPipeline,
    Artifact,
    ModelUsage,
)
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    JobCreate,
    JobRepository,
)
from job_hunting_machine.database.repositories.artifacts import ArtifactRepository
from job_hunting_machine.database.repositories.knowledge import (
    CandidateFactRepository,
    CatalogRepository,
)
from job_hunting_machine.knowledge.evidence import Provenance, store_evidence
from job_hunting_machine.models import MockResponsesClient, ModelGateway, ModelResponse, TokenUsage
from job_hunting_machine.orchestration import QueueService
from job_hunting_machine.resume.artifacts import pdf_pages, read_record, sha256
from job_hunting_machine.resume.config import ResumePolicy
from job_hunting_machine.resume.documents import GoogleDocsHTTP
from job_hunting_machine.resume.fake import FakeDocuments
from job_hunting_machine.resume.native import (
    Json,
    edit_requests,
    normalize,
    paragraphs,
    signature,
    slots,
)
from job_hunting_machine.resume.template import propose_template
from job_hunting_machine.resume.worker import ResumeWorker
from job_hunting_machine.security.paths import PathGuard, PathGuardError

CLOCK = FrozenClock(datetime(2026, 9, 5, 12, tzinfo=UTC))


def template() -> Json:
    # The supplied document's unusual topology: resume content in a header table,
    # not in the body. Side cells contain dates that must not leak into new projects.
    rows = []
    index = 1
    for row_number, texts in enumerate(
        [
            ("Synthetic Candidate", "Contact"),
            ("EDUCATION", ""),
            ("Preserved University", "2027"),
            ("WORK EXPERIENCE", ""),
            ("Preserved role", "2024"),
            ("PROJECTS", ""),
            ("Old project title", "Unverified date"),
            ("Old metric 99%", ""),
            ("Old project two", "Unverified date"),
            ("Old claim", ""),
            ("SKILLS", ""),
            ("OldSkill", ""),
        ]
    ):
        cells = []
        for value in texts:
            cells.append(
                {
                    "tableCellStyle": {"columnSpan": 1},
                    "content": [
                        {
                            "startIndex": index,
                            "endIndex": index + len(value) + 1,
                            "paragraph": {
                                "paragraphStyle": {"spaceAbove": {"magnitude": 0, "unit": "PT"}},
                                "elements": [
                                    {
                                        "startIndex": index,
                                        "endIndex": index + len(value) + 1,
                                        "textRun": {
                                            "content": value + "\n",
                                            "textStyle": {
                                                "weightedFontFamily": {
                                                    "fontFamily": "Times New Roman"
                                                },
                                                "fontSize": {"magnitude": 11, "unit": "PT"},
                                                "bold": row_number in (0, 5, 6, 10),
                                            },
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            )
            index += len(value) + 3
        rows.append(
            {"tableCells": cells, "tableRowStyle": {"minRowHeight": {"magnitude": 4, "unit": "PT"}}}
        )
    return normalize(
        {
            "documentId": "template",
            "title": "Synthetic Template",
            "revisionId": "1",
            "documentStyle": {"marginTop": {"magnitude": 22, "unit": "PT"}},
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-one"},
                    "documentTab": {
                        "body": {"content": []},
                        "headers": {
                            "header-one": {
                                "content": [
                                    {
                                        "table": {
                                            "tableRows": rows,
                                            "tableStyle": {
                                                "columnProperties": [{"widthType": "FIXED_WIDTH"}]
                                            },
                                        }
                                    }
                                ]
                            }
                        },
                        "footers": {"footer-one": {"content": []}},
                    },
                }
            ],
        }
    )


def export_pdf(document: Json, *, pages: int = 1, omit: bool = False) -> bytes:
    stream = io.BytesIO()
    canvas = Canvas(stream, pagesize=(612, 792), invariant=1)
    canvas.setFont("Times-Roman", 11)
    y = 770
    for p in paragraphs(document):
        for line in p.text.splitlines():
            if not omit:
                canvas.drawString(30, y, line)
            y -= 14
    canvas.showPage()
    for _ in range(pages - 1):
        canvas.drawString(30, 770, "Synthetic overflow")
        canvas.showPage()
    canvas.save()
    return stream.getvalue()


class Setup:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = Database(root / "jobs.db")
        self.database.migrate()
        self.queue = QueueService(self.database, clock=CLOCK)
        self.adapter = FakeDocuments(root / "fake-docs", export_pdf)
        self.adapter.put(template())
        self.template_path = PathGuard().write_text(root / "template.gdoc", '{"doc_id":"template"}')
        self.policy = ResumePolicy(template=self.template_path, output_root=root)
        template_fact = propose_template(self.database, self.adapter, self.policy)
        with self.database.transaction() as session:
            CandidateFactRepository(session, CLOCK).decide(
                template_fact, "VERIFIED", expected_status="UNVERIFIED", reviewer="fixture_human"
            )
        self.policy = self.policy.model_copy(update={"template_fact_id": template_fact})
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
                ApplicationCreate("Fixture Company", "Python Robotics", job.canonical_url),
            )
            self.task_id, self.app_id = application.task_id, application.application_id
            project = CatalogRepository(session, CLOCK).ensure_project("fixture/robotics")
            project.source_commit_sha = "a" * 40
            session.flush()
            self.projects: list[str] = []
            self.skills: list[str] = []
            for index, (statement, skill) in enumerate(
                [
                    ("Built Python robotics navigation.", None),
                    ("Tested Python robotics in simulation.", None),
                    ("Used Python for robotics.", "Python"),
                ]
            ):
                path, sha = store_evidence(root / "evidence", statement.encode())
                provenance = Provenance(
                    source_type="GITHUB",
                    source_reference=f"fixture:{index}",
                    path=path,
                    sha256=sha,
                    line_start=1,
                    line_end=1,
                    quote=statement,
                    project_id=project.project_id,
                    commit_sha="a" * 40,
                )
                repository = CandidateFactRepository(session, CLOCK)
                fact = repository.add(
                    fact_type="PROJECT",
                    fact_key=f"fixture-{index}",
                    statement=statement,
                    provenance=provenance,
                    skill=skill,
                )
                repository.decide(
                    fact.fact_id, "VERIFIED", expected_status="UNVERIFIED", reviewer="fixture_human"
                )
                (self.skills if skill else self.projects).append(fact.fact_id)
        self.reply = {"project_fact_ids": self.projects, "skill_fact_ids": self.skills}
        self.client = MockResponsesClient(
            [ModelResponse(json.dumps(self.reply), TokenUsage(50, 0, 50)) for _ in range(4)]
        )
        self.gateway = ModelGateway(self.database, client=self.client, clock=CLOCK)

    def worker(self) -> ResumeWorker:
        return ResumeWorker(self.queue, self.adapter, self.gateway, policy=self.policy)

    def run(self) -> ResumeWorker:
        worker = self.worker()
        assert asyncio.run(worker.run_once())
        return worker

    def tasks(self, kind: str) -> list[AgentTask]:
        with self.database.transaction() as session:
            return list(session.scalars(select(AgentTask).where(AgentTask.task_type == kind)))

    def artifacts(self) -> list[Artifact]:
        with self.database.transaction() as session:
            return list(session.scalars(select(Artifact)))


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    value = Setup(tmp_path)
    yield value
    value.database.dispose()


def test_native_resume_acceptance_and_hashes(setup: Setup) -> None:
    worker = setup.run()
    assert setup.queue.get(setup.task_id).task_status == "SUCCEEDED", setup.queue.memory(
        setup.task_id
    )
    data = worker._load(setup.task_id)
    assert data
    doc = setup.adapter.get(data["document_id"])
    assert signature(template(), edited=True) == signature(doc, edited=True)
    texts = " ".join(p.text for p in paragraphs(doc))
    assert "Built Python robotics navigation." in texts and "Python" in texts
    assert "Old project" not in texts and "99%" not in texts and "Unverified date" not in texts
    assert "Preserved University" in texts and "Preserved role" in texts
    assert setup.adapter.get("template") == template()
    assert len(setup.artifacts()) == 3 and len(setup.tasks("FORM_PROCESS")) == 1
    for artifact in setup.artifacts():
        path = PathGuard().validate_write(artifact.path)
        assert sha256(path.read_bytes()) == artifact.sha256
        assert not artifact.approved_for_submission
        if path.suffix in {".pdf", ".gdoc"}:
            assert (
                path.name
                == f"NDanddank_resume_Fixture_Company_Python_Robotics_09052026{path.suffix}"
            )
    assert (
        pdf_pages(read_record(data["artifacts"]["pdf"]), ["Built Python robotics navigation."]) == 1
    )
    with setup.database.transaction() as session:
        app = session.get(ApplicationPipeline, setup.app_id)
        assert app and app.application_status == "FORM_READY"
        usage = list(session.scalars(select(ModelUsage)))
        assert [row.model_id for row in usage] == ["gpt-5.6-terra", "gpt-5.6-sol"]


def test_content_compression_preserves_fonts_and_margins(setup: Setup) -> None:
    def exporter(doc: Json) -> bytes:
        full = "".join(p.text for p in paragraphs(doc))
        return export_pdf(doc, pages=2 if "Tested Python" in full else 1)

    setup.adapter.exporter = exporter
    worker = setup.run()
    data = worker._load(setup.task_id)
    assert data and data["page_counts"] == [2, 1]
    assert data["round"] == 1 and len(setup.artifacts()) == 3
    assert signature(template(), edited=True) == signature(
        setup.adapter.get(data["document_id"]), edited=True
    )


@pytest.mark.parametrize("failure", ["overflow", "clipped", "corrupt"])
def test_invalid_pdf_never_creates_form_task(setup: Setup, failure: str) -> None:
    setup.adapter.exporter = lambda doc: (
        b"broken"
        if failure == "corrupt"
        else export_pdf(doc, pages=2 if failure == "overflow" else 1, omit=failure == "clipped")
    )
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "WAITING_HUMAN"
    assert not setup.tasks("FORM_PROCESS") and not setup.artifacts()
    with setup.database.transaction() as session:
        app = session.get(ApplicationPipeline, setup.app_id)
        assert app and app.application_status == "QUALIFIED"


def test_unverified_or_revoked_facts_cannot_publish(setup: Setup) -> None:
    original_export = setup.adapter.export

    def revoke(document_id: str) -> bytes:
        with setup.database.transaction() as session:
            CandidateFactRepository(session, CLOCK).decide(
                setup.projects[0], "REJECTED", expected_status="VERIFIED", reviewer="fixture_human"
            )
        return original_export(document_id)

    setup.adapter.export = revoke  # type: ignore[method-assign]
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "WAITING_HUMAN"
    assert not setup.artifacts() and not setup.tasks("FORM_PROCESS")


def test_model_cannot_invent_fact_or_finalization_fact(setup: Setup) -> None:
    setup.client = MockResponsesClient(
        [
            ModelResponse(
                json.dumps({**setup.reply, "project_fact_ids": ["invented"]}), TokenUsage(10, 0, 10)
            )
        ]
    )
    setup.gateway = ModelGateway(setup.database, client=setup.client, clock=CLOCK)
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "WAITING_HUMAN"
    assert setup.adapter.copies == 0 and not setup.tasks("FORM_PROCESS")


@pytest.mark.parametrize("crash_point", ["gdoc", "publish", "copy", "edit"])
def test_process_death_and_restart_no_duplicate_artifacts(setup: Setup, crash_point: str) -> None:
    class ProcessDeath(BaseException):
        pass

    worker = setup.worker()
    lease = setup.queue.claim(("BUILD_RESUME",))
    assert lease
    if crash_point == "gdoc":
        setup.adapter.export = lambda document_id: (_ for _ in ()).throw(ProcessDeath())  # type: ignore[method-assign]
    elif crash_point == "publish":
        original_publish = worker._publish

        def die_publish(*args: Any) -> None:
            original_publish(*args)
            raise ProcessDeath()

        worker._publish = die_publish  # type: ignore[method-assign, assignment]
    else:
        original = getattr(setup.adapter, crash_point)

        def die_action(*args: Any) -> Any:
            original(*args)
            raise ProcessDeath()

        setattr(setup.adapter, crash_point, die_action)
    with pytest.raises(ProcessDeath):
        asyncio.run(worker._execute(lease))
    copies_before = list((setup.root / "fake-docs").glob("fake_*.json"))
    assert len(copies_before) == 1
    setup.database.dispose()
    setup.database = Database(setup.root / "jobs.db")
    setup.queue = QueueService(setup.database, clock=FrozenClock(CLOCK.now() + timedelta(hours=1)))
    assert setup.queue.recover() == 1
    setup.adapter = FakeDocuments(setup.root / "fake-docs", export_pdf)
    worker = setup.run()
    assert setup.queue.get(setup.task_id).task_status == "SUCCEEDED", setup.queue.memory(
        setup.task_id
    )
    assert len(setup.artifacts()) == 3 and len(setup.tasks("FORM_PROCESS")) == 1
    assert setup.adapter.copies == 0
    assert len(setup.client.calls) == 2
    assert worker._load(setup.task_id)


def test_cover_letter_policy_holds_form_until_both_validate(setup: Setup) -> None:
    setup.policy = setup.policy.model_copy(update={"cover_letter": True})
    setup.run()
    assert len(setup.tasks("BUILD_COVER_LETTER")) == 1 and not setup.tasks("FORM_PROCESS")
    setup.run()
    cover = setup.tasks("BUILD_COVER_LETTER")[0]
    assert cover.task_status == "SUCCEEDED", setup.queue.memory(cover.task_id)
    assert len(setup.artifacts()) == 6 and len(setup.tasks("FORM_PROCESS")) == 1
    with setup.database.transaction() as session:
        details = session.get(ApplicationDetails, setup.app_id)
        assert details and details.resume_artifact_id and details.cover_letter_artifact_id
    cover_pdf = next(a for a in setup.artifacts() if a.artifact_type == "COVER_LETTER_PDF")
    assert (
        Path(cover_pdf.path).name
        == "NDanddank_CoverLetter_Fixture_Company_Python_Robotics_09052026.pdf"
    )
    assert (
        pdf_pages(
            PathGuard().validate_write(cover_pdf.path).read_bytes(),
            ["Built Python robotics navigation."],
        )
        == 1
    )


def test_sol_finalization_can_be_disabled(setup: Setup) -> None:
    setup.policy = setup.policy.model_copy(update={"sol_finalization": False})
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "SUCCEEDED"
    assert len(setup.client.calls) == 1


def test_output_boundary_rejects_external_and_symlink(setup: Setup) -> None:
    for root in (Path("/tmp/escape"), setup.root / "../escape"):
        setup.policy = setup.policy.model_copy(update={"output_root": root})
        with pytest.raises(PathGuardError):
            setup.worker()
    link = setup.root / "symlink"
    link.symlink_to("/tmp", target_is_directory=True)
    setup.policy = setup.policy.model_copy(update={"output_root": link})
    with pytest.raises(PathGuardError):
        setup.worker()


def test_edit_requests_scope_utf16_and_no_style_shrinking() -> None:
    doc = template()
    edits = {p.key: "Python \U0001f916" for p in slots(doc)}
    requests = edit_requests(doc, edits)
    assert all(set(r) <= {"deleteContentRange", "insertText", "updateTextStyle"} for r in requests)
    for r in requests:
        if "updateTextStyle" in r:
            value = r["updateTextStyle"]
            assert value["range"]["endIndex"] - value["range"]["startIndex"] == 9
            assert value["range"]["segmentId"] == "header-one"
            assert value["range"]["tabId"] == "tab-one"
            assert value["textStyle"]["fontSize"]["magnitude"] == 11
    with pytest.raises(ValueError, match="scope"):
        edit_requests(doc, {"unauthorized": "invented"})


def test_protected_format_tampering_is_detected() -> None:
    doc = template()
    modified = copy.deepcopy(doc)
    modified["documentStyle"]["marginTop"]["magnitude"] = 1
    assert signature(doc, edited=True) != signature(modified, edited=True)
    modified = copy.deepcopy(doc)
    paragraphs(modified)[0].paragraph["elements"][0]["textRun"]["content"] = "Unauthorized\n"
    assert signature(doc, edited=True) != signature(modified, edited=True)


def test_live_docs_opt_in_and_mock_http_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_DOCS_ALLOW_LIVE", raising=False)
    with pytest.raises(ValueError, match="opt_in"):
        GoogleDocsHTTP(folder_id="folder")
    monkeypatch.setenv("GOOGLE_DOCS_ALLOW_LIVE", "1")
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "fixture-only")
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and "documents" in request.url.path:
            return httpx.Response(200, json=template())
        if request.url.path.endswith("export"):
            return httpx.Response(200, content=export_pdf(template()))
        if request.method == "GET":
            return httpx.Response(200, json={"files": [{"id": "copy-id"}]})
        return httpx.Response(200, json={"id": "copy-id"})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    adapter = GoogleDocsHTTP(folder_id="folder", client=client)
    assert adapter.copy("template", "Generated", "action") == "copy-id"
    assert adapter.find("action") == "copy-id"
    adapter.edit(adapter.get("template"), {p.key: "Python" for p in slots(template())})
    assert pdf_pages(adapter.export("copy-id"), ["Synthetic Candidate"]) == 1
    request = next(r for r in seen if r.url.path.endswith(":batchUpdate"))
    assert json.loads(request.content)["writeControl"] == {"requiredRevisionId": "1"}
    assert all(r.method in {"GET", "POST"} for r in seen)
    adapter.close()


def test_publication_transaction_rolls_back_all_artifacts(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ArtifactRepository.create
    calls = 0

    def fail_second(self: ArtifactRepository, data: Any) -> Artifact:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture_publication_failure")
        return original(self, data)

    monkeypatch.setattr(ArtifactRepository, "create", fail_second)
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "FAILED"
    assert not setup.artifacts() and not setup.tasks("FORM_PROCESS")
    with setup.database.transaction() as session:
        details = session.get(ApplicationDetails, setup.app_id)
        assert (
            details and not details.resume_artifact_id and details.application_status == "QUALIFIED"
        )


def test_cover_letter_also_compresses_without_duplicate_body(setup: Setup) -> None:
    setup.policy = setup.policy.model_copy(update={"cover_letter": True})
    setup.run()
    setup.adapter.exporter = lambda doc: export_pdf(
        doc, pages=2 if "Tested Python" in "".join(p.text for p in paragraphs(doc)) else 1
    )
    worker = setup.run()
    task = setup.tasks("BUILD_COVER_LETTER")[0]
    assert task.task_status == "SUCCEEDED"
    data = worker._load(task.task_id)
    assert data and data["page_counts"] == [2, 1]
    assert (
        "".join(p.text for p in paragraphs(setup.adapter.get(data["document_id"]))).count(
            "Dear Hiring Team"
        )
        == 1
    )


def test_missing_verified_facts_waits_and_can_resume_after_review(setup: Setup) -> None:
    with setup.database.transaction() as session:
        repository = CandidateFactRepository(session, CLOCK)
        for key in setup.projects + setup.skills:
            repository.decide(
                key, "UNVERIFIED", expected_status="VERIFIED", reviewer="fixture_human"
            )
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "WAITING_HUMAN"
    assert not setup.client.calls and not setup.artifacts()
    memory = setup.queue.memory(setup.task_id)
    with setup.database.transaction() as session:
        repository = CandidateFactRepository(session, CLOCK)
        for key in setup.projects + setup.skills:
            repository.decide(
                key, "VERIFIED", expected_status="UNVERIFIED", reviewer="fixture_human"
            )
    setup.queue.resume(setup.task_id, next(iter(memory["interrupts"])), "reviewed")  # type: ignore[call-overload]
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "SUCCEEDED"


def test_protected_hyperlinks_and_later_run_font_cannot_change() -> None:
    from job_hunting_machine.resume.native import replacements, validate_edit

    doc = template()
    first = paragraphs(doc)[0].paragraph["elements"][0]["textRun"]
    first["textStyle"]["link"] = {"url": "https://example.invalid/original"}
    changed = copy.deepcopy(doc)
    paragraphs(changed)[0].paragraph["elements"][0]["textRun"]["textStyle"]["link"]["url"] = (
        "https://example.invalid/wrong"
    )
    assert signature(doc, edited=True) != signature(changed, edited=True)
    edits = replacements(doc, ["Python project"], ["Python"])
    changed = copy.deepcopy(doc)
    for p in slots(changed):
        p.paragraph["elements"] = [
            {"textRun": {"content": edits[p.key] + "\n", "textStyle": p.style}}
        ]
    target = next(p for p in slots(changed) if edits[p.key] == "Python project")
    target.paragraph["elements"] = [
        {"textRun": {"content": "Python ", "textStyle": target.style}},
        {
            "textRun": {
                "content": "project\n",
                "textStyle": {**target.style, "fontSize": {"magnitude": 8, "unit": "PT"}},
            }
        },
    ]
    with pytest.raises(ValueError, match="style_changed"):
        validate_edit(doc, changed, edits)


def test_unknown_model_result_never_repeats_billable_call(setup: Setup) -> None:
    worker = setup.worker()
    lease = setup.queue.claim(("BUILD_RESUME",))
    assert lease
    data = worker._start(lease)
    data["model_pending"] = "resume_planning"
    worker._save(lease, data)
    asyncio.run(worker._execute(lease))
    assert setup.queue.get(setup.task_id).task_status == "WAITING_HUMAN"
    assert not setup.client.calls and not setup.tasks("FORM_PROCESS")


def test_preserved_template_content_requires_explicit_verification(setup: Setup) -> None:
    setup.policy = setup.policy.model_copy(update={"template_fact_id": None})
    setup.run()
    assert setup.queue.get(setup.task_id).task_status == "WAITING_HUMAN"
    assert not setup.artifacts() and setup.adapter.copies == 0
