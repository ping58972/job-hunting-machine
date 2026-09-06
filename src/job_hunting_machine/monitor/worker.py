"""Lease-aware, retry-safe Gmail and portal monitor worker."""

from dataclasses import replace

from job_hunting_machine.database.models import ApplicationDetails, ApplicationPipeline
from job_hunting_machine.monitor.config import MonitorSettings
from job_hunting_machine.monitor.portal import PortalAdapterRegistry
from job_hunting_machine.monitor.service import MonitorService
from job_hunting_machine.monitor.types import TERMINAL_STATUSES, GmailReader, PortalReader
from job_hunting_machine.orchestration.queue import Lease, QueueService, RetryableError
from job_hunting_machine.slack.control import SlackControlPlane
from job_hunting_machine.slack.messages import Notice


class MonitorWorker:
    def __init__(
        self,
        queue: QueueService,
        service: MonitorService,
        gmail: GmailReader,
        portal: PortalReader,
        settings: MonitorSettings,
        *,
        slack: SlackControlPlane | None = None,
    ) -> None:
        self.queue = queue
        self.service = service
        self.gmail = gmail
        self.portal = portal
        self.settings = settings
        self.slack = slack
        self.adapters = PortalAdapterRegistry()

    def _details(self, lease: Lease) -> tuple[ApplicationPipeline, ApplicationDetails]:
        if not lease.payload.get("application_id") or lease.payload["application_id"] != (
            self.queue.get(lease.task_id).application_id
        ):
            raise ValueError("monitor_payload_mismatch")
        application_id = str(lease.payload["application_id"])
        with self.queue.database.transaction() as session:
            app = session.get(ApplicationPipeline, application_id)
            details = session.get(ApplicationDetails, application_id)
            if app is None or details is None:
                raise ValueError("monitor_application_missing")
            return app, details

    async def execute(self, lease: Lease) -> dict[str, object]:
        app, details = self._details(lease)
        if app.application_status in TERMINAL_STATUSES:
            return {"terminal": True, "email_events": 0, "portal_events": 0, "changed": False}
        query_company = details.company_name.replace('"', " ")[:200]
        query_title = details.job_title.replace('"', " ")[:200]
        messages = await self.gmail.search(
            f'newer_than:30d ("{query_company}" OR "{query_title}")',
            limit=self.settings.max_gmail_messages_per_run,
        )
        email_events = 0
        changed = False
        for message in messages:
            result = await self.service.email(lease.task_id, app.application_id, message)
            if result is not None and not result.duplicate:
                email_events += 1
                changed = changed or result.changed
        # An email may have made the application terminal. Do not make a portal request afterward.
        with self.queue.database.transaction() as session:
            current = session.get(ApplicationPipeline, app.application_id)
            assert current is not None
            terminal = current.application_status in TERMINAL_STATUSES
        portal_events = 0
        if not terminal:
            url = details.application_url or details.job_url
            page = await self.portal.read(url)
            adapter = self.adapters.select(details.ats_type, url, page)
            page = replace(page, text=adapter.evidence_text(page), vendor=adapter.kind)
            result = await self.service.portal(lease.task_id, app.application_id, page)
            if not result.duplicate:
                portal_events = 1
                changed = changed or result.changed
        if changed and self.slack is not None:
            self.slack.notify(lease.task_id, Notice.STATUS_CHANGED)
        return {
            "terminal": terminal,
            "email_events": email_events,
            "portal_events": portal_events,
            "changed": changed,
        }

    async def startup(self) -> int:
        return self.queue.recover()

    async def run_once(self) -> bool:
        lease = self.queue.claim(["MONITOR_APPLICATION"])
        if lease is None:
            return False
        try:
            memory = await self.execute(lease)
        except RetryableError:
            self.queue.fail(lease, retryable=True)
        except Exception:
            self.queue.fail(lease, retryable=False)
        else:
            self.queue.complete(lease, memory)
        return True
