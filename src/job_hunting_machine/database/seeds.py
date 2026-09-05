"""Versioned Architecture v2 policy DATA only; no qualification evaluation."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_hunting_machine.clock import Clock
from job_hunting_machine.database.models import (
    QualificationRule,
    QualificationRuleSet,
    SalaryLocationRule,
)
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import Repository, canonical_json
from job_hunting_machine.ids import IdGenerator

RULE_SET_NAME = "architecture-v2-qualification"
RULE_SET_VERSION = 1
ALLOWED_COUNTRIES = ["US", "CA", "GB", "AU", "NZ", "SG", "HK", "CN", "KR", "JP"]
RELEVANT_FIELDS = [
    "Software Engineering",
    "Machine Learning",
    "Artificial Intelligence",
    "Data Engineering",
    "Data Science",
    "Robotics",
    "Computer Vision",
    "NLP",
    "IT / Computing",
    "related technical areas",
]


@dataclass(frozen=True, slots=True)
class SeedResult:
    rule_sets_created: int
    qualification_rules_created: int
    salary_rules_created: int


@dataclass(frozen=True, slots=True)
class _Rule:
    key: str
    applies_to: str
    evaluator: str
    operator: str
    description: str
    expected: dict[str, object]


_RULES = (
    _Rule(
        "internship.period",
        "INTERNSHIP",
        "DETERMINISTIC",
        "INTERSECTS",
        "Internship work intersects January through August 2027.",
        {"start": "2027-01-01", "end": "2027-08-31", "inclusive": True},
    ),
    _Rule(
        "internship.country",
        "INTERNSHIP",
        "DETERMINISTIC",
        "EQUALS",
        "Internships must be in the United States.",
        {"country_code": "US"},
    ),
    _Rule(
        "internship.paid",
        "INTERNSHIP",
        "DETERMINISTIC",
        "EQUALS",
        "Internships must be paid.",
        {"paid": True},
    ),
    _Rule(
        "internship.cpt",
        "INTERNSHIP",
        "RESEARCH",
        "REQUIRE",
        "Internships must be CPT-compatible; unknown evidence requires review.",
        {"cpt_compatible": True, "unknown": "REVIEW"},
    ),
    _Rule(
        "internship.salary",
        "INTERNSHIP",
        "DETERMINISTIC",
        "LOCATION_MINIMUM",
        "Internship hourly salary meets the editable location threshold.",
        {"table": "salary_location_rules", "currency": "USD", "period": "HOUR"},
    ),
    _Rule(
        "relevant_field",
        "ALL",
        "SEMANTIC",
        "RELEVANT_TO",
        "Position is relevant to the architecture's technical occupational areas.",
        {"fields": RELEVANT_FIELDS, "unknown": "REVIEW"},
    ),
    _Rule(
        "application_open",
        "ALL",
        "DETERMINISTIC",
        "EQUALS",
        "Application remains open.",
        {"open": True, "unknown": "REVIEW"},
    ),
    _Rule(
        "new_grad.start_timing",
        "NEW_GRAD",
        "DETERMINISTIC",
        "COMPATIBLE_WITH",
        "Start timing is compatible with graduation after May 2027; policy reference only.",
        {"graduation_reference": "2027-05", "relationship": "after", "unknown": "REVIEW"},
    ),
    _Rule(
        "early_career.start_timing",
        "EARLY_CAREER",
        "DETERMINISTIC",
        "COMPATIBLE_WITH",
        "Start timing is compatible with graduation after May 2027; policy reference only.",
        {"graduation_reference": "2027-05", "relationship": "after", "unknown": "REVIEW"},
    ),
    _Rule(
        "new_grad.countries",
        "NEW_GRAD",
        "DETERMINISTIC",
        "IN",
        "New-graduate positions use the Architecture v2 country allowlist.",
        {"country_codes": ALLOWED_COUNTRIES},
    ),
    _Rule(
        "early_career.countries",
        "EARLY_CAREER",
        "DETERMINISTIC",
        "IN",
        "Early-career positions use the Architecture v2 country allowlist.",
        {"country_codes": ALLOWED_COUNTRIES},
    ),
    _Rule(
        "professional_experience",
        "ALL",
        "DETERMINISTIC",
        "LESS_THAN",
        "A normalized minimum of at least five professional years fails; unknown needs review.",
        {"minimum_years_exclusive_upper_bound": 5, "unknown": "REVIEW"},
    ),
    _Rule(
        "usa.full_time_authorization",
        "FULL_TIME",
        "RESEARCH",
        "REVIEW_POLICY",
        "Distinguish U.S. OPT and sponsorship evidence without inferring from absent language.",
        {
            "country_code": "US",
            "states": [
                "OPT_COMPATIBLE",
                "SPONSORSHIP_SUPPORTED",
                "SPONSORSHIP_NOT_SUPPORTED",
                "UNKNOWN",
            ],
            "absent_language": "UNKNOWN",
            "unknown": "REVIEW",
        },
    ),
)


def seed_policies(
    session: Session,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
) -> SeedResult:
    """Insert missing defaults and an audit in one savepoint; never overwrite edits.

    IDs for policy tables have no architecture-defined prefix: generate a plain
    ULID once, then find persisted rows by their policy identity on later runs.
    The caller owns commit/rollback. No candidate facts or operational tasks are seeded.
    """
    context = Repository(session, clock, ids)
    now = context.timestamp()
    with session.begin_nested():
        rule_set = session.scalar(
            select(QualificationRuleSet).where(
                QualificationRuleSet.name == RULE_SET_NAME,
                QualificationRuleSet.version == RULE_SET_VERSION,
            )
        )
        created_sets = int(rule_set is None)
        if rule_set is None:
            rule_set = QualificationRuleSet(
                rule_set_id=context.ids.generate_ulid(),
                name=RULE_SET_NAME,
                version=RULE_SET_VERSION,
                description="Architecture v2 section 11 policy data.",
                enabled=1,
                created_at=now,
                updated_at=now,
            )
            session.add(rule_set)
            session.flush()
        existing_keys = set(
            session.scalars(
                select(QualificationRule.rule_key).where(
                    QualificationRule.rule_set_id == rule_set.rule_set_id
                )
            )
        )
        created_rules = 0
        for priority, rule in enumerate(_RULES, start=1):
            if rule.key in existing_keys:
                continue
            session.add(
                QualificationRule(
                    rule_id=context.ids.generate_ulid(),
                    rule_set_id=rule_set.rule_set_id,
                    rule_key=rule.key,
                    description=rule.description,
                    applies_to=rule.applies_to,
                    evaluator_kind=rule.evaluator,
                    severity="HARD",
                    operator=rule.operator,
                    expected_value_json=canonical_json(rule.expected),
                    priority=priority * 10,
                    enabled=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            created_rules += 1
        created_salary = 0
        for state, city, tier, threshold in (
            (None, None, "STANDARD", 20.0),
            ("CA", "San Francisco", "VERY_HIGH", 35.0),
            ("NY", "New York City", "VERY_HIGH", 35.0),
        ):
            existing_salary = session.scalars(
                select(SalaryLocationRule).where(
                    SalaryLocationRule.country_code == "US",
                    SalaryLocationRule.state_region == state,
                    SalaryLocationRule.city == city,
                )
            ).one_or_none()
            if existing_salary is not None:
                continue
            session.add(
                SalaryLocationRule(
                    salary_rule_id=context.ids.generate_ulid(),
                    country_code="US",
                    state_region=state,
                    city=city,
                    cost_tier=tier,
                    internship_min_hourly_usd=threshold,
                    enabled=1,
                    notes="Architecture v2 section 10.3 internship salary policy.",
                    created_at=now,
                    updated_at=now,
                )
            )
            created_salary += 1
        session.flush()
        result = SeedResult(created_sets, created_rules, created_salary)
        if created_sets or created_rules or created_salary:
            ActivityLogRepository(session, context.clock, context.ids).append(
                ActivityEvent(
                    "POLICY_SEEDED",
                    actor_name="policy_seed",
                    metadata={
                        "rule_set_id": rule_set.rule_set_id,
                        "rule_sets_created": created_sets,
                        "qualification_rules_created": created_rules,
                        "salary_rules_created": created_salary,
                    },
                )
            )
    return result
