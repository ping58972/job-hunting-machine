"""Dedicated Outreach Sender; Connector Agent has no send operation."""

from pathlib import Path

from job_hunting_machine.orchestration.checkpoints import run_blocking
from job_hunting_machine.orchestration.queue import Lease, QueueService
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.orchestration.workflows import fake_workflow
from job_hunting_machine.outreach.actions import ExternalActionService, OutreachActionError
from job_hunting_machine.outreach.types import EmailOutcome


class OutreachSender(Worker):
    def __init__(
        self,
        queue: QueueService,
        actions: ExternalActionService,
        *,
        checkpoint_path: Path | None = None,
    ) -> None:
        super().__init__(
            queue,
            {"SEND_OUTREACH_EMAIL": fake_workflow()},
            checkpoint_path=checkpoint_path
            or queue.database.path.parent / "langgraph-checkpoints.db",
        )
        self.actions = actions

    async def startup(self) -> int:
        return await super().startup() + await run_blocking(self.actions.recover)

    async def _execute(self, lease: Lease) -> None:
        try:
            result = await self.actions.send(lease)
        except OutreachActionError as error:
            await run_blocking(
                self.queue.complete,
                lease,
                {"interrupts": {"email": {"kind": str(error)}}},
                waiting=True,
            )
            return
        memory: dict[str, object] = {"email_outcome": result.outcome.value}
        if result.outcome is EmailOutcome.UNKNOWN:
            memory["interrupts"] = {"email": {"kind": "EMAIL_RESULT_UNKNOWN"}}
        await run_blocking(
            self.queue.complete,
            lease,
            memory,
            waiting=result.outcome is EmailOutcome.UNKNOWN,
        )
