"""Explicit source-backed human verification of retained template content."""

import json

from sqlalchemy.orm import Session

from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.repositories.knowledge import CandidateFactRepository
from job_hunting_machine.knowledge.evidence import Provenance, store_evidence
from job_hunting_machine.resume.actions import ReviewRequired
from job_hunting_machine.resume.artifacts import encoded
from job_hunting_machine.resume.config import ResumePolicy
from job_hunting_machine.resume.documents import DocumentAdapter, identifier
from job_hunting_machine.resume.native import Json, normalize, signature
from job_hunting_machine.security.paths import PathGuard


def propose_template(database: Database, adapter: DocumentAdapter, policy: ResumePolicy) -> str:
    """Read and save evidence, then propose UNVERIFIED. This never grants verification."""
    policy.validate_paths()
    pointer = json.loads(PathGuard().validate_write(policy.template).read_text())
    source_id = identifier(pointer["doc_id"])
    document = adapter.get(source_id)
    path, sha = store_evidence(policy.output_root / "data/resume-templates", encoded(document))
    provenance = Provenance(
        source_type="GOOGLE_DOC",
        source_reference=source_id,
        path=path,
        sha256=sha,
        line_start=1,
        line_end=1,
        quote=json.dumps(source_id),
    )
    with database.transaction(immediate=True) as session:
        fact = CandidateFactRepository(session).add(
            fact_type="RESUME_TEMPLATE",
            fact_key=f"resume_template:{sha}",
            statement="The retained candidate claims in this resume template are accurate.",
            provenance=provenance,
        )
        return fact.fact_id


def validate_template(session: Session, document: Json, fact_id: str | None) -> None:
    facts = {f.fact_id: f for f in CandidateFactRepository(session).verified()}
    if fact_id not in facts or facts[fact_id].fact_type != "RESUME_TEMPLATE":
        raise ReviewRequired("retained_template_content_requires_verified_source_review")
    value = json.loads(facts[fact_id].value_json)
    evidence = Provenance.model_validate(value["provenance"])
    source = normalize(json.loads(PathGuard().validate_write(evidence.path).read_bytes()))
    if (
        evidence.source_type != "GOOGLE_DOC"
        or evidence.source_reference != document["documentId"]
        or signature(source, edited=True) != signature(document, edited=True)
    ):
        raise ReviewRequired("retained_template_differs_from_verified_source")
