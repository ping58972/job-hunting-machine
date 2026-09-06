"""Editable policy evaluation with deterministic rules before bounded semantic checks."""

import json
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_hunting_machine.agents.extraction import JobFacts
from job_hunting_machine.database.models import (
    QualificationRule,
    QualificationRuleSet,
    SalaryLocationRule,
)
from job_hunting_machine.models.budgets import BudgetExceeded
from job_hunting_machine.models.gateway import EscalationReason, ModelGateway, ModelGatewayError
from job_hunting_machine.models.schemas import StructuredOutput


class RuleResult(BaseModel):
    rule_id: str
    key: str
    result: Literal["PASS", "FAIL", "REVIEW"]
    explanation: str


class Decision(BaseModel):
    outcome: Literal["PASS", "FAIL", "REVIEW"]
    rules: list[RuleResult]


def policy_snapshot(session: Session) -> dict[str, Any]:
    sets = list(
        session.scalars(select(QualificationRuleSet).where(QualificationRuleSet.enabled == 1))
    )
    if len(sets) != 1:
        raise ValueError("qualification_requires_one_active_rule_set")
    rules = list(
        session.scalars(
            select(QualificationRule)
            .where(
                QualificationRule.rule_set_id == sets[0].rule_set_id, QualificationRule.enabled == 1
            )
            .order_by(QualificationRule.priority)
        )
    )
    salaries = list(
        session.scalars(select(SalaryLocationRule).where(SalaryLocationRule.enabled == 1))
    )
    return {
        "rule_set_id": sets[0].rule_set_id,
        "version": sets[0].version,
        "rules": [
            {
                "id": r.rule_id,
                "key": r.rule_key,
                "applies": r.applies_to,
                "operator": r.operator,
                "expected": json.loads(r.expected_value_json or "{}"),
            }
            for r in rules
        ],
        "salaries": [
            {
                "id": s.salary_rule_id,
                "country": s.country_code,
                "state": s.state_region,
                "city": s.city,
                "minimum": s.internship_min_hourly_usd,
            }
            for s in salaries
        ],
    }


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _expired(value: str | None, now: datetime) -> bool | None:
    if value is None:
        return None
    if len(value) == 10:
        day = _day(value)
        return day < now.date() if day else None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant.astimezone(UTC) < now if instant.tzinfo else None
    except ValueError:
        return None


def evaluate(f: JobFacts, policy: dict[str, Any], at: str) -> Decision:
    """Pure evaluation: explicit facts, policy snapshot and UTC time only."""
    now = datetime.fromisoformat(at.replace("Z", "+00:00"))
    results: list[RuleResult] = []

    def add(identity: str, key: str, value: bool | None, reason: str) -> None:
        results.append(
            RuleResult(
                rule_id=identity,
                key=key,
                result="REVIEW" if value is None else "PASS" if value else "FAIL",
                explanation=reason,
            )
        )

    if f.employment_type == "UNKNOWN":
        add(
            "extraction.employment_type",
            "extraction.employment_type",
            None,
            "Employment category is unresolved.",
        )
    if not f.company_name or not f.job_title:
        add(
            "extraction.identity",
            "extraction.identity",
            None,
            "Job/company identity is incomplete.",
        )
    for issue in f.issues:
        add(
            f"extraction.{issue}",
            f"extraction.{issue}",
            None,
            "Evidence is incomplete or contradictory.",
        )
    for rule in policy["rules"]:
        key, expected = rule["key"], rule["expected"]
        applies = rule["applies"]
        if applies not in {"ALL", f.employment_type} and not (
            applies == "FULL_TIME" and f.employment_type in {"NEW_GRAD", "EARLY_CAREER"}
        ):
            continue
        expected_operators = {
            "internship.period": "INTERSECTS",
            "internship.country": "EQUALS",
            "internship.paid": "EQUALS",
            "internship.cpt": "REQUIRE",
            "internship.salary": "LOCATION_MINIMUM",
            "relevant_field": "RELEVANT_TO",
            "application_open": "EQUALS",
            "professional_experience": "LESS_THAN",
            "new_grad.start_timing": "COMPATIBLE_WITH",
            "early_career.start_timing": "COMPATIBLE_WITH",
            "new_grad.countries": "IN",
            "early_career.countries": "IN",
            "usa.full_time_authorization": "REVIEW_POLICY",
        }
        if rule["operator"] != expected_operators.get(key):
            add(rule["id"], key, None, "Rule operator requires an implemented evaluator.")
            continue
        value: bool | None = None
        reason = "Required evidence is unresolved."
        if key == "internship.period":
            start, end = _day(f.internship_start), _day(f.internship_end)
            lower, upper = _day(expected.get("start")), _day(expected.get("end"))
            if start and end and lower and upper and start <= end:
                value = start <= upper and end >= lower
            reason = "Work dates must intersect the configured internship window."
        elif key == "internship.country":
            value = f.country_code == expected.get("country_code") if f.country_code else None
            reason = "Internship country must match the configured country."
        elif key.endswith(".countries"):
            value = f.country_code in expected.get("country_codes", []) if f.country_code else None
            reason = "Country must be in the configured allowlist."
        elif key == "internship.paid":
            value = f.paid == expected.get("paid") if f.paid is not None else None
            reason = "Internship must satisfy the paid-work rule."
        elif key == "internship.cpt":
            value = None if f.cpt == "UNKNOWN" else f.cpt == "SUPPORTED"
            reason = "CPT compatibility requires explicit supporting evidence."
        elif key == "internship.salary":
            matches = [
                s
                for s in policy["salaries"]
                if s["country"] == f.country_code
                and (
                    s["state"] is None or s["state"].casefold() == (f.state_region or "").casefold()
                )
                and (s["city"] is None or s["city"].casefold() == (f.city or "").casefold())
            ]
            matches.sort(
                key=lambda s: (s["city"] is not None, s["state"] is not None), reverse=True
            )
            if (
                matches
                and f.city
                and f.salary_min is not None
                and f.salary_currency == expected.get("currency")
                and f.salary_period == expected.get("period")
            ):
                # Missing region must not silently select a cheaper country default.
                uncertain_location = any(
                    s["city"]
                    and s["city"].casefold() == f.city.casefold()
                    and s["state"]
                    and not f.state_region
                    for s in policy["salaries"]
                )
                value = None if uncertain_location else f.salary_min >= matches[0]["minimum"]
                reason = (
                    f"Hourly minimum {f.salary_min:g} USD compared with location threshold "
                    f"{matches[0]['minimum']:g} USD (salary rule {matches[0]['id']})."
                )
        elif key == "professional_experience":
            value = (
                f.required_experience_min < expected["minimum_years_exclusive_upper_bound"]
                if f.required_experience_min is not None
                else None
            )
            reason = "Compare the minimum required professional years, not the upper range."
        elif key == "application_open":
            expired = _expired(f.application_deadline, now)
            value = False if expired is True else f.application_open
            if f.application_deadline and expired is None and value is not False:
                value = None
            reason = "Application must be explicitly open and its deadline must not have passed."
        elif key.endswith(".start_timing"):
            start = _day(f.internship_start)
            reference = str(expected.get("graduation_reference", ""))
            value = start.strftime("%Y-%m") > reference if start else None
            reason = "Job start month must be after the policy graduation reference."
        elif key == "relevant_field":
            value = f.relevant
            # Exact configured occupational terms are sufficient; ambiguous language uses AI.
            if value is None and f.job_title:
                value = (
                    True
                    if any(
                        str(term).casefold() in f.job_title.casefold()
                        for term in expected.get("fields", [])
                        if term != "related technical areas"
                    )
                    else None
                )
            reason = "Role must match these configured areas: " + ", ".join(
                expected.get("fields", [])
            )
        elif key == "usa.full_time_authorization":
            if f.country_code != expected.get("country_code"):
                continue
            value = (
                None
                if f.authorization == "UNKNOWN"
                else f.authorization in {"OPT_COMPATIBLE", "SPONSORSHIP_SUPPORTED"}
            )
            reason = "Explicit authorization evidence is required; silence is unknown."
        add(rule["id"], key, value, reason)
    outcome: Literal["PASS", "FAIL", "REVIEW"] = (
        "FAIL"
        if any(r.result == "FAIL" for r in results)
        else "REVIEW"
        if not results or any(r.result == "REVIEW" for r in results)
        else "PASS"
    )
    return Decision(outcome=outcome, rules=results)


class SemanticFinding(StructuredOutput):
    field: Literal["relevant", "cpt", "authorization"]
    value: Literal["SUPPORTED", "NOT_SUPPORTED", "OPT_COMPATIBLE", "UNKNOWN"]
    quote: str
    confidence: float = Field(ge=0, le=1)


class SemanticOutput(StructuredOutput):
    findings: list[SemanticFinding]


def semantic_needed(decision: Decision, facts: JobFacts) -> bool:
    keys = {r.key for r in decision.rules if r.result == "REVIEW"}
    return (
        decision.outcome != "FAIL"
        and bool(keys & {"relevant_field", "internship.cpt", "usa.full_time_authorization"})
        and bool(facts.text)
    )


async def semantic_check(
    gateway: ModelGateway, task_id: str, facts: JobFacts, decision: Decision
) -> JobFacts:
    """Model interpretation never replaces a known deterministic fact or invents evidence."""
    keys = [r.key for r in decision.rules if r.result == "REVIEW"]
    context = json.dumps(
        {
            "title": facts.job_title,
            "unresolved_rules": [r.model_dump() for r in decision.rules if r.result == "REVIEW"],
            "evidence": facts.text[:7000],
        }
    )

    def threshold(item: SemanticFinding) -> float:
        name = (
            "qualification_auto_fail"
            if item.value == "NOT_SUPPORTED"
            else "qualification_auto_pass"
        )
        return gateway.registry.confidence.get(name, 1.0)

    def quality(output: SemanticOutput) -> EscalationReason | None:
        for item in output.findings:
            if item.value != "UNKNOWN" and (
                item.quote not in facts.text
                or not item.quote.strip()
                or item.confidence < threshold(item)
            ):
                return EscalationReason.LOW_CONFIDENCE
        return None

    try:
        output = await gateway.structured(
            task_id=task_id,
            operation="qualification",
            agent_name="qualification",
            context=context,
            output_type=SemanticOutput,
            prompt_name="qualification",
            quality_check=quality,
        )
    except (BudgetExceeded, ModelGatewayError):
        return facts
    updated = facts.model_copy(deep=True)
    updated.semantic_evidence = [item.model_dump() for item in output.findings]
    fields = [item.field for item in output.findings]
    for item in output.findings:
        if (
            fields.count(item.field) != 1
            or item.value == "UNKNOWN"
            or item.quote not in facts.text
            or not item.quote.strip()
            or item.confidence < threshold(item)
        ):
            continue
        if (
            item.field == "relevant"
            and "relevant_field" in keys
            and item.value in {"SUPPORTED", "NOT_SUPPORTED"}
        ):
            updated.relevant = item.value == "SUPPORTED"
        elif (
            item.field == "cpt"
            and updated.cpt == "UNKNOWN"
            and "cpt" in item.quote.lower()
            and item.value in {"SUPPORTED", "NOT_SUPPORTED"}
        ):
            updated.cpt = "SUPPORTED" if item.value == "SUPPORTED" else "NOT_SUPPORTED"
        elif (
            item.field == "authorization"
            and updated.authorization == "UNKNOWN"
            and any(word in item.quote.lower() for word in ("sponsor", "opt"))
        ):
            if item.value == "SUPPORTED":
                updated.authorization = "SPONSORSHIP_SUPPORTED"
            elif item.value == "NOT_SUPPORTED":
                updated.authorization = "SPONSORSHIP_NOT_SUPPORTED"
            elif item.value == "OPT_COMPATIBLE":
                updated.authorization = "OPT_COMPATIBLE"
    return updated
