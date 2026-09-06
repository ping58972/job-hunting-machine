"""Fail-closed health, database, artifact, and architecture invariant checks."""

import ast
import hashlib
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select, text

from job_hunting_machine.browser.types import BaseAdapter
from job_hunting_machine.config import RuntimeSettings
from job_hunting_machine.database.engine import Database
from job_hunting_machine.database.models import (
    Approval,
    Artifact,
    BrowserSession,
    CandidateFact,
    ExternalAction,
    ModelUsage,
)
from job_hunting_machine.reliability.evals import EvalDatasetError, evaluate_all
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

Level = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: Level
    detail: str


class Doctor:
    def __init__(self, database: Database, *, checkpoint_path: Path | None = None) -> None:
        self.database = database
        self.checkpoint_path = checkpoint_path or database.path.parent / "langgraph-checkpoints.db"
        self.guard = PathGuard()

    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def database_integrity(self) -> dict[str, object]:
        with self.database.engine.connect() as connection:
            integrity = connection.execute(text("PRAGMA integrity_check")).scalar_one()
            foreign_keys = list(connection.execute(text("PRAGMA foreign_key_check")))
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            journal = connection.execute(text("PRAGMA journal_mode")).scalar_one()
            busy = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
        return {
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_keys),
            "alembic_revision": revision,
            "journal_mode": str(journal).upper(),
            "busy_timeout_ms": busy,
            "ok": integrity == "ok"
            and not foreign_keys
            and revision == "0001_architecture_v2"
            and str(journal).casefold() == "wal"
            and int(busy) == 5000,
        }

    def _checkpoint(self) -> Check:
        path = self.guard.validate_write(self.checkpoint_path)
        if not path.exists():
            return Check("checkpoint_database", "WARN", "not_created_until_first_worker_start")
        try:
            with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error:
            return Check("checkpoint_database", "FAIL", "integrity_check_failed")
        return Check(
            "checkpoint_database",
            "PASS" if result and result[0] == "ok" else "FAIL",
            "integrity_ok" if result and result[0] == "ok" else "integrity_check_failed",
        )

    def _files_and_rows(self) -> list[Check]:
        checks: list[Check] = []
        with self.database.transaction() as session:
            conflicts = list(
                session.execute(
                    text(
                        "SELECT p.application_id FROM application_pipeline p "
                        "JOIN application_details d USING(application_id) "
                        "WHERE p.application_status <> d.application_status"
                    )
                )
            )
            checks.append(
                Check(
                    "application_status_consistency",
                    "PASS" if not conflicts else "FAIL",
                    "consistent" if not conflicts else f"conflicts={len(conflicts)}",
                )
            )
            invalid_actions = list(
                session.scalars(
                    select(ExternalAction).where(
                        (ExternalAction.idempotency_key == "")
                        | (
                            ExternalAction.action_type.in_(
                                [
                                    "BROWSER_FILL",
                                    "BROWSER_UPLOAD",
                                    "SUBMIT_APPLICATION",
                                    "SEND_EMAIL",
                                ]
                            )
                            & ExternalAction.approval_id.is_(None)
                        )
                    )
                )
            )
            checks.append(
                Check(
                    "external_action_authorization",
                    "PASS" if not invalid_actions else "FAIL",
                    "idempotent_and_approval_bound"
                    if not invalid_actions
                    else f"invalid={len(invalid_actions)}",
                )
            )
            unbound_usage = list(
                session.scalars(select(ModelUsage).where(ModelUsage.task_id.is_(None)))
            )
            checks.append(
                Check(
                    "model_usage_task_binding",
                    "PASS" if not unbound_usage else "FAIL",
                    "all_bound" if not unbound_usage else f"unbound={len(unbound_usage)}",
                )
            )
            artifact_failures = 0
            for artifact in session.scalars(select(Artifact)):
                try:
                    path = self.guard.validate_write(PROJECT_ROOT / artifact.path)
                    if not path.is_file() or self._hash(path) != artifact.sha256:
                        artifact_failures += 1
                except (OSError, ValueError):
                    artifact_failures += 1
            checks.append(
                Check(
                    "artifact_integrity",
                    "PASS" if artifact_failures == 0 else "FAIL",
                    "all_hashes_match"
                    if artifact_failures == 0
                    else f"invalid={artifact_failures}",
                )
            )
            approval_failures = 0
            for approval in session.scalars(
                select(Approval).where(Approval.approval_status.in_(["PENDING", "APPROVED"]))
            ):
                try:
                    path = self.guard.validate_write(PROJECT_ROOT / approval.payload_path)
                    if not path.is_file() or self._hash(path) != approval.payload_sha256:
                        approval_failures += 1
                except (OSError, ValueError):
                    approval_failures += 1
            checks.append(
                Check(
                    "approval_payload_integrity",
                    "PASS" if approval_failures == 0 else "FAIL",
                    "all_hashes_match"
                    if approval_failures == 0
                    else f"invalid={approval_failures}",
                )
            )
            evidence_failures = 0
            for fact in session.scalars(
                select(CandidateFact).where(CandidateFact.verification_status == "VERIFIED")
            ):
                if not fact.evidence_path:
                    evidence_failures += 1
                    continue
                try:
                    if not self.guard.validate_write(fact.evidence_path).is_file():
                        evidence_failures += 1
                except ValueError:
                    evidence_failures += 1
            checks.append(
                Check(
                    "verified_fact_provenance",
                    "PASS" if evidence_failures == 0 else "FAIL",
                    "all_evidence_present"
                    if evidence_failures == 0
                    else f"invalid={evidence_failures}",
                )
            )
            session_failures = 0
            for browser in session.scalars(select(BrowserSession)):
                if browser.storage_state_path:
                    try:
                        path = self.guard.validate_write(PROJECT_ROOT / browser.storage_state_path)
                        if path.exists() and (path.stat().st_mode & 0o077):
                            session_failures += 1
                    except (OSError, ValueError):
                        session_failures += 1
            checks.append(
                Check(
                    "browser_session_confinement",
                    "PASS" if session_failures == 0 else "FAIL",
                    "root_confined_private"
                    if session_failures == 0
                    else f"invalid={session_failures}",
                )
            )
        return checks

    @staticmethod
    def _source_invariants() -> list[Check]:
        imports = []
        for path in (PROJECT_ROOT / "src/job_hunting_machine").rglob("*.py"):
            if path.name == "client.py" and path.parent.name == "models":
                continue
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(path))
            if any(
                (
                    isinstance(node, ast.Import)
                    and any(
                        alias.name == "openai" or alias.name.startswith("openai.")
                        for alias in node.names
                    )
                )
                or (
                    isinstance(node, ast.ImportFrom)
                    and node.module is not None
                    and (node.module == "openai" or node.module.startswith("openai."))
                )
                for node in ast.walk(tree)
            ):
                imports.append(path)
        adapter_methods = set(BaseAdapter.__dict__)
        browser_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PROJECT_ROOT / "src/job_hunting_machine/browser").glob("*.py")
        ).casefold()
        return [
            Check(
                "openai_gateway_boundary",
                "PASS" if not imports else "FAIL",
                "confined_to_models_client" if not imports else f"violations={len(imports)}",
            ),
            Check(
                "form_adapter_has_no_submit",
                "PASS" if "submit" not in adapter_methods else "FAIL",
                "submit_callable_absent"
                if "submit" not in adapter_methods
                else "submit_callable_present",
            ),
            Check(
                "linkedin_browser_automation_absent",
                "PASS" if "linkedin.com" not in browser_text else "FAIL",
                "absent" if "linkedin.com" not in browser_text else "browser_reference_present",
            ),
            Check(
                "dry_run_default",
                "PASS" if RuntimeSettings().runtime_mode is RuntimeMode.DRY_RUN else "FAIL",
                RuntimeSettings().runtime_mode.value,
            ),
        ]

    def run(self, configured_mode: RuntimeMode) -> dict[str, object]:
        checks: list[Check] = []
        try:
            integrity = self.database_integrity()
            checks.append(
                Check(
                    "application_database",
                    "PASS" if integrity["ok"] else "FAIL",
                    "integrity_schema_pragmas_ok"
                    if integrity["ok"]
                    else "integrity_schema_or_pragmas_failed",
                )
            )
        except Exception:
            checks.append(Check("application_database", "FAIL", "integrity_check_failed"))
            integrity = {"ok": False}
        checks.append(self._checkpoint())
        checks.extend(self._source_invariants())
        checks.extend(self._files_and_rows())
        try:
            evaluation = evaluate_all()
            checks.append(
                Check(
                    "evaluation_datasets",
                    "PASS" if evaluation["failed"] == 0 else "FAIL",
                    f"passed={evaluation['passed']};failed={evaluation['failed']}",
                )
            )
        except (EvalDatasetError, OSError, ValueError):
            evaluation = {"failed": 1}
            checks.append(Check("evaluation_datasets", "FAIL", "validation_failed"))
        failures = sum(item.level == "FAIL" for item in checks)
        warnings = sum(item.level == "WARN" for item in checks)
        # Local static checks cannot validate real credentials/provider behavior without an
        # explicitly authorized live exercise. Keep the answer fail-closed.
        safe_to_enable_live = False
        return {
            "architecture_ready": failures == 0,
            "safe_to_enable_live": safe_to_enable_live,
            "live_readiness_reason": "live_provider_and_credential_validation_not_performed",
            "configured_runtime_mode": configured_mode.value,
            "failures": failures,
            "warnings": warnings,
            "checks": [asdict(item) for item in checks],
            "database": integrity,
            "evaluations": evaluation,
        }
