"""Durable, lease-fenced local LaTeX document generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import (
    ApplicationDetails,
    ApplicationPipeline,
    Artifact,
    Job,
)
from job_hunting_machine.database.repositories import TaskCreate, TaskRepository
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.artifacts import ArtifactCreate, ArtifactRepository
from job_hunting_machine.ids import IdKind
from job_hunting_machine.models.gateway import ModelGateway
from job_hunting_machine.models.prompts import digest
from job_hunting_machine.models.router import Tier
from job_hunting_machine.orchestration.checkpoints import run_blocking
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.resume.artifacts import (
    encoded,
    filename_part,
    immutable,
    pdf_pages,
    read_record,
)
from job_hunting_machine.resume.config import ResumePolicy
from job_hunting_machine.resume.errors import ReviewRequired
from job_hunting_machine.resume.latex import (
    COVER_LETTER,
    PROJECTS,
    SKILLS,
    Compiler,
    LatexCompiler,
    LatexTemplate,
    latex_escape,
)
from job_hunting_machine.resume.planning import (
    Selection,
    candidate_pool,
    compress,
    render,
    revalidate,
    validate_selection,
)
from job_hunting_machine.security.paths import PathGuard


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean_plain_text(value: str) -> str:
    return " ".join(value.split())


def render_resume_sections(projects: list[str], skills: list[str]) -> dict[str, str]:
    if not projects or not skills:
        raise ReviewRequired("resume_requires_verified_projects_and_skills")
    project_lines = [r"\begin{itemize}"]
    project_lines.extend(f"  \\item {latex_escape(_clean_plain_text(item))}" for item in projects)
    project_lines.append(r"\end{itemize}")
    skill_text = ", ".join(latex_escape(_clean_plain_text(item)) for item in skills)
    return {
        PROJECTS: "\n".join(project_lines),
        SKILLS: "\n".join((r"\begin{itemize}", f"  \\item {skill_text}", r"\end{itemize}")),
    }


def cover_letter_plain_text(
    company: str,
    position: str,
    projects: list[str],
    skills: list[str],
) -> str:
    facts = "\n\n".join(_clean_plain_text(item) for item in projects)
    skill_text = ", ".join(_clean_plain_text(item) for item in skills)
    return (
        f"Dear Hiring Team,\n\nI am interested in the {position} position at {company}.\n\n"
        f"{facts}\n\nRelevant verified skills: {skill_text}.\n\n"
        "Thank you for your consideration."
    )


def render_cover_letter(content: str) -> dict[str, str]:
    paragraphs = [latex_escape(_clean_plain_text(item)) for item in content.split("\n\n")]
    return {COVER_LETTER: "\n\n".join(paragraphs)}


class ResumeWorker(Worker):
    """Generate immutable TEX/PDF pairs and advance only after one-page validation."""

    def __init__(
        self,
        queue: QueueService,
        gateway: ModelGateway,
        *,
        compiler: Compiler | None = None,
        policy: ResumePolicy | None = None,
    ) -> None:
        super().__init__(
            queue, {}, checkpoint_path=queue.database.path.parent / "langgraph-checkpoints.db"
        )
        self.task_types = ("BUILD_RESUME", "BUILD_COVER_LETTER")
        self.gateway = gateway
        self.policy = policy or ResumePolicy()
        self.policy.validate_paths()
        self.compiler = compiler or LatexCompiler(
            preferred=self.policy.preferred_compiler,
            timeout_seconds=self.policy.timeout_seconds,
        )

    def _save(self, lease: Lease, data: dict[str, Any]) -> None:
        with self.queue.fence(lease):
            record = immutable(
                self.policy.build_root / "runs" / lease.task_id / f"{digest(data)}.json",
                encoded(data),
            )
        memory = self.queue.memory(lease.task_id)
        memory["resume_snapshot"] = record
        self.queue.save_memory(lease, memory)

    def _load(self, task_id: str) -> dict[str, Any] | None:
        record = self.queue.memory(task_id).get("resume_snapshot")
        if record is None:
            return None
        if not isinstance(record, dict):
            raise ReviewRequired("invalid_resume_memory")
        data: dict[str, Any] = json.loads(read_record(record))
        if data["version"] != 2 or data["policy"] != self.policy.model_dump(mode="json"):
            raise ReviewRequired("resume_policy_changed_during_run")
        return data

    @staticmethod
    def _next_artifact_version(session: Any, application_id: str, kind: str) -> int:
        artifact_type = "RESUME_PDF" if kind == "resume" else "COVER_LETTER_PDF"
        current = session.scalar(
            select(func.max(Artifact.version)).where(
                Artifact.application_id == application_id,
                Artifact.artifact_type == artifact_type,
            )
        )
        return int(current or 0) + 1

    def _start(self, lease: Lease) -> dict[str, Any]:
        with self.queue.fence(lease) as session:
            task = self.queue._get(session, lease.task_id)
            app = session.get(ApplicationPipeline, task.application_id)
            details = session.get(ApplicationDetails, task.application_id)
            job = session.get(Job, task.job_id)
            if not app or not details or not job or app.job_id != job.job_id:
                raise ReviewRequired("resume_task_application_mismatch")
            if app.pipeline_stage != "RESUME" or app.current_task_id != lease.task_id:
                raise ReviewRequired("application_not_ready_for_document_generation")
            kind = "resume" if lease.task_type == "BUILD_RESUME" else "cover"
            requirements = details.job_title
            if job.description_text_path:
                description = PathGuard().validate_write(job.description_text_path).read_text()
                requirements += " " + description[:12000]
            template_path = (
                self.policy.resume_template
                if kind == "resume"
                else self.policy.cover_letter_template
            )
            required = (PROJECTS, SKILLS) if kind == "resume" else (COVER_LETTER,)
            LatexTemplate(template_path.read_text(encoding="utf-8"), required)
            data: dict[str, Any] = {
                "version": 2,
                "policy": self.policy.model_dump(mode="json"),
                "application_id": app.application_id,
                "job_id": job.job_id,
                "company": details.company_name,
                "position": details.job_title,
                "requirements": requirements,
                "date": self.queue.clock.now().strftime("%m%d%Y"),
                "round": 0,
                "kind": kind,
                "artifact_version": self._next_artifact_version(session, app.application_id, kind),
                "artifact_ids": {
                    key: self.queue.ids.new(IdKind.ARTIFACT) for key in ("tex", "pdf")
                },
                "template_path": str(template_path),
                "template_sha256": _file_hash(template_path),
                "checkpoints": ["TEMPLATE_VALIDATED"],
            }
            if kind == "cover":
                resume_task = lease.payload.get("resume_task_id")
                if not isinstance(resume_task, str):
                    raise ReviewRequired("cover_letter_requires_validated_resume")
                prior = self._load(resume_task)
                if (
                    not prior
                    or prior["application_id"] != app.application_id
                    or not prior.get("artifacts")
                ):
                    raise ReviewRequired("cover_letter_resume_mismatch")
                data["resume_task_id"] = resume_task
                data["resume"] = prior
            ActivityLogRepository(session, self.queue.clock).append(
                ActivityEvent(
                    "document_generation_started",
                    task_id=lease.task_id,
                    application_id=app.application_id,
                )
            )
        data["pool"] = candidate_pool(self.queue.database, data["requirements"])
        if not data["pool"]:
            raise ReviewRequired("no_relevant_verified_facts")
        data["checkpoints"].append("FACTS_RETRIEVED")
        if data["kind"] == "cover":
            selected = Selection.model_validate(data["resume"]["selection"])
            allowed = set(selected.project_fact_ids + selected.skill_fact_ids)
            data["pool"] = {key: value for key, value in data["pool"].items() if key in allowed}
        return data

    async def _select(self, lease: Lease, data: dict[str, Any]) -> None:
        operations = ["resume_planning"] if data["kind"] == "resume" else ["cover_letter"]
        if data["kind"] == "resume" and self.policy.sol_finalization:
            operations.append("resume_finalization")
        for operation in operations:
            if operation in data:
                continue
            if data.get("model_pending"):
                raise ReviewRequired("model_result_unknown_review_budget_reservation")
            pool = data["pool"]
            if operation == "resume_finalization":
                plan = Selection.model_validate(data["resume_planning"])
                pool = {key: pool[key] for key in plan.project_fact_ids + plan.skill_fact_ids}
            data["model_pending"] = operation
            await run_blocking(self._save, lease, data)
            chosen = await self.gateway.structured(
                task_id=lease.task_id,
                operation=operation,
                agent_name="resume_agent",
                context=json.dumps({"requirements": data["requirements"], "pool": pool}),
                output_type=Selection,
                prompt_name="resume_selection",
                ceiling=Tier.SOL if operation == "resume_finalization" else Tier.TERRA,
            )
            validate_selection(chosen, pool)
            data[operation] = chosen.model_dump()
            data.pop("model_pending")
            await run_blocking(self._save, lease, data)
        if "selection" not in data:
            data["selection"] = data[operations[-1]]
            data["checkpoints"].extend(["PROJECTS_SELECTED", "SKILLS_SELECTED"])
            await run_blocking(self._save, lease, data)

    def _paths(self, lease: Lease, data: dict[str, Any]) -> tuple[Path, Path, Path]:
        output = (
            self.policy.resume_output_dir
            if data["kind"] == "resume"
            else self.policy.cover_letter_output_dir
        )
        label = "resume" if data["kind"] == "resume" else "CoverLetter"
        company = filename_part(data["company"])
        position = filename_part(data["position"])
        name = f"NDanddank_{label}_{company}_{position}_{data['date']}"
        directory = (
            output
            / data["application_id"]
            / lease.task_id
            / f"version-{data['artifact_version']}"
            / f"round-{data['round']}"
        )
        return (
            directory / f"{name}.tex",
            directory / f"{name}.pdf",
            self.policy.build_root / lease.task_id / f"round-{data['round']}",
        )

    def _check_template_and_facts(self, lease: Lease, data: dict[str, Any]) -> str:
        template = PathGuard().validate_write(data["template_path"])
        if _file_hash(template) != data["template_sha256"]:
            raise ReviewRequired("master_latex_template_changed_during_run")
        with self.queue.fence(lease) as session:
            revalidate(session, Selection.model_validate(data["selection"]), data["pool"])
        return template.read_text(encoding="utf-8")

    def _generate_round(self, lease: Lease, data: dict[str, Any]) -> None:
        source = self._check_template_and_facts(lease, data)
        projects, skills = render(Selection.model_validate(data["selection"]), data["pool"])
        required = (PROJECTS, SKILLS) if data["kind"] == "resume" else (COVER_LETTER,)
        template = LatexTemplate(source, required)
        if data["kind"] == "resume":
            rendered = template.render(render_resume_sections(projects, skills))
        else:
            plain_text = cover_letter_plain_text(
                data["company"], data["position"], projects, skills
            )
            data["plain_text"] = plain_text
            rendered = template.render(render_cover_letter(plain_text))
        tex_path, pdf_path, build_dir = self._paths(lease, data)
        with self.queue.fence(lease):
            tex = immutable(tex_path, rendered.encode("utf-8"))
        for checkpoint in ("TEX_GENERATED", "TEX_SAVED"):
            if checkpoint not in data["checkpoints"]:
                data["checkpoints"].append(checkpoint)
        if pdf_path.exists():
            pdf_bytes = PathGuard().validate_write(pdf_path).read_bytes()
        else:
            self.compiler.compile(tex_path, build_dir, pdf_path)
            pdf_bytes = PathGuard().validate_write(pdf_path).read_bytes()
        count = pdf_pages(pdf_bytes)
        pdf = immutable(pdf_path, pdf_bytes)
        data["page_counts"] = [*data.get("page_counts", []), count]
        if count != self.policy.required_pages:
            reduced = compress(Selection.model_validate(data["selection"]))
            if reduced is None or data["round"] >= self.policy.max_compression_rounds:
                raise ReviewRequired("one_page_content_compression_exhausted")
            data["selection"] = reduced.model_dump()
            data["round"] += 1
            self._save(lease, data)
            return
        data["artifacts"] = {"tex": tex, "pdf": pdf}
        data["checkpoints"].extend(["PDF_COMPILED", "PDF_VALIDATED", "ONE_PAGE_CONFIRMED"])
        self._save(lease, data)

    def _publish(self, lease: Lease, data: dict[str, Any]) -> None:
        self._check_template_and_facts(lease, data)
        for record in data["artifacts"].values():
            read_record(record)
        if pdf_pages(read_record(data["artifacts"]["pdf"])) != self.policy.required_pages:
            raise ReviewRequired("pdf_no_longer_one_page")
        with self.queue.fence(lease) as session:
            app = session.get(ApplicationPipeline, data["application_id"])
            details = session.get(ApplicationDetails, data["application_id"])
            assert app and details
            field = "resume_artifact_id" if data["kind"] == "resume" else "cover_letter_artifact_id"
            repository = ArtifactRepository(session, self.queue.clock, self.queue.ids)
            if repository.get(data["artifact_ids"]["pdf"]):
                if getattr(details, field) != data["artifact_ids"]["pdf"]:
                    raise ReviewRequired("published_artifact_reference_changed")
                return
            if app.pipeline_stage != "RESUME" or app.current_task_id != lease.task_id:
                raise ReviewRequired("application_changed_before_document_publication")
            if data["kind"] == "cover":
                prior = data["resume"]
                if details.resume_artifact_id != prior["artifact_ids"]["pdf"]:
                    raise ReviewRequired("selected_resume_changed")
                revalidate(session, Selection.model_validate(prior["selection"]), prior["pool"])
                for record in prior["artifacts"].values():
                    read_record(record)
            prefix = "RESUME" if data["kind"] == "resume" else "COVER_LETTER"
            for extension, mime in (("tex", "application/x-tex"), ("pdf", "application/pdf")):
                record = data["artifacts"][extension]
                repository.create(
                    ArtifactCreate(
                        artifact_type=f"{prefix}_{extension.upper()}",
                        path=record["path"],
                        sha256=record["sha256"],
                        mime_type=mime,
                        application_id=data["application_id"],
                        task_id=lease.task_id,
                        version=data["artifact_version"],
                        artifact_id=data["artifact_ids"][extension],
                    )
                )
            setattr(details, field, data["artifact_ids"]["pdf"])
            cover_required = data["kind"] == "resume" and self.policy.cover_letter
            next_type = "BUILD_COVER_LETTER" if cover_required else "FORM_PROCESS"
            next_task = TaskRepository(session, self.queue.clock).create(
                TaskCreate(
                    next_type,
                    task_status="READY",
                    parent_task_id=lease.task_id,
                    dedupe_key=f"{next_type.lower()}:{data['application_id']}",
                    application_id=data["application_id"],
                    job_id=data["job_id"],
                    payload={"resume_task_id": lease.task_id} if cover_required else None,
                )
            )
            old_status = app.application_status
            app.current_task_id = next_task.task_id
            app.application_status = details.application_status = (
                "RESUME_BUILDING" if cover_required else "FORM_READY"
            )
            app.pipeline_stage = "RESUME" if cover_required else "FORM"
            app.updated_at = details.updated_at = format_utc(self.queue.clock.now())
            data["checkpoints"].append("ARTIFACTS_REGISTERED")
            ActivityLogRepository(session, self.queue.clock).append(
                ActivityEvent(
                    "latex_document_artifacts_validated",
                    task_id=lease.task_id,
                    application_id=app.application_id,
                    old_state=old_status,
                    new_state=app.application_status,
                    metadata={
                        "artifact_ids": data["artifact_ids"],
                        "artifact_sha256": data["artifacts"],
                        "artifact_version": data["artifact_version"],
                        "fact_ids": data["selection"],
                        "page_count": 1,
                        "next_task_id": next_task.task_id,
                    },
                )
            )

    async def _execute(self, lease: Lease) -> None:
        try:
            data = await run_blocking(self._load, lease.task_id)
            if data is None:
                data = await run_blocking(self._start, lease)
                await run_blocking(self._save, lease, data)
            await self._select(lease, data)
            while "artifacts" not in data:
                await run_blocking(self._generate_round, lease, data)
            await run_blocking(self._publish, lease, data)
            if "ARTIFACTS_REGISTERED" not in data["checkpoints"]:
                data["checkpoints"].append("ARTIFACTS_REGISTERED")
            await run_blocking(self._save, lease, data)
            memory = self.queue.memory(lease.task_id)
            for key in ("interrupts", "resume", "document_review"):
                memory.pop(key, None)
            await run_blocking(self.queue.complete, lease, memory)
        except ReviewRequired as error:
            memory = self.queue.memory(lease.task_id)
            memory["document_review"] = str(error)
            memory.pop("resume", None)
            memory["interrupts"] = {
                self.queue.ids.generate_ulid(): {
                    "question": "Review the local LaTeX document issue, correct it, then retry.",
                    "reason": str(error),
                }
            }
            await run_blocking(self.queue.complete, lease, memory, waiting=True)
