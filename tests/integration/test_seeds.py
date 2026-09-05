"""The seed is complete, repeatable policy data and never a candidate profile."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from job_hunting_machine.clock import FrozenClock
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    QualificationRule,
    QualificationRuleSet,
    SalaryLocationRule,
)
from job_hunting_machine.database.seeds import ALLOWED_COUNTRIES, seed_policies


def test_seed_current_policy_and_salary_preserves_edits_and_ids(tmp_path: Path) -> None:
    database = Database(tmp_path / "seed.db")
    database.migrate()
    clock = FrozenClock(datetime(2026, 9, 5, tzinfo=UTC))
    try:
        with database.transaction() as session:
            result = seed_policies(session, clock)
            assert asdict(result) == {
                "rule_sets_created": 1,
                "qualification_rules_created": 13,
                "salary_rules_created": 3,
            }
            rules = {rule.rule_key: rule for rule in session.scalars(select(QualificationRule))}
            assert json.loads(rules["internship.period"].expected_value_json or "null") == {
                "start": "2027-01-01",
                "end": "2027-08-31",
                "inclusive": True,
            }
            assert (
                json.loads(rules["new_grad.countries"].expected_value_json or "null")[
                    "country_codes"
                ]
                == ALLOWED_COUNTRIES
            )
            assert set(ALLOWED_COUNTRIES) == {
                "US",
                "CA",
                "GB",
                "AU",
                "NZ",
                "SG",
                "HK",
                "CN",
                "KR",
                "JP",
            }
            assert (
                json.loads(rules["professional_experience"].expected_value_json or "null")[
                    "minimum_years_exclusive_upper_bound"
                ]
                == 5
            )
            auth = json.loads(rules["usa.full_time_authorization"].expected_value_json or "null")
            assert auth["absent_language"] == auth["states"][-1] == "UNKNOWN"
            salaries = list(session.scalars(select(SalaryLocationRule)))
            assert {item.city: item.internship_min_hourly_usd for item in salaries} == {
                None: 20.0,
                "San Francisco": 35.0,
                "New York City": 35.0,
            }
            saved_ids = {rule.rule_key: rule.rule_id for rule in rules.values()}
            rules["internship.paid"].enabled = 0  # Synthetic policy edit in isolated test DB.
            salaries[0].internship_min_hourly_usd = 27.0
            salary_id = salaries[0].salary_rule_id
        with database.transaction() as session:
            assert all(value == 0 for value in asdict(seed_policies(session, clock)).values())
            assert {
                rule.rule_key: rule.rule_id for rule in session.scalars(select(QualificationRule))
            } == saved_ids
            disabled = session.scalar(
                select(QualificationRule).where(QualificationRule.rule_key == "internship.paid")
            )
            assert disabled is not None and disabled.enabled == 0
            edited = session.get(SalaryLocationRule, salary_id)
            assert edited is not None and edited.internship_min_hourly_usd == 27.0
            assert (
                session.execute(
                    text("SELECT COUNT(*) FROM activity_log WHERE event_type='POLICY_SEEDED'")
                ).scalar_one()
                == 1
            )
            for table in (
                "candidate_facts",
                "my_information_for_filling_form",
                "agent_queue",
                "application_pipeline",
            ):
                assert session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
    finally:
        database.dispose()


def test_seed_is_rolled_back_with_outer_transaction(tmp_path: Path) -> None:
    database = Database(tmp_path / "rollback.db")
    database.migrate()
    try:
        with pytest.raises(RuntimeError), database.transaction() as session:
            seed_policies(session)
            raise RuntimeError("injected after policy writes")
        with database.transaction() as session:
            assert list(session.scalars(select(QualificationRuleSet))) == []
            assert session.execute(text("SELECT COUNT(*) FROM activity_log")).scalar_one() == 0
    finally:
        database.dispose()
