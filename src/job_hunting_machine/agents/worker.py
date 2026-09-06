"""Phase 5 effect boundaries around a replay-safe qualification graph."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from job_hunting_machine.agents.extraction import JobFacts
from job_hunting_machine.agents.fetch import JobFetcher
from job_hunting_machine.agents.qualification import (
    Decision,
    evaluate,
    policy_snapshot,
    semantic_check,
    semantic_needed,
)
from job_hunting_machine.agents.retrieve_links import retrieve
from job_hunting_machine.clock import format_utc
from job_hunting_machine.database.models import Job
from job_hunting_machine.database.repositories import (
    ApplicationCreate,
    ApplicationRepository,
    JobRepository,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.models.gateway import ModelGateway
from job_hunting_machine.orchestration.checkpoints import run_blocking
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.orchestration.workflows import Workflow, WorkflowState
from job_hunting_machine.security.paths import PathGuard


def qualification_workflow() -> Workflow:
    def decide(state: WorkflowState) -> dict[str, str]:
        payload = state["payload"]
        facts = JobFacts.model_validate(payload["facts"])
        return {"result": evaluate(facts, payload["policy"], payload["at"]).model_dump_json()}

    graph = StateGraph(WorkflowState)
    graph.add_node("decide", decide)
    graph.add_edge(START, "decide")

    def human(state: WorkflowState) -> dict[str, str]:
        interrupt(
            {"question": "Job page requires human access review. No challenge bypass is allowed."}
        )
        return {"result": state["result"]}

    graph.add_node("human", human)
    graph.add_conditional_edges(
        "decide",
        lambda state: "human" if "human_challenge" in state["payload"]["facts"]["issues"] else END,
    )
    graph.add_edge("human", END)
    return Workflow("qualification-v1", graph)


class QualificationWorker(Worker):
    def __init__(
        self,
        queue: QueueService,
        *,
        fetcher: JobFetcher | None = None,
        gateway: ModelGateway | None = None,
        checkpoint_path: Path | None = None,
    ) -> None:
        workflow = qualification_workflow()
        super().__init__(
            queue,
            {"RETRIEVE_LINKS": workflow, "QUALIFY_JOB": workflow},
            checkpoint_path=checkpoint_path
            or queue.database.path.parent / "langgraph-checkpoints.db",
        )
        self.fetcher = fetcher or JobFetcher()
        self.gateway = gateway

    def _store(self, lease: Lease, memory: dict[str, object], data: dict[str, Any]) -> None:
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
        sha = hashlib.sha256(encoded.encode()).hexdigest()
        guard = PathGuard()
        directory = guard.mkdir(
            self.fetcher.root / "tasks" / lease.task_id, parents=True, exist_ok=True
        )
        target = directory / f"{sha}.json"
        # Files are immutable by content identity. Orphan files after a lost lease are harmless.
        guard.write_text(target, encoded)
        memory["qualification_snapshot"] = {"path": str(target), "sha256": sha}
        self.queue.save_memory(lease, memory)

    def _load(self, memory: dict[str, object]) -> dict[str, Any]:
        pointer = memory["qualification_snapshot"]
        if not isinstance(pointer, dict):
            raise ValueError("invalid_qualification_snapshot")
        path = PathGuard().validate_write(str(pointer["path"]))
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != pointer["sha256"]:
            raise ValueError("qualification_snapshot_hash_mismatch")
        value: dict[str, Any] = json.loads(content)
        raw = PathGuard().validate_write(value["raw_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError("job_evidence_hash_mismatch")
        return value

    def _start(self, lease: Lease) -> tuple[str, str, dict[str, Any]]:
        with self.queue.fence(lease) as session:
            task = self.queue.get(lease.task_id)
            if not task.job_id or task.job_id != lease.payload.get("job_id"):
                raise ValueError("qualification_task_job_mismatch")
            job = session.get(Job, task.job_id)
            if job is None:
                raise ValueError("qualification_job_missing")
            if job.qualification_status == "NEW":
                JobRepository(session, self.queue.clock).set_qualification_status(
                    job.job_id, "ACTIVE", expected_status="NEW"
                )
            elif job.qualification_status != "ACTIVE":
                raise ValueError("qualification_job_not_active")
            return job.job_id, job.canonical_url, policy_snapshot(session)

    async def _execute(self, lease: Lease) -> None:
        if lease.task_type == "RETRIEVE_LINKS":
            ids = await run_blocking(retrieve, self.queue, lease)
            await run_blocking(self.queue.complete, lease, {"job_ids": ids})
            return
        memory = await run_blocking(self.queue.memory, lease.task_id)
        if "qualification_snapshot" in memory:
            data = await run_blocking(self._load, memory)
        else:
            job_id, url, policy = await run_blocking(self._start, lease)
            evidence = await self.fetcher.fetch(url, job_id)
            data = {
                "job_id": job_id,
                "facts": evidence.facts.model_dump(),
                "policy": policy,
                "at": format_utc(self.queue.clock.now()),
                "raw_path": evidence.raw_path,
                "text_path": evidence.text_path,
                "sha256": evidence.sha256,
                "attempts": evidence.attempts,
            }
            await run_blocking(self._store, lease, memory, data)
        facts = JobFacts.model_validate(data["facts"])
        decision = evaluate(facts, data["policy"], data["at"])
        if (
            self.gateway is not None
            and semantic_needed(decision, facts)
            and not data.get("semantic_started")
        ):
            data["semantic_started"] = True
            await run_blocking(self._store, lease, memory, data)
            facts = await semantic_check(self.gateway, lease.task_id, facts, decision)
            data["facts"] = facts.model_dump()
            data["semantic_completed"] = True
            await run_blocking(self._store, lease, memory, data)
        # An interrupted model request is not blindly billed again: original unknowns review.
        await super()._execute(replace(lease, payload=data))

    def _publish(self, lease: Lease, data: dict[str, Any], decision: Decision) -> None:
        with self.queue.fence(lease) as session:
            job = session.get(Job, data["job_id"])
            if job is None:
                raise ValueError("qualification_job_missing")
            if job.qualification_status in {"PASSED", "ABORTED", "NEEDS_REVIEW"}:
                return  # Commit-before-checkpoint/queue-completion recovery.
            if job.qualification_status != "ACTIVE":
                raise ValueError("qualification_state_conflict")
            facts = JobFacts.model_validate(data["facts"])
            # Recheck time-sensitive rules at publication after a long interruption.
            decision = evaluate(facts, data["policy"], format_utc(self.queue.clock.now()))
            for key in (
                "company_name",
                "job_title",
                "employment_type",
                "country_code",
                "city",
                "state_region",
                "salary_min",
                "salary_max",
                "salary_currency",
                "salary_period",
                "required_experience_min",
                "required_experience_max",
                "posting_date",
                "application_deadline",
                "internship_start",
                "internship_end",
            ):
                setattr(job, key, getattr(facts, key))
            job.raw_snapshot_path, job.description_text_path, job.content_sha256 = (
                data["raw_path"],
                data["text_path"],
                data["sha256"],
            )
            job.work_authorization_text = f"CPT={facts.cpt}; US authorization={facts.authorization}"
            job.location_text = ", ".join(
                v for v in (facts.city, facts.state_region, facts.country_code) if v
            )
            job.failed_rules_json = json.dumps(
                [r.rule_id for r in decision.rules if r.result == "FAIL"]
            )
            job.unknown_rules_json = json.dumps(
                [r.rule_id for r in decision.rules if r.result == "REVIEW"]
            )
            job.notes = decision.model_dump_json()
            job.last_checked_at = format_utc(self.queue.clock.now())
            job.qualification_confidence = (
                None  # Individual evidence carries confidence; do not invent it.
            )
            session.flush()
            status = {"PASS": "PASSED", "FAIL": "ABORTED", "REVIEW": "NEEDS_REVIEW"}[
                decision.outcome
            ]
            JobRepository(session, self.queue.clock).set_qualification_status(
                job.job_id, status, expected_status="ACTIVE"
            )
            if status == "PASSED":
                assert facts.company_name and facts.job_title
                ApplicationRepository(session, self.queue.clock).create_from_passed_job(
                    job.job_id,
                    ApplicationCreate(
                        facts.company_name,
                        facts.job_title,
                        job.canonical_url,
                        location_text=job.location_text,
                        employment_type=job.employment_type,
                    ),
                )
            ActivityLogRepository(session, self.queue.clock).append(
                ActivityEvent(
                    "qualification_decided",
                    task_id=lease.task_id,
                    job_id=job.job_id,
                    new_state=status,
                    metadata={
                        "rule_set_id": data["policy"]["rule_set_id"],
                        "evidence_sha256": data["sha256"],
                        "decision": decision.model_dump(),
                        "snapshot": self.queue.memory(lease.task_id).get("qualification_snapshot"),
                    },
                )
            )

    async def _record(
        self, lease: Lease, memory: dict[str, object], snapshot: Any, interrupts: dict[str, Any]
    ) -> None:
        data = await run_blocking(self._load, memory)
        decision = Decision.model_validate_json(snapshot.values["result"])
        await run_blocking(self._publish, lease, data, decision)
        await super()._record(lease, memory, snapshot, interrupts)


async def run_workers(workers: list[QualificationWorker]) -> None:
    """Bounded pool with one signal owner; each worker drains its active lease safely."""
    import asyncio
    import signal

    if not 1 <= len(workers) <= 8:
        raise ValueError("qualification_pool_requires_one_to_eight_workers")
    loop = asyncio.get_running_loop()
    installed = (signal.SIGINT, signal.SIGTERM)

    def stop() -> None:
        for worker in workers:
            worker.request_stop()

    for sig in installed:
        loop.add_signal_handler(sig, stop)
    try:
        async with asyncio.TaskGroup() as group:
            for worker in workers:
                group.create_task(worker.run())
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
