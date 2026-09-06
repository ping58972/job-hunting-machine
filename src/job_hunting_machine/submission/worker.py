"""Dedicated queue worker for final submission and reconciliation only."""

from pathlib import Path

from job_hunting_machine.orchestration.checkpoints import run_blocking
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.orchestration.workflows import fake_workflow
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.submission.service import ExternalActionService, SubmissionError
from job_hunting_machine.submission.types import SubmissionAdapter, SubmissionOutcome


class SubmissionWorker(Worker):
    def __init__(
        self,
        queue: QueueService,
        adapter: SubmissionAdapter,
        *,
        runtime_mode: RuntimeMode,
        authorized_user_ids: frozenset[str],
        checkpoint_path: Path | None = None,
    ) -> None:
        super().__init__(
            queue,
            {"SUBMIT_APPLICATION": fake_workflow()},
            checkpoint_path=checkpoint_path
            or queue.database.path.parent / "langgraph-checkpoints.db",
        )
        self.runtime_mode = runtime_mode
        self.actions = ExternalActionService(
            queue,
            adapter,
            runtime_mode=runtime_mode,
            authorized_user_ids=authorized_user_ids,
        )

    async def startup(self) -> int:
        return await super().startup() + await run_blocking(self.actions.recover)

    async def _execute(self, lease: Lease) -> None:
        try:
            result = await self.actions.execute(lease)
        except SubmissionError as error:
            await run_blocking(
                self.queue.complete,
                lease,
                {"submission_blocked": str(error), "interrupts": {"submission": str(error)}},
                waiting=True,
            )
            return
        new_review_task = None
        if result.outcome is SubmissionOutcome.NOT_SUBMITTED:
            application_id = lease.payload.get("application_id")
            if isinstance(application_id, str):
                new_review_task = await run_blocking(
                    self.actions.schedule_new_review, lease, application_id
                )
        memory: dict[str, object] = {
            "submission_outcome": result.outcome.value,
            "action_id": result.action_id,
            "new_review_task_id": new_review_task,
        }
        if result.outcome is SubmissionOutcome.UNKNOWN:
            memory["interrupts"] = {"submission": {"kind": "SUBMISSION_RECONCILIATION_REQUIRED"}}
        await run_blocking(
            self.queue.complete,
            lease,
            memory,
            waiting=result.outcome is SubmissionOutcome.UNKNOWN,
        )
