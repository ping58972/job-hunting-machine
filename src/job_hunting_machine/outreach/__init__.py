"""Phase 10 Connector Agent and approval-bound Outreach Sender."""

from job_hunting_machine.outreach.actions import (
    ConnectorExternalActionService,
    ExternalActionService,
)
from job_hunting_machine.outreach.connector import ConnectorWorker
from job_hunting_machine.outreach.discovery import ContactDiscovery
from job_hunting_machine.outreach.gmail import FakeGmailAdapter
from job_hunting_machine.outreach.manual import ManualOutreachWorker
from job_hunting_machine.outreach.sender import OutreachSender

__all__ = [
    "ConnectorExternalActionService",
    "ConnectorWorker",
    "ContactDiscovery",
    "ExternalActionService",
    "FakeGmailAdapter",
    "ManualOutreachWorker",
    "OutreachSender",
]
