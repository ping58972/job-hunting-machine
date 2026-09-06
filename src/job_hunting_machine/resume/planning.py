"""Constrained model selection: prose is rendered from exact verified facts, never invented."""

import json
from typing import Any

from pydantic import Field
from sqlalchemy.orm import Session

from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.repositories.knowledge import CandidateFactRepository
from job_hunting_machine.knowledge.retrieval import retrieve
from job_hunting_machine.models.schemas import StructuredOutput
from job_hunting_machine.resume.errors import ReviewRequired


class Selection(StructuredOutput):
    project_fact_ids: list[str] = Field(min_length=1, max_length=20)
    skill_fact_ids: list[str] = Field(min_length=1, max_length=30)


def candidate_pool(database: Database, requirements: str) -> dict[str, Any]:
    shortlist = retrieve(database, requirements)
    project_ids = {p.project_id for p in shortlist.projects}
    matches = {f.fact_id for f in shortlist.facts}
    with database.transaction() as session:
        result = {
            fact.fact_id: json.loads(fact.value_json)
            for fact in CandidateFactRepository(session).verified()
            if fact.fact_id in matches
        }
    return {
        key: value
        for key, value in result.items()
        if value.get("skill") or value["provenance"].get("project_id") in project_ids
    }


def validate_selection(selection: Selection, pool: dict[str, Any]) -> None:
    for values, skill in ((selection.project_fact_ids, False), (selection.skill_fact_ids, True)):
        if len(set(values)) != len(values):
            raise ReviewRequired("duplicate_selected_fact")
        for key in values:
            if key not in pool:
                raise ReviewRequired("model_selected_unknown_fact")
            value = pool[key]
            if skill and not value.get("skill"):
                raise ReviewRequired("selected_fact_is_not_verified_skill")
            if not skill and (not value["provenance"].get("project_id") or value.get("skill")):
                raise ReviewRequired("selected_fact_is_not_verified_project")


def revalidate(session: Session, selection: Selection, pool: dict[str, Any]) -> None:
    validate_selection(selection, pool)
    current = {
        f.fact_id: json.loads(f.value_json) for f in CandidateFactRepository(session).verified()
    }
    if any(
        current.get(key) != pool[key]
        for key in selection.project_fact_ids + selection.skill_fact_ids
    ):
        raise ReviewRequired("selected_evidence_revoked_changed_or_stale")


def render(selection: Selection, pool: dict[str, Any]) -> tuple[list[str], list[str]]:
    validate_selection(selection, pool)
    projects = [str(pool[key]["statement"]).strip() for key in selection.project_fact_ids]
    skills = [str(pool[key]["skill"]).strip() for key in selection.skill_fact_ids]
    if any(not value or any(ord(c) < 32 for c in value) for value in projects + skills):
        raise ReviewRequired("selected_fact_requires_editorial_review")
    return projects, list(dict.fromkeys(skills))


def compress(selection: Selection) -> Selection | None:
    """Drop the lowest-ranked claim. Never truncate a metric/qualifier or change style."""
    if len(selection.project_fact_ids) > 1:
        return selection.model_copy(update={"project_fact_ids": selection.project_fact_ids[:-1]})
    if len(selection.skill_fact_ids) > 1:
        return selection.model_copy(update={"skill_fact_ids": selection.skill_fact_ids[:-1]})
    return None
