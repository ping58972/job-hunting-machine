"""Phase 4 Slack control plane; inbound callbacks record decisions, never submit."""

from job_hunting_machine.slack.actions import ExternalActionService
from job_hunting_machine.slack.adapter import FakeSlackAdapter
from job_hunting_machine.slack.config import SlackInputError, SlackSettings
from job_hunting_machine.slack.control import SlackControlPlane
from job_hunting_machine.slack.messages import Notice, Question

__all__ = [
    "ExternalActionService",
    "FakeSlackAdapter",
    "Notice",
    "Question",
    "SlackControlPlane",
    "SlackInputError",
    "SlackSettings",
]
