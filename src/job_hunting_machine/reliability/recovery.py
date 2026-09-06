"""One local coordinator for safe startup recovery; it performs no external calls."""

from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from job_hunting_machine.browser.actions import ExternalActionService as BrowserActions
from job_hunting_machine.database.engine import Database
from job_hunting_machine.orchestration.queue import QueueService
from job_hunting_machine.outreach.actions import ExternalActionService as OutreachActions
from job_hunting_machine.outreach.gmail import FakeGmailAdapter
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.slack.actions import ExternalActionService as SlackActions
from job_hunting_machine.slack.config import SlackSettings
from job_hunting_machine.submission.fake import FakeSubmissionAdapter
from job_hunting_machine.submission.service import ExternalActionService as SubmissionActions


class RecoveryError(RuntimeError):
    """Recovery cannot safely operate on the current durable state."""


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    queue_tasks: int
    browser_actions: int
    slack_actions: int
    submission_actions: int
    gmail_actions: int

    @property
    def total(self) -> int:
        return (
            self.queue_tasks
            + self.browser_actions
            + self.slack_actions
            + self.submission_actions
            + self.gmail_actions
        )


class RecoveryService:
    def __init__(self, database: Database, *, slack_settings: SlackSettings) -> None:
        self.database = database
        self.queue = QueueService(database)
        self.slack_settings = slack_settings

    def recover(self) -> RecoveryResult:
        """Recover stale ownership and mark uncertain effects; never repeat an effect."""
        from job_hunting_machine.reliability.doctor import Doctor

        try:
            compatible = Doctor(self.database).database_integrity()["ok"]
        except (SQLAlchemyError, LookupError, ValueError) as error:
            raise RecoveryError("recovery_database_compatibility_failed") from error
        if not compatible:
            raise RecoveryError("recovery_database_compatibility_failed")
        queue_tasks = self.queue.recover()
        browser_actions = BrowserActions(self.queue).recover()
        slack_actions = SlackActions(self.database, self.slack_settings).recover()
        submission_actions = SubmissionActions(
            self.queue,
            FakeSubmissionAdapter(),
            runtime_mode=RuntimeMode.DRY_RUN,
            authorized_user_ids=frozenset(),
        ).recover()
        gmail_actions = OutreachActions(
            self.queue,
            FakeGmailAdapter(),
            runtime_mode=RuntimeMode.DRY_RUN,
            authorized_user_ids=frozenset(),
        ).recover()
        return RecoveryResult(
            queue_tasks,
            browser_actions,
            slack_actions,
            submission_actions,
            gmail_actions,
        )
