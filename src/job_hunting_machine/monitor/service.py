"""Lease-independent monitoring use cases; callers own queue completion."""

import hashlib

from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import AgentTask, ApplicationDetails, ApplicationPipeline
from job_hunting_machine.monitor.classification import matches_application
from job_hunting_machine.monitor.config import MonitorSettings
from job_hunting_machine.monitor.evidence import MonitorEvidenceStore
from job_hunting_machine.monitor.repository import MonitorEventRepository, MonitorResult
from job_hunting_machine.monitor.types import GmailMessage, PortalPage, StatusClassifier


class MonitorService:
    def __init__(
        self,
        database: Database,
        classifier: StatusClassifier,
        settings: MonitorSettings,
        *,
        evidence: MonitorEvidenceStore | None = None,
    ) -> None:
        self.database = database
        self.classifier = classifier
        self.settings = settings
        self.evidence = evidence or MonitorEvidenceStore()

    def _application(self, task_id: str, application_id: str) -> tuple[str, str, str | None, str]:
        with self.database.transaction() as session:
            task = session.get(AgentTask, task_id)
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            if task is None or task.task_type != "MONITOR_APPLICATION":
                raise ValueError("monitor_task_invalid")
            if task.application_id != application_id or app is None or details is None:
                raise ValueError("monitor_task_application_mismatch")
            return (
                details.company_name,
                details.job_title,
                details.application_url,
                app.application_status,
            )

    async def email(
        self, task_id: str, application_id: str, message: GmailMessage
    ) -> MonitorResult | None:
        company, title, _, _ = self._application(task_id, application_id)
        source_reference = f"gmail:{message.provider_id}"
        with self.database.transaction() as session:
            duplicate = MonitorEventRepository(session).get_by_source("EMAIL", source_reference)
            if duplicate:
                return MonitorResult(
                    duplicate.monitor_event_id,
                    False,
                    True,
                    duplicate.previous_status,
                    duplicate.detected_status,
                )
        if not matches_application(message, application_id, company, title):
            return None
        classification = await self.classifier.classify_email(task_id, application_id, message)
        evidence_path = self.evidence.email(application_id, message)
        with self.database.transaction(immediate=True) as session:
            return MonitorEventRepository(session).record(
                task_id=task_id,
                application_id=application_id,
                source_type="EMAIL",
                source_reference=source_reference,
                evidence_path=evidence_path,
                classification=classification,
                transition_confidence=self.settings.transition_confidence,
                destructive_confidence=self.settings.destructive_confidence,
            )

    async def portal(self, task_id: str, application_id: str, page: PortalPage) -> MonitorResult:
        self._application(task_id, application_id)
        content_hash = hashlib.sha256(
            f"{page.url}\n{page.status_code}\n{page.text}".encode()
        ).hexdigest()
        source_reference = f"portal:{content_hash}"
        with self.database.transaction() as session:
            duplicate = MonitorEventRepository(session).get_by_source("PORTAL", source_reference)
            if duplicate:
                return MonitorResult(
                    duplicate.monitor_event_id,
                    False,
                    True,
                    duplicate.previous_status,
                    duplicate.detected_status,
                )
        classification = await self.classifier.classify_portal(task_id, application_id, page)
        evidence_path = self.evidence.portal(application_id, page, source_reference)
        with self.database.transaction(immediate=True) as session:
            return MonitorEventRepository(session).record(
                task_id=task_id,
                application_id=application_id,
                source_type="PORTAL",
                source_reference=source_reference,
                evidence_path=evidence_path,
                classification=classification,
                transition_confidence=self.settings.transition_confidence,
                destructive_confidence=self.settings.destructive_confidence,
            )
