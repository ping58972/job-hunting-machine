"""Audited candidate facts and derived project/skill catalogs. Caller owns commit."""

import hashlib
import json
import re
from typing import Literal

from sqlalchemy import select, update

from job_hunting_machine.database.models import CandidateFact, ProjectCatalog, SkillCatalog
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.base import (
    ConcurrentUpdateError,
    RecordNotFoundError,
    ReplayConflictError,
    Repository,
    required_text,
)
from job_hunting_machine.knowledge.evidence import Provenance
from job_hunting_machine.knowledge.extract import LANGUAGES, LIBRARIES

Verification = Literal["UNVERIFIED", "VERIFIED", "REJECTED"]


class CandidateFactRepository(Repository):
    def get(self, fact_id: str) -> CandidateFact:
        fact = self.session.get(CandidateFact, fact_id)
        if fact is None:
            raise RecordNotFoundError("candidate_fact_missing")
        return fact

    def add(
        self,
        *,
        fact_type: str,
        fact_key: str,
        statement: str,
        provenance: Provenance,
        skill: str | None = None,
    ) -> CandidateFact:
        """Record a proposed claim with exact evidence; never automatically verify it."""
        required_text(fact_type)
        required_text(fact_key)
        required_text(statement)
        if len(statement) > 4000:
            raise ValueError("candidate_fact_too_large")
        provenance.validate_content()
        if skill is not None:
            required_text(skill)
            if len(skill) > 80:
                raise ValueError("skill_name_too_large")
            names = {name.casefold(): name for name in (*LANGUAGES.values(), *LIBRARIES.values())}
            skill = names.get(skill.strip().casefold(), skill.strip().casefold())
        if provenance.project_id:
            project = self.session.get(ProjectCatalog, provenance.project_id)
            if project is None or not provenance.commit_sha:
                raise ValueError("project_provenance_missing")
        value = json.dumps(
            {"statement": statement, "skill": skill, "provenance": provenance.model_dump()},
            sort_keys=True,
        )
        existing = self.session.scalar(
            select(CandidateFact).where(
                CandidateFact.fact_key == fact_key,
                CandidateFact.source_reference == provenance.source_reference,
            )
        )
        if existing:
            if existing.value_json != value or existing.fact_type != fact_type:
                raise ReplayConflictError("candidate_fact_identity_conflict")
            return existing
        now = self.timestamp()
        fact = CandidateFact(
            fact_id=self.ids.generate_ulid(),
            fact_type=fact_type,
            fact_key=fact_key,
            value_json=value,
            source_type=provenance.source_type,
            source_reference=provenance.source_reference,
            evidence_path=provenance.path,
            verification_status="UNVERIFIED",
            created_at=now,
            updated_at=now,
        )
        self.session.add(fact)
        self.session.flush()
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent(
                "candidate_fact_recorded",
                new_state="UNVERIFIED",
                metadata={"fact_id": fact.fact_id, "evidence_sha256": provenance.sha256},
            )
        )
        return fact

    def current(self, fact: CandidateFact, *, check_evidence: bool = True) -> bool:
        try:
            provenance = Provenance.model_validate(json.loads(fact.value_json)["provenance"])
            if (
                fact.source_reference != provenance.source_reference
                or fact.source_type != provenance.source_type
                or fact.evidence_path != provenance.path
            ):
                return False
            if provenance.project_id:
                project = self.session.get(ProjectCatalog, provenance.project_id)
                if project is None or project.source_commit_sha != provenance.commit_sha:
                    return False
            if check_evidence:
                provenance.validate_content()
            return True
        except (ValueError, OSError, KeyError):
            return False

    def verified(self) -> list[CandidateFact]:
        return [
            fact
            for fact in self.session.scalars(
                select(CandidateFact)
                .where(CandidateFact.verification_status == "VERIFIED")
                .order_by(CandidateFact.fact_id)
            )
            if self.current(fact)
        ]

    def decide(
        self, fact_id: str, status: Verification, *, expected_status: Verification, reviewer: str
    ) -> CandidateFact:
        """Explicit local human review, protected against stale status/evidence."""
        if status not in {"VERIFIED", "UNVERIFIED", "REJECTED"} or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", reviewer
        ):
            raise ValueError("invalid_verification_decision")
        fact = self.get(fact_id)
        if fact.verification_status != expected_status:
            raise ConcurrentUpdateError("candidate_fact_verification_changed")
        if status == "VERIFIED" and not self.current(fact):
            raise ValueError("candidate_fact_source_not_current")
        if fact.verification_status == status:
            return fact
        old = fact.verification_status
        changed = self.session.scalar(
            update(CandidateFact)
            .where(
                CandidateFact.fact_id == fact_id,
                CandidateFact.verification_status == expected_status,
                CandidateFact.value_json == fact.value_json,
            )
            .values(verification_status=status, updated_at=self.timestamp())
            .returning(CandidateFact.fact_id)
            .execution_options(synchronize_session=False)
        )
        if changed is None:
            raise ConcurrentUpdateError("candidate_fact_verification_changed")
        self.session.refresh(fact)
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent(
                "candidate_fact_reviewed",
                actor_type="USER",
                actor_name=reviewer,
                old_state=old,
                new_state=status,
                metadata={
                    "fact_id": fact.fact_id,
                    "value_sha256": hashlib.sha256(fact.value_json.encode()).hexdigest(),
                },
            )
        )
        CatalogRepository(self.session, self.clock, self.ids).refresh()
        return fact


class CatalogRepository(Repository):
    def project(self, repository: str) -> ProjectCatalog | None:
        return self.session.scalars(
            select(ProjectCatalog).where(ProjectCatalog.repository_name == repository.lower())
        ).one_or_none()

    def ensure_project(self, repository: str) -> ProjectCatalog:
        existing = self.project(repository)
        if existing:
            return existing
        now = self.timestamp()
        project = ProjectCatalog(
            project_id=self.ids.generate_ulid(),
            name=repository.split("/")[-1],
            repository_name=repository.lower(),
            github_url=f"https://github.com/{repository.lower()}",
            verified_facts_json="[]",
            created_at=now,
            updated_at=now,
        )
        self.session.add(project)
        self.session.flush()
        ActivityLogRepository(self.session, self.clock, self.ids).append(
            ActivityEvent("project_catalog_created", metadata={"project_id": project.project_id})
        )
        return project

    def refresh(self) -> None:
        """Derived caches never grant verification; authoritative facts determine eligibility."""
        facts = list(self.session.scalars(select(CandidateFact)))
        fact_repo = CandidateFactRepository(self.session, self.clock, self.ids)
        verified_ids = {f.fact_id for f in fact_repo.verified()}
        project_facts: dict[str, list[str]] = {}
        skill_facts: dict[str, list[str]] = {}
        for fact in facts:
            value = json.loads(fact.value_json)
            provenance = value.get("provenance", {})
            if fact.fact_id in verified_ids and provenance.get("project_id"):
                project_facts.setdefault(provenance["project_id"], []).append(fact.fact_id)
            if value.get("skill") and fact_repo.current(fact):
                skill_facts.setdefault(value["skill"], []).append(fact.fact_id)
        for project in self.session.scalars(select(ProjectCatalog)):
            encoded = json.dumps(sorted(project_facts.get(project.project_id, [])))
            if project.verified_facts_json != encoded:
                project.verified_facts_json, project.updated_at = encoded, self.timestamp()
                ActivityLogRepository(self.session, self.clock, self.ids).append(
                    ActivityEvent(
                        "project_verified_facts_refreshed",
                        metadata={
                            "project_id": project.project_id,
                            "fact_ids": project_facts.get(project.project_id, []),
                        },
                    )
                )
        existing = {s.canonical_name: s for s in self.session.scalars(select(SkillCatalog))}
        for name in sorted(set(existing) | set(skill_facts)):
            ids = sorted(skill_facts.get(name, []))
            status = "VERIFIED" if verified_ids.intersection(ids) else "UNVERIFIED"
            evidence = json.dumps({"fact_ids": ids}, sort_keys=True)
            skill = existing.get(name)
            old = skill.verification_status if skill else None
            if skill is None:
                skill = SkillCatalog(
                    skill_id=self.ids.generate_ulid(),
                    canonical_name=name,
                    category="TECHNICAL",
                    evidence_json=evidence,
                    verification_status=status,
                    created_at=self.timestamp(),
                    updated_at=self.timestamp(),
                )
                self.session.add(skill)
            elif skill.evidence_json == evidence and old == status:
                continue
            skill.evidence_json, skill.verification_status, skill.updated_at = (
                evidence,
                status,
                self.timestamp(),
            )
            skill.last_verified_at = self.timestamp() if status == "VERIFIED" else None
            self.session.flush()
            ActivityLogRepository(self.session, self.clock, self.ids).append(
                ActivityEvent(
                    "skill_catalog_refreshed",
                    old_state=old,
                    new_state=status,
                    metadata={"skill_id": skill.skill_id, "fact_ids": ids},
                )
            )
        self.session.flush()

    def verified_skills(self) -> list[SkillCatalog]:
        verified_ids = {
            f.fact_id
            for f in CandidateFactRepository(self.session, self.clock, self.ids).verified()
        }
        return [
            s
            for s in self.session.scalars(
                select(SkillCatalog).where(SkillCatalog.verification_status == "VERIFIED")
            )
            if verified_ids.intersection(json.loads(s.evidence_json).get("fact_ids", []))
        ]
