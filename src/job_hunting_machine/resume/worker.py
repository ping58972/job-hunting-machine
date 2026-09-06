"""Durable Phase 7 effect state machine; no effects occur in replay-sensitive graph nodes."""

import json
from pathlib import Path
from typing import Any

from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import ApplicationDetails, ApplicationPipeline, Job
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
from job_hunting_machine.resume.actions import ExternalActionService, ReviewRequired
from job_hunting_machine.resume.artifacts import (
    encoded,
    filename_part,
    immutable,
    pdf_pages,
    read_record,
)
from job_hunting_machine.resume.config import ResumePolicy
from job_hunting_machine.resume.documents import DocumentAdapter, identifier
from job_hunting_machine.resume.native import paragraphs, replacements, signature, validate_edit
from job_hunting_machine.resume.planning import (
    Selection,
    candidate_pool,
    compress,
    render,
    revalidate,
    validate_selection,
)
from job_hunting_machine.resume.template import validate_template
from job_hunting_machine.security.paths import PathGuard


class ResumeWorker(Worker):
    def __init__(
        self,
        queue: QueueService,
        adapter: DocumentAdapter,
        gateway: ModelGateway,
        *,
        policy: ResumePolicy | None = None,
    ) -> None:
        # This effect state machine uses task memory, not a graph with embedded I/O.
        # Worker still owns claiming, heartbeat, lease recovery, shutdown and task outcomes.
        super().__init__(
            queue, {}, checkpoint_path=queue.database.path.parent / "langgraph-checkpoints.db"
        )
        self.task_types = ("BUILD_RESUME", "BUILD_COVER_LETTER")
        self.adapter, self.gateway = adapter, gateway
        self.policy = policy or ResumePolicy()
        self.policy.validate_paths()
        self.actions = ExternalActionService(queue, adapter)

    def _save(self, lease: Lease, data: dict[str, Any]) -> None:
        with self.queue.fence(lease):
            target = (
                self.policy.output_root
                / "data/resume-runs"
                / lease.task_id
                / f"{digest(data)}.json"
            )
            record = immutable(target, encoded(data))
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
        if data["version"] != 1 or data["policy"] != self.policy.model_dump(mode="json"):
            raise ReviewRequired("resume_policy_changed_during_run")
        return data

    def _start(self, lease: Lease) -> dict[str, Any]:
        with self.queue.fence(lease) as session:
            task = self.queue.get(lease.task_id)
            app = session.get(ApplicationPipeline, task.application_id)
            details = session.get(ApplicationDetails, task.application_id)
            job = session.get(Job, task.job_id)
            if not app or not details or not job or app.job_id != job.job_id:
                raise ReviewRequired("resume_task_application_mismatch")
            if app.pipeline_stage != "RESUME" or app.current_task_id != lease.task_id:
                raise ReviewRequired("application_not_ready_for_document_generation")
            requirements = details.job_title
            if job.description_text_path:
                requirements += (
                    " " + PathGuard().validate_write(job.description_text_path).read_text()[:12000]
                )
            data: dict[str, Any] = {
                "version": 1,
                "policy": self.policy.model_dump(mode="json"),
                "application_id": app.application_id,
                "job_id": job.job_id,
                "company": details.company_name,
                "position": details.job_title,
                "requirements": requirements,
                "date": self.queue.clock.now().strftime("%m%d%Y"),
                "round": 0,
                "kind": "resume" if lease.task_type == "BUILD_RESUME" else "cover",
                "artifact_ids": {
                    k: self.queue.ids.new(IdKind.ARTIFACT) for k in ("gdoc", "pdf", "snapshot")
                },
            }
            if lease.task_type == "BUILD_COVER_LETTER":
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
        if data["kind"] == "cover":
            # Cover letters must remain consistent with the selected, validated resume.
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
            await run_blocking(self._save, lease, data)

    def _check_facts(self, lease: Lease, data: dict[str, Any]) -> None:
        with self.queue.fence(lease) as session:
            revalidate(session, Selection.model_validate(data["selection"]), data["pool"])

    def _paths(self, lease: Lease, data: dict[str, Any]) -> tuple[Path, str]:
        folder, kind = (
            ("resumes", "resume") if data["kind"] == "resume" else ("cover-letters", "CoverLetter")
        )
        name = (
            f"NDanddank_{kind}_{filename_part(data['company'])}_"
            f"{filename_part(data['position'])}_{data['date']}"
        )
        # IDs prevent collisions between applications/attempts without changing required basenames.
        directory = (
            self.policy.output_root
            / folder
            / data["application_id"]
            / lease.task_id
            / f"round-{data['round']}"
        )
        PathGuard().validate_write(directory)
        return directory, name

    async def _generate(self, lease: Lease, data: dict[str, Any]) -> None:
        await run_blocking(self._check_facts, lease, data)
        directory, name = self._paths(lease, data)
        if "template" not in data:
            if data["kind"] == "resume":
                pointer = json.loads(PathGuard().validate_write(self.policy.template).read_text())
                data["source_id"] = identifier(pointer["doc_id"])
                data["template"] = await run_blocking(self.adapter.get, data["source_id"])
            else:
                data["source_id"], data["template"] = "", {}
            await run_blocking(self._save, lease, data)
        if "document_id" not in data:
            if data["kind"] == "resume":
                with self.queue.fence(lease) as session:
                    validate_template(session, data["template"], self.policy.template_fact_id)
            data["document_id"] = await run_blocking(
                self.actions.copy, lease, data["source_id"], name
            )
            await run_blocking(self._save, lease, data)
        if "before" not in data:
            data["before"] = await run_blocking(self.adapter.get, data["document_id"])
            if (
                data["kind"] == "resume"
                and data["round"] == 0
                and signature(data["template"]) != signature(data["before"])
            ):
                raise ReviewRequired("native_copy_does_not_match_template")
            await run_blocking(self._save, lease, data)
        selection = Selection.model_validate(data["selection"])
        projects, skills = render(selection, data["pool"])
        if data["kind"] == "resume":
            text = replacements(data["before"], projects, skills)
            await run_blocking(self.actions.edit, lease, data["before"], text, data["round"])
        else:
            cover_text = (
                f"Dear Hiring Team,\n\nI am interested in the {data['position']} "
                f"position at {data['company']}.\n\n"
                + "\n\n".join(projects)
                + "\n\nSkills: "
                + ", ".join(skills)
                + "\n\nThank you for your consideration.\n"
            )
            await run_blocking(self.actions.fill, lease, data["before"], cover_text, data["round"])
        current = await run_blocking(self.adapter.get, data["document_id"])
        if data["kind"] == "resume":
            try:
                validate_edit(data["template"], current, text)
            except ValueError:
                raise ReviewRequired("native_template_validation_failed") from None
        expected = [p.text for p in paragraphs(current)]
        # Bind the GDOC pointer to a snapshot, not merely to a mutable cloud URL.
        with self.queue.fence(lease):
            snapshot = immutable(directory / f"{name}.snapshot.json", encoded(current))
            pointer = {
                "doc_id": data["document_id"],
                "url": f"https://docs.google.com/document/d/{data['document_id']}/edit",
                "revision_id": current["revisionId"],
                "content_sha256": snapshot["sha256"],
            }
            gdoc = immutable(directory / f"{name}.gdoc", encoded(pointer))
        data["gdoc_saved"] = gdoc
        await run_blocking(self._save, lease, data)
        pdf_path = PathGuard().validate_write(directory / f"{name}.pdf")
        pdf = (
            pdf_path.read_bytes()
            if pdf_path.exists()
            else await run_blocking(self.adapter.export, data["document_id"])
        )
        after_export = await run_blocking(self.adapter.get, data["document_id"])
        if digest(current) != digest(after_export):
            raise ReviewRequired("native_document_changed_during_pdf_export")
        count = pdf_pages(pdf, expected)
        with self.queue.fence(lease):
            pdf_record = immutable(pdf_path, pdf)
        data["page_counts"] = [*data.get("page_counts", []), count]
        if count != 1:
            reduced = compress(selection)
            if reduced is None or data["round"] >= self.policy.max_compression_rounds:
                raise ReviewRequired("one_page_content_compression_exhausted")
            data.update(selection=reduced.model_dump(), round=data["round"] + 1)
            data.pop("before")
            data.pop("gdoc_saved", None)
            await run_blocking(self._save, lease, data)
            return
        data["artifacts"] = {"gdoc": gdoc, "pdf": pdf_record, "snapshot": snapshot}
        await run_blocking(self._save, lease, data)

    def _publish(self, lease: Lease, data: dict[str, Any]) -> None:
        with self.queue.fence(lease) as session:
            revalidate(session, Selection.model_validate(data["selection"]), data["pool"])
            template_data = data if data["kind"] == "resume" else data["resume"]
            validate_template(session, template_data["template"], self.policy.template_fact_id)
            for record in data["artifacts"].values():
                read_record(record)
            snapshot = json.loads(read_record(data["artifacts"]["snapshot"]))
            if (
                pdf_pages(
                    read_record(data["artifacts"]["pdf"]), [p.text for p in paragraphs(snapshot)]
                )
                != 1
            ):
                raise ReviewRequired("pdf_no_longer_one_page")
            app = session.get(ApplicationPipeline, data["application_id"])
            details = session.get(ApplicationDetails, data["application_id"])
            assert app and details
            field = "resume_artifact_id" if data["kind"] == "resume" else "cover_letter_artifact_id"
            artifact_repository = ArtifactRepository(session, self.queue.clock, self.queue.ids)
            if artifact_repository.get(data["artifact_ids"]["pdf"]):
                if getattr(details, field) != data["artifact_ids"]["pdf"]:
                    raise ReviewRequired("published_artifact_reference_changed")
                return  # Commit-before-worker-completion replay; never regress business state.
            if app.pipeline_stage != "RESUME" or app.current_task_id != lease.task_id:
                raise ReviewRequired("application_changed_before_document_publication")
            if data["kind"] == "cover":
                prior = data["resume"]
                revalidate(session, Selection.model_validate(prior["selection"]), prior["pool"])
                if details.resume_artifact_id != prior["artifact_ids"]["pdf"]:
                    raise ReviewRequired("selected_resume_changed")
                for record in prior["artifacts"].values():
                    read_record(record)
            for kind, record in data["artifacts"].items():
                artifact_repository.create(
                    ArtifactCreate(
                        artifact_type=(
                            "RESUME_PDF" if data["kind"] == "resume" else "COVER_LETTER_PDF"
                        )
                        if kind == "pdf"
                        else "OTHER",
                        path=record["path"],
                        sha256=record["sha256"],
                        mime_type={
                            "pdf": "application/pdf",
                            "gdoc": "application/vnd.google-apps.document",
                            "snapshot": "application/json",
                        }[kind],
                        application_id=data["application_id"],
                        task_id=lease.task_id,
                        artifact_id=data["artifact_ids"][kind],
                    )
                )
            setattr(details, field, data["artifact_ids"]["pdf"])
            cover_required = data["kind"] == "resume" and self.policy.cover_letter
            next_type = "BUILD_COVER_LETTER" if cover_required else "FORM_PROCESS"
            task = TaskRepository(session, self.queue.clock).create(
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
            app.current_task_id = task.task_id
            app.application_status = details.application_status = (
                "RESUME_BUILDING" if cover_required else "FORM_READY"
            )
            app.pipeline_stage = "RESUME" if cover_required else "FORM"
            app.updated_at = details.updated_at = format_utc(self.queue.clock.now())
            session.flush()
            ActivityLogRepository(session, self.queue.clock).append(
                ActivityEvent(
                    "document_artifacts_validated",
                    task_id=lease.task_id,
                    application_id=app.application_id,
                    old_state=old_status,
                    new_state=app.application_status,
                    metadata={
                        "artifacts": data["artifacts"],
                        "artifact_ids": data["artifact_ids"],
                        "fact_ids": data["selection"],
                        "page_count": 1,
                        "next_task_id": task.task_id,
                        "native_content_sha256": data["artifacts"]["snapshot"]["sha256"],
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
                await self._generate(lease, data)
            await run_blocking(self._publish, lease, data)
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
                    "question": "Review the local document issue, correct it, then retry.",
                    "reason": str(error),
                }
            }
            await run_blocking(self.queue.complete, lease, memory, waiting=True)
