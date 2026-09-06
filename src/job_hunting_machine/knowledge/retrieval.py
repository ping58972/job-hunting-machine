"""Verified-only deterministic retrieval, with optional constrained ModelGateway reranking."""

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.repositories.knowledge import CandidateFactRepository
from job_hunting_machine.models.budgets import BudgetExceeded
from job_hunting_machine.models.gateway import ModelGateway, ModelGatewayError
from job_hunting_machine.models.schemas import StructuredOutput


class FactMatch(BaseModel):
    fact_id: str
    statement: str
    provenance: dict[str, Any]
    matched_terms: list[str]


class ProjectMatch(BaseModel):
    project_id: str
    score: int
    facts: list[FactMatch]


class Retrieval(BaseModel):
    facts: list[FactMatch]
    projects: list[ProjectMatch]


class Ranking(StructuredOutput):
    project_ids: list[str] = Field(max_length=20)


def terms(text: str) -> set[str]:
    if len(text) > 20_000:
        raise ValueError("requirements_too_large")
    normalized = text.casefold()
    for short, long in (
        ("ml", "machine learning"),
        ("ai", "artificial intelligence"),
        ("ros2", "ros 2"),
        ("pytorch", "torch"),
    ):
        normalized = re.sub(rf"\b{short}\b", long, normalized)
    stop = {
        "and",
        "or",
        "the",
        "a",
        "an",
        "with",
        "of",
        "in",
        "to",
        "for",
        "is",
        "we",
        "you",
        "experience",
        "required",
        "skills",
        "project",
        "repository",
        "source",
        "candidate",
        "contains",
    }
    return set(re.findall(r"[a-z0-9]+(?:\+\+|#)?", normalized)) - stop


def retrieve(database: Database, requirements: str, *, limit: int = 5) -> Retrieval:
    if not 1 <= limit <= 20:
        raise ValueError("invalid_retrieval_limit")
    query = terms(requirements)
    matches: list[FactMatch] = []
    grouped: dict[str, list[FactMatch]] = {}
    with database.transaction() as session:
        for fact in CandidateFactRepository(session).verified():
            value = json.loads(fact.value_json)
            matched = sorted(query & terms(value["statement"] + " " + (value.get("skill") or "")))
            if not matched:
                continue
            item = FactMatch(
                fact_id=fact.fact_id,
                statement=value["statement"],
                provenance=value["provenance"],
                matched_terms=matched,
            )
            matches.append(item)
            project_id = value["provenance"].get("project_id")
            if project_id:
                grouped.setdefault(project_id, []).append(item)
    projects = [
        ProjectMatch(
            project_id=key,
            score=len({term for f in facts for term in f.matched_terms}),
            facts=facts[:20],
        )
        for key, facts in grouped.items()
    ]
    projects.sort(key=lambda project: (-project.score, project.project_id))
    matches.sort(key=lambda fact: (-len(fact.matched_terms), fact.fact_id))
    return Retrieval(facts=matches[:100], projects=projects[:limit])


async def ranked_retrieval(
    database: Database,
    requirements: str,
    *,
    task_id: str,
    gateway: ModelGateway | None = None,
    limit: int = 5,
) -> Retrieval:
    result = retrieve(database, requirements, limit=limit)
    if gateway is None or len(result.projects) < 2:
        return result
    # Send only the already-filtered verified shortlist; never raw repository content or metrics.
    context = json.dumps(
        {
            "requirements": requirements[:4000],
            "projects": [
                {
                    "project_id": p.project_id,
                    "facts": [
                        {"fact_id": f.fact_id, "statement": f.statement} for f in p.facts[:5]
                    ],
                }
                for p in result.projects
            ],
        }
    )
    try:
        ranking = await gateway.structured(
            task_id=task_id,
            operation="project_matching",
            agent_name="project_retrieval",
            context=context,
            output_type=Ranking,
            prompt_name="project_ranking",
        )
    except (BudgetExceeded, ModelGatewayError):
        return retrieve(database, requirements, limit=limit)
    # Recheck verification after the model await: revocation must not race into a resume context.
    fresh = retrieve(database, requirements, limit=limit)
    choices = {p.project_id: p for p in fresh.projects}
    if len(ranking.project_ids) != len(choices) or set(ranking.project_ids) != set(choices):
        return fresh
    fresh.projects = [choices[identity] for identity in ranking.project_ids]
    return fresh
