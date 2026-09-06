"""Evidence-bound canonical form-field resolution."""

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_hunting_machine.browser.types import SemanticField
from job_hunting_machine.database.models import CandidateFact, FormInformation
from job_hunting_machine.models.gateway import EscalationReason, ModelGateway
from job_hunting_machine.models.router import Tier
from job_hunting_machine.models.schemas import StructuredOutput

ResolutionStatus = Literal["RESOLVED", "NEEDS_USER", "LOCAL_ONLY"]
ValueRecord = tuple[object, str, str, float, str | None]

_ALIASES = {
    "first name": "identity.first_name",
    "given name": "identity.first_name",
    "firstname": "identity.first_name",
    "last name": "identity.last_name",
    "family name": "identity.last_name",
    "lastname": "identity.last_name",
    "email": "contact.email",
    "email address": "contact.email",
    "phone": "contact.phone",
    "phone number": "contact.phone",
    "city": "address.city",
    "state": "address.state",
    "postal code": "address.postal_code",
    "zip code": "address.postal_code",
    "linkedin": "profile.linkedin_url",
    "linkedin profile": "profile.linkedin_url",
    "github": "profile.github_url",
    "github profile": "profile.github_url",
}
_SENSITIVE = re.compile(
    r"\b(password|secret|social security|ssn|race|ethnic|gender|disability|veteran|"
    r"date of birth)\b",
    re.I,
)


def normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


class ModelFieldResolution(StructuredOutput):
    model_config = ConfigDict(extra="forbid", strict=True)

    canonical_key: str | None
    needs_user: bool


@dataclass(frozen=True, slots=True)
class ResolvedField:
    field: SemanticField
    status: ResolutionStatus
    value: object | None = None
    source_type: str | None = None
    source_reference: str | None = None
    canonical_key: str | None = None
    confidence: float | None = None
    reason: str | None = None


class CanonicalFieldResolver:
    def __init__(self, gateway: ModelGateway | None = None) -> None:
        self.gateway = gateway

    @staticmethod
    def _candidate(field: SemanticField) -> str | None:
        candidates = [field.label, field.name or "", field.autocomplete or "", field.field_key]
        for raw in candidates:
            key = normalize(raw)
            if key in _ALIASES:
                return _ALIASES[key]
        return None

    @staticmethod
    def values(session: Session) -> dict[str, ValueRecord]:
        values: dict[str, ValueRecord] = {}
        for row in session.scalars(
            select(FormInformation).where(
                FormInformation.verified == 1,
                FormInformation.auto_fill_policy != "MANUAL_ONLY",
                FormInformation.sensitivity != "SENSITIVE",
            )
        ):
            values[row.info_key] = (
                json.loads(row.value_json),
                "CANONICAL_INFO",
                row.info_key,
                1.0,
                row.auto_fill_policy,
            )
        for fact in session.scalars(
            select(CandidateFact).where(CandidateFact.verification_status == "VERIFIED")
        ):
            values.setdefault(
                fact.fact_key,
                (json.loads(fact.value_json), "CANDIDATE_FACT", fact.fact_id, 1.0, None),
            )
        return values

    async def resolve_values(
        self,
        task_id: str,
        field: SemanticField,
        values: dict[str, ValueRecord],
    ) -> ResolvedField:
        if _SENSITIVE.search(f"{field.label} {field.name or ''} {field.field_key}"):
            return ResolvedField(field, "LOCAL_ONLY", reason="sensitive_or_secret_field")
        key = self._candidate(field)
        if key and key in values:
            value, source_type, reference, confidence, policy = values[key]
            if policy == "ASK_IF_AMBIGUOUS" and len(field.options) > 1:
                return ResolvedField(field, "NEEDS_USER", canonical_key=key, reason="ambiguous")
            return ResolvedField(field, "RESOLVED", value, source_type, reference, key, confidence)
        if self.gateway and values:
            allowed = sorted(values)
            context = json.dumps(
                {
                    "field": {
                        "label": field.label,
                        "name": field.name,
                        "autocomplete": field.autocomplete,
                        "control": field.control,
                        "options": field.options,
                    },
                    "allowed_canonical_keys": allowed,
                },
                sort_keys=True,
            )

            def quality(result: ModelFieldResolution) -> EscalationReason | None:
                if result.canonical_key is not None and result.canonical_key not in values:
                    return EscalationReason.REQUIRED_FIELD_UNRESOLVED
                return None

            result = await self.gateway.structured(
                task_id=task_id,
                operation="unfamiliar_form_question",
                agent_name="FormAgent",
                context=context,
                output_type=ModelFieldResolution,
                ceiling=Tier.SOL,
                prompt_name="structured_response",
                quality_check=quality,
            )
            if not result.needs_user and result.canonical_key in values:
                value, source_type, reference, confidence, _ = values[result.canonical_key]
                return ResolvedField(
                    field,
                    "RESOLVED",
                    value,
                    source_type,
                    reference,
                    result.canonical_key,
                    confidence,
                )
        return ResolvedField(field, "NEEDS_USER", canonical_key=key, reason="unresolved")

    async def resolve(self, session: Session, task_id: str, field: SemanticField) -> ResolvedField:
        """Convenience API for callers that do not hold a write transaction across await."""
        return await self.resolve_values(task_id, field, self.values(session))
