"""Versioned, network-free evaluation dataset loading and deterministic scoring."""

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from job_hunting_machine.agents.extraction import JobFacts, extract
from job_hunting_machine.agents.qualification import evaluate
from job_hunting_machine.knowledge.retrieval import terms
from job_hunting_machine.monitor.classification import KeywordStatusClassifier
from job_hunting_machine.monitor.types import GmailMessage
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

Suite = Literal["qualification", "job_extraction", "status_classification", "project_ranking"]


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    suite: Suite
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    input: dict[str, Any]
    expected: dict[str, Any]
    tags: tuple[str, ...] = ()


class EvalDatasetError(ValueError):
    pass


def load_dataset(path: Path) -> tuple[EvalCase, ...]:
    checked = PathGuard().validate_write(path)
    cases: list[EvalCase] = []
    try:
        for line in checked.read_text(encoding="utf-8").splitlines():
            if line.strip():
                cases.append(EvalCase.model_validate_json(line))
    except (OSError, ValueError) as error:
        raise EvalDatasetError("evaluation_dataset_invalid") from error
    if not cases or len({item.case_id for item in cases}) != len(cases):
        raise EvalDatasetError("evaluation_dataset_empty_or_duplicate")
    return tuple(cases)


def _subset(actual: Mapping[str, object], expected: Mapping[str, object]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _qualification_policy() -> dict[str, object]:
    return {
        "rules": [
            {
                "id": "eval.country",
                "key": "internship.country",
                "applies": "INTERNSHIP",
                "operator": "EQUALS",
                "expected": {"country_code": "US"},
            },
            {
                "id": "eval.paid",
                "key": "internship.paid",
                "applies": "INTERNSHIP",
                "operator": "EQUALS",
                "expected": {"paid": True},
            },
            {
                "id": "eval.salary",
                "key": "internship.salary",
                "applies": "INTERNSHIP",
                "operator": "LOCATION_MINIMUM",
                "expected": {"currency": "USD", "period": "HOUR"},
            },
            {
                "id": "eval.open",
                "key": "application_open",
                "applies": "ALL",
                "operator": "EQUALS",
                "expected": {"open": True},
            },
            {
                "id": "eval.authorization",
                "key": "usa.full_time_authorization",
                "applies": "FULL_TIME",
                "operator": "REVIEW_POLICY",
                "expected": {"country_code": "US"},
            },
        ],
        "salaries": [
            {
                "id": "eval.us.minimum",
                "country": "US",
                "state": None,
                "city": None,
                "minimum": 20.0,
            }
        ],
    }


def _project_order(value: Mapping[str, Any]) -> list[str]:
    query = terms(str(value.get("requirements", "")))
    ranked: list[tuple[int, str]] = []
    projects = value.get("projects")
    if not isinstance(projects, list):
        return []
    for project in projects:
        if not isinstance(project, dict) or project.get("verified") is not True:
            continue
        identity, facts = project.get("project_id"), project.get("facts")
        if not isinstance(identity, str) or not isinstance(facts, list):
            continue
        matched: set[str] = set()
        for fact in facts:
            if isinstance(fact, str):
                matched.update(query & terms(fact))
        if matched:
            ranked.append((len(matched), identity))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [identity for _, identity in ranked]


def evaluate_all(root: Path = PROJECT_ROOT / "evals/datasets") -> dict[str, object]:
    paths = sorted(PathGuard().validate_write(root).glob("*.jsonl"))
    counts: dict[str, int] = {}
    scored = passed = 0
    hashes: dict[str, str] = {}
    classifier = KeywordStatusClassifier()
    for path in paths:
        cases = load_dataset(path)
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for case in cases:
            counts[case.suite] = counts.get(case.suite, 0) + 1
            if case.suite == "job_extraction":
                facts = extract(str(case.input["html"]), int(case.input.get("status", 200)))
                actual = {
                    "company_name": facts.company_name,
                    "job_title": facts.job_title,
                    "paid": facts.paid,
                    "application_open": facts.application_open,
                }
                ok = _subset(actual, case.expected)
            elif case.suite == "status_classification":
                result = asyncio.run(
                    classifier.classify_email(
                        "TASK_00000000000000000000000000",
                        "APP_00000000000000000000000000",
                        GmailMessage(
                            case.case_id,
                            "fixture@example.test",
                            str(case.input.get("subject", "")),
                            str(case.input.get("body", "")),
                            "2026-01-01T00:00:00Z",
                        ),
                    )
                )
                actual = {
                    "detected_status": result.detected_status.value
                    if result.detected_status
                    else None
                }
                ok = _subset(actual, case.expected)
            elif case.suite == "qualification":
                facts = JobFacts.model_validate(case.input)
                decision = evaluate(
                    facts,
                    _qualification_policy(),
                    "2026-09-06T12:00:00Z",
                )
                ok = decision.outcome == case.expected.get("decision")
            else:
                ok = _project_order(case.input) == case.expected.get("ordered_project_ids")
            scored += 1
            passed += int(ok)
    required = {"qualification", "job_extraction", "status_classification", "project_ranking"}
    if set(counts) != required:
        raise EvalDatasetError("evaluation_suite_missing")
    return {
        "datasets": len(paths),
        "cases": scored,
        "passed": passed,
        "failed": scored - passed,
        "by_suite": dict(sorted(counts.items())),
        "sha256": hashes,
    }
