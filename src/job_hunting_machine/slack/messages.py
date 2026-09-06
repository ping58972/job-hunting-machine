"""Outbound content is generated from a closed set of static templates and identifiers."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.slack.config import SlackID


class Notice(StrEnum):
    SUCCEEDED = "task_succeeded"
    FAILED = "task_failed"
    STATUS_CHANGED = "application_status_changed"


class Question(StrEnum):
    AVAILABILITY = "availability"
    WORK_AUTHORIZATION = "work_authorization"
    MISSING_INFORMATION = "missing_information"


class OutboundMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["notice", "approval", "question"]
    channel: SlackID
    task_id: str
    application_id: str | None = None
    approval_id: str | None = None
    approval_type: (
        Literal["PREPARE_APPLICATION", "SUBMIT_APPLICATION", "SEND_EMAIL", "SEND_EXTERNAL_MESSAGE"]
        | None
    ) = None
    payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    notice: Notice | None = None
    question: Question | None = None
    interrupt_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")

    @model_validator(mode="after")
    def valid(self) -> "OutboundMessage":
        validate_id(self.task_id, IdKind.TASK)
        if self.application_id:
            validate_id(self.application_id, IdKind.APPLICATION)
        if self.approval_id:
            validate_id(self.approval_id, IdKind.APPROVAL)
        if self.kind == "approval" and not (
            self.approval_id and self.application_id and self.payload_sha256
        ):
            raise ValueError("Approval correlation is required")
        if self.kind == "question" and not (self.question and self.interrupt_id):
            raise ValueError("Question correlation is required")
        if self.kind == "notice" and not self.notice:
            raise ValueError("Notice type is required")
        return self

    def render(self, action_id: str) -> dict[str, Any]:
        heading = {
            "notice": "Task update",
            "approval": (
                "Email outreach ready for review"
                if self.approval_type in {"SEND_EMAIL", "SEND_EXTERNAL_MESSAGE"}
                else (
                    "Application ready for submission review"
                    if self.approval_type == "SUBMIT_APPLICATION"
                    else "Application preparation approval requested"
                )
            ),
            "question": "Missing information requested",
        }[self.kind]
        text = f"{heading}\nTask: {self.task_id}"
        if self.application_id:
            text += f"\nApplication: {self.application_id}"
        if self.notice:
            text += f"\nStatus: {self.notice.value}"
        if self.payload_sha256:
            text += f"\nReview SHA-256: {self.payload_sha256}"
            text += (
                "\nReview the matching payload locally. "
                "Buttons record a decision only; they do not submit."
            )
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "plain_text", "text": text}}
        ]
        if self.kind == "approval":
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": f"jhm_{decision}",
                            "value": action_id,
                            "text": {"type": "plain_text", "text": label},
                        }
                        for decision, label in (
                            (
                                "approve",
                                "Approve submission"
                                if self.approval_type == "SUBMIT_APPLICATION"
                                else (
                                    "Approve email"
                                    if self.approval_type in {"SEND_EMAIL", "SEND_EXTERNAL_MESSAGE"}
                                    else "Approve preparation"
                                ),
                            ),
                            ("reject", "Reject"),
                        )
                    ],
                }
            )
        if self.kind == "question":
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": f"Requested field: {self.question.value if self.question else ''}. "
                        "Provide only the missing non-secret information. "
                        "Never send passwords, tokens, or cookies. "
                        "Use the local workflow for sensitive information.",
                    },
                }
            )
            blocks.append(
                {
                    "type": "input",
                    "block_id": "jhm_answer_block",
                    "label": {"type": "plain_text", "text": "Answer"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "jhm_answer_input",
                        "max_length": 2000,
                    },
                }
            )
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": "jhm_answer",
                            "value": action_id,
                            "text": {"type": "plain_text", "text": "Record answer"},
                        }
                    ],
                }
            )
        return {
            "channel": self.channel,
            "text": text,
            "blocks": blocks,
            "unfurl_links": False,
            "unfurl_media": False,
        }
