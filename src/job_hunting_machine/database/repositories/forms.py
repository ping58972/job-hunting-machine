"""Audited form information, per-application answers, and browser sessions."""

import json
import re
from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import select

from job_hunting_machine.database.models import BrowserSession, FormAnswer, FormInformation
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import (
    ReplayConflictError,
    Repository,
    required_text,
)
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

Sensitivity = Literal["NORMAL", "PERSONAL", "SENSITIVE"]
AutoFillPolicy = Literal["ALLOW", "ASK_IF_AMBIGUOUS", "MANUAL_ONLY"]
AnswerSource = Literal["CANONICAL_INFO", "CANDIDATE_FACT", "USER", "DERIVED", "FILE"]
AnswerStatus = Literal["EMPTY", "FILLED", "NEEDS_USER", "VALIDATED"]

_KEY = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class InformationUpsert:
    info_key: str
    category: str
    value: object
    value_type: str
    sensitivity: Sensitivity
    auto_fill_policy: AutoFillPolicy
    source: str
    verified: bool
    notes: str | None = None


class FormInformationRepository(Repository):
    def get(self, info_key: str) -> FormInformation | None:
        return self.session.get(FormInformation, info_key)

    def upsert(self, data: InformationUpsert) -> FormInformation:
        if _KEY.fullmatch(data.info_key) is None:
            raise ValueError("invalid_canonical_info_key")
        for value in (data.category, data.value_type, data.source):
            required_text(value)
        encoded = _json(data.value)
        row = self.get(data.info_key)
        now = self.timestamp()
        if row is None:
            row = FormInformation(
                info_key=data.info_key,
                category=data.category,
                value_json=encoded,
                value_type=data.value_type,
                sensitivity=data.sensitivity,
                auto_fill_policy=data.auto_fill_policy,
                source=data.source,
                verified=data.verified,
                notes=data.notes,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
            old = None
        else:
            old = "VERIFIED" if row.verified else "UNVERIFIED"
            row.category = data.category
            row.value_json = encoded
            row.value_type = data.value_type
            row.sensitivity = data.sensitivity
            row.auto_fill_policy = data.auto_fill_policy
            row.source = data.source
            row.verified = data.verified
            row.notes = data.notes
            row.updated_at = now
        self.session.flush()
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent(
                "canonical_form_information_saved",
                old_state=old,
                new_state="VERIFIED" if data.verified else "UNVERIFIED",
                metadata={"info_key": data.info_key},
            )
        )
        return row

    def usable(self) -> dict[str, FormInformation]:
        return {
            row.info_key: row
            for row in self.session.scalars(
                select(FormInformation).where(
                    FormInformation.verified == 1,
                    FormInformation.auto_fill_policy != "MANUAL_ONLY",
                    FormInformation.sensitivity != "SENSITIVE",
                )
            )
        }

    def save_authorized_user_answer(
        self, info_key: str, value: object, *, user_reference: str
    ) -> FormInformation:
        row = self.get(info_key)
        if row is None:
            raise ValueError("canonical_key_not_registered")
        if row.sensitivity != "NORMAL" or row.auto_fill_policy == "MANUAL_ONLY":
            raise ValueError("canonical_answer_persistence_not_allowed")
        return self.upsert(
            InformationUpsert(
                info_key=row.info_key,
                category=row.category,
                value=value,
                value_type=row.value_type,
                sensitivity="NORMAL",
                auto_fill_policy=cast(AutoFillPolicy, row.auto_fill_policy),
                source=user_reference,
                verified=True,
                notes=row.notes,
            )
        )


@dataclass(frozen=True, slots=True)
class AnswerUpsert:
    application_id: str
    page_key: str
    field_key: str
    question_text: str
    answer: object | None
    source_type: AnswerSource | None
    source_reference: str | None
    status: AnswerStatus
    confidence: float | None = None


class FormAnswerRepository(Repository):
    def get(self, application_id: str, page_key: str, field_key: str) -> FormAnswer | None:
        validate_id(application_id, IdKind.APPLICATION)
        return self.session.scalar(
            select(FormAnswer).where(
                FormAnswer.application_id == application_id,
                FormAnswer.page_key == page_key,
                FormAnswer.field_key == field_key,
            )
        )

    def upsert(self, data: AnswerUpsert) -> FormAnswer:
        validate_id(data.application_id, IdKind.APPLICATION)
        for value in (data.page_key, data.field_key, data.question_text):
            required_text(value)
        if data.confidence is not None and not 0 <= data.confidence <= 1:
            raise ValueError("invalid_answer_confidence")
        answer_json = _json(data.answer) if data.answer is not None else None
        row = self.get(data.application_id, data.page_key, data.field_key)
        now = self.timestamp()
        if row is None:
            row = FormAnswer(
                answer_id=self.ids.generate_ulid(),
                application_id=data.application_id,
                page_key=data.page_key,
                field_key=data.field_key,
                question_text=data.question_text,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
            old = None
        else:
            old = row.answer_status
            # A replay may repeat exact data but must not silently replace a validated answer.
            if row.answer_status == "VALIDATED" and (
                row.answer_json != answer_json
                or row.answer_source_type != data.source_type
                or row.answer_source_reference != data.source_reference
            ):
                raise ReplayConflictError("validated_form_answer_changed")
        row.answer_json = answer_json
        row.answer_source_type = data.source_type
        row.answer_source_reference = data.source_reference
        row.answer_status = data.status
        row.confidence = data.confidence
        row.last_verified_at = now if data.status == "VALIDATED" else None
        row.updated_at = now
        self.session.flush()
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent(
                "form_answer_saved",
                application_id=data.application_id,
                old_state=old,
                new_state=data.status,
                metadata={"answer_id": row.answer_id, "field_key": data.field_key},
            )
        )
        return row

    def for_application(self, application_id: str) -> list[FormAnswer]:
        validate_id(application_id, IdKind.APPLICATION)
        return list(
            self.session.scalars(
                select(FormAnswer)
                .where(FormAnswer.application_id == application_id)
                .order_by(FormAnswer.page_key, FormAnswer.field_key)
            )
        )


@dataclass(frozen=True, slots=True)
class SessionCheckpoint:
    application_id: str
    ats_type: str
    storage_state_path: str
    current_url: str
    current_page_key: str
    session_status: str = "ACTIVE"
    browser_session_id: str | None = None


class BrowserSessionRepository(Repository):
    def latest(self, application_id: str) -> BrowserSession | None:
        validate_id(application_id, IdKind.APPLICATION)
        return self.session.scalar(
            select(BrowserSession)
            .where(BrowserSession.application_id == application_id)
            .order_by(BrowserSession.updated_at.desc(), BrowserSession.browser_session_id.desc())
            .limit(1)
        )

    def active_for_domain(self, domain: str) -> BrowserSession | None:
        """Return the latest active owner for a normalized domain."""
        required_text(domain)
        return self.session.scalar(
            select(BrowserSession)
            .where(
                BrowserSession.session_status == "ACTIVE",
                BrowserSession.current_url.like(f"%://{domain}/%"),
            )
            .order_by(BrowserSession.updated_at.desc())
            .limit(1)
        )

    def checkpoint(self, data: SessionCheckpoint) -> BrowserSession:
        validate_id(data.application_id, IdKind.APPLICATION)
        path = PathGuard().validate_write(data.storage_state_path)
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        row = (
            self.session.get(BrowserSession, data.browser_session_id)
            if data.browser_session_id
            else self.latest(data.application_id)
        )
        now = self.timestamp()
        if row is None:
            row = BrowserSession(
                browser_session_id=self.ids.generate_ulid(),
                application_id=data.application_id,
                created_at=now,
            )
            self.session.add(row)
        elif row.application_id != data.application_id:
            raise ReplayConflictError("browser_session_application_mismatch")
        row.ats_type = data.ats_type
        row.storage_state_path = relative
        row.current_url = data.current_url
        row.current_page_key = data.current_page_key
        row.session_status = data.session_status
        row.last_checkpoint_at = now
        row.updated_at = now
        self.session.flush()
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent(
                "browser_session_checkpointed",
                application_id=data.application_id,
                new_state=data.session_status,
                metadata={"browser_session_id": row.browser_session_id},
            )
        )
        return row
