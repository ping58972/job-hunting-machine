"""Lease-fenced incremental scanning with durable, content-addressed manifests."""

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from job_hunting_machine.database.models import CandidateFact
from job_hunting_machine.database.repositories import TaskCreate, TaskRepository
from job_hunting_machine.database.repositories.activity import ActivityEvent, ActivityLogRepository
from job_hunting_machine.database.repositories.knowledge import (
    CandidateFactRepository,
    CatalogRepository,
)
from job_hunting_machine.knowledge.evidence import Provenance, store_evidence
from job_hunting_machine.knowledge.extract import LANGUAGES, observations, selected
from job_hunting_machine.knowledge.github import (
    FakeGitHub,
    GitHubReader,
    blob,
    head,
    inventory,
    repository_name,
    sha,
)
from job_hunting_machine.orchestration.checkpoints import run_blocking
from job_hunting_machine.orchestration.queue import Lease, QueueService, RetryableError
from job_hunting_machine.orchestration.worker import Worker
from job_hunting_machine.orchestration.workflows import fake_workflow
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


def read_manifest(path: str) -> dict[str, Any]:
    target = PathGuard().validate_write(path)
    if target.stat().st_size > 2_000_000:
        raise ValueError("catalog_manifest_too_large")
    data = target.read_bytes()
    if hashlib.sha256(data).hexdigest() != target.stem:
        raise ValueError("catalog_manifest_hash_mismatch")
    value: dict[str, Any] = json.loads(data)
    return value


class GitHubScanner:
    def __init__(
        self,
        queue: QueueService,
        client: GitHubReader | None = None,
        *,
        evidence_root: Path = PROJECT_ROOT / "evidence/projects",
    ) -> None:
        self.queue = queue
        self.client = client or FakeGitHub()
        self.root = PathGuard().validate_write(evidence_root)

    def _previous(self, repository: str) -> tuple[str | None, dict[str, Any]]:
        with self.queue.database.transaction() as session:
            project = CatalogRepository(session).project(repository)
            if project is None:
                return None, {}
            return project.source_commit_sha, read_manifest(
                project.evidence_path
            ) if project.evidence_path else {}

    async def scan(self, lease: Lease) -> dict[str, object]:
        repository = repository_name(str(lease.payload.get("repository", "")))
        memory = await run_blocking(self.queue.memory, lease.task_id)
        pointer = memory.get("github_snapshot")
        if isinstance(pointer, str):
            snapshot = await run_blocking(read_manifest, pointer)
            if snapshot["head"]["repository"] != repository:
                raise ValueError("scan_repository_mismatch")
        else:
            previous_sha, previous = await run_blocking(self._previous, repository)
            metadata = await head(self.client, repository)
            if previous.get("head") == metadata:
                with self.queue.fence(lease) as session:
                    project = CatalogRepository(session).project(repository)
                    if project is None or project.source_commit_sha != metadata["commit"]:
                        raise RetryableError("github_scan_concurrent_change")
                    project.last_scanned_at = CatalogRepository(
                        session, self.queue.clock
                    ).timestamp()
                    ActivityLogRepository(session, self.queue.clock).append(
                        ActivityEvent(
                            "github_scan_unchanged",
                            task_id=lease.task_id,
                            metadata={
                                "project_id": project.project_id,
                                "commit_sha": project.source_commit_sha,
                            },
                        )
                    )
                return {"changed": False, "repository": repository}
            tree = await self.client.get(
                f"/repos/{repository}/git/trees/{metadata['tree']}?recursive=1"
            )
            if tree.get("truncated") is not False or tree.get("sha") != metadata["tree"]:
                raise ValueError("github_tree_incomplete")
            entries = tree["tree"]
            if not isinstance(entries, list) or len(entries) > 100_000:
                raise ValueError("github_tree_invalid")
            eligible = [
                e
                for e in entries
                if e.get("type") == "blob" and selected(e["path"], e["mode"], e.get("size", 0))
            ]
            eligible.sort(
                key=lambda e: (
                    not PurePosixPath(e["path"]).name.lower().startswith("readme"),
                    e["path"],
                )
            )
            files: dict[str, Any] = {}
            old_files = previous.get("files", {})
            for entry in eligible[:40]:
                path, identity = entry["path"], sha(entry["sha"])
                old = old_files.get(path)
                if old and old["blob_sha"] == identity:
                    raw = PathGuard().validate_write(old["path"]).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != old["sha256"]:
                        raise ValueError("catalog_cached_evidence_corrupt")
                    files[path] = old
                else:
                    raw = await blob(self.client, repository, identity)
                    local, digest = await run_blocking(store_evidence, self.root / "blobs", raw)
                    files[path] = {"path": local, "sha256": digest, "blob_sha": identity}
            changes = {
                "added": sorted(set(files) - set(old_files)),
                "removed": sorted(set(old_files) - set(files)),
                "modified": sorted(
                    p
                    for p in set(files) & set(old_files)
                    if files[p]["blob_sha"] != old_files[p]["blob_sha"]
                ),
            }
            snapshot = {
                "version": 1,
                "base_commit": previous_sha,
                "head": metadata,
                "files": files,
                "changes": changes,
                "eligible_files": len(eligible),
                "scanned_files": len(files),
            }
            encoded = json.dumps(snapshot, sort_keys=True).encode()
            pointer, _ = await run_blocking(store_evidence, self.root / "manifests", encoded)
            memory["github_snapshot"] = pointer
            await run_blocking(self.queue.save_memory, lease, memory)
        assert isinstance(pointer, str)
        try:
            return await run_blocking(self._publish, lease, pointer, snapshot)
        except RetryableError:
            memory.pop("github_snapshot", None)
            await run_blocking(self.queue.save_memory, lease, memory)
            raise

    def _publish(
        self, lease: Lease, manifest_path: str, snapshot: dict[str, Any]
    ) -> dict[str, object]:
        metadata = snapshot["head"]
        with self.queue.fence(lease) as session:
            catalogs = CatalogRepository(session, self.queue.clock)
            project = catalogs.project(metadata["repository"])
            if project and project.evidence_path == manifest_path:
                return {"changed": False, "project_id": project.project_id}
            current = project.source_commit_sha if project else None
            if current != snapshot["base_commit"]:
                raise RetryableError("github_scan_concurrent_change")
            project = catalogs.ensure_project(metadata["repository"])
            # Old commits stay available for audit, but cannot silently retain verification.
            for fact in session.scalars(
                select(CandidateFact).where(CandidateFact.verification_status == "VERIFIED")
            ):
                provenance = json.loads(fact.value_json).get("provenance", {})
                if (
                    provenance.get("project_id") == project.project_id
                    and provenance.get("commit_sha") != metadata["commit"]
                ):
                    fact.verification_status, fact.updated_at = "UNVERIFIED", catalogs.timestamp()
                    ActivityLogRepository(session, self.queue.clock).append(
                        ActivityEvent(
                            "candidate_fact_source_changed",
                            old_state="VERIFIED",
                            new_state="UNVERIFIED",
                            task_id=lease.task_id,
                            metadata={"fact_id": fact.fact_id},
                        )
                    )
            project.description = metadata["description"]
            project.topics_json = json.dumps(metadata["topics"])
            project.source_commit_sha = metadata["commit"]
            project.evidence_path = manifest_path
            project.last_scanned_at = project.updated_at = catalogs.timestamp()
            project.languages_json = json.dumps(
                sorted(
                    {
                        LANGUAGES[PurePosixPath(p).suffix]
                        for p in snapshot["files"]
                        if PurePosixPath(p).suffix in LANGUAGES
                    }
                )
            )
            session.flush()
            facts = CandidateFactRepository(session, self.queue.clock)
            frameworks: set[str] = set()
            for source_path, evidence in snapshot["files"].items():
                raw = PathGuard().validate_write(evidence["path"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != evidence["sha256"]:
                    raise ValueError("catalog_evidence_hash_mismatch")
                for item in observations(source_path, raw.decode("utf-8")):
                    reference = (
                        f"https://github.com/{metadata['repository']}/blob/{metadata['commit']}/"
                        f"{quote(source_path, safe='/')}#L{item['line']}"
                    )
                    provenance = Provenance(
                        source_type="GITHUB",
                        source_reference=reference,
                        path=evidence["path"],
                        sha256=evidence["sha256"],
                        line_start=item["line"],
                        line_end=item["line"],
                        quote=item["quote"],
                        project_id=project.project_id,
                        commit_sha=metadata["commit"],
                        repository_path=source_path,
                    )
                    key = hashlib.sha256(
                        json.dumps(
                            [
                                project.project_id,
                                source_path,
                                item["line"],
                                item["skill"],
                                item["statement"],
                            ]
                        ).encode()
                    ).hexdigest()
                    facts.add(
                        fact_type="PROJECT_SKILL" if item["skill"] else "PROJECT_OBSERVATION",
                        fact_key=key,
                        statement=item["statement"],
                        provenance=provenance,
                        skill=item["skill"],
                    )
                    if item["skill"] and item["skill"] not in LANGUAGES.values():
                        frameworks.add(item["skill"])
            project.frameworks_json = json.dumps(sorted(frameworks))
            catalogs.refresh()
            ActivityLogRepository(session, self.queue.clock).append(
                ActivityEvent(
                    "github_scan_published",
                    task_id=lease.task_id,
                    metadata={
                        "project_id": project.project_id,
                        "commit_sha": metadata["commit"],
                        "changes": snapshot["changes"],
                        "manifest": manifest_path,
                    },
                )
            )
            return {
                "changed": True,
                "project_id": project.project_id,
                "changes": snapshot["changes"],
            }


class CatalogWorker(Worker):
    def __init__(
        self,
        queue: QueueService,
        client: GitHubReader | None = None,
        *,
        evidence_root: Path = PROJECT_ROOT / "evidence/projects",
    ) -> None:
        super().__init__(
            queue,
            {"SCAN_GITHUB": fake_workflow(), "GITHUB_INVENTORY": fake_workflow()},
            checkpoint_path=queue.database.path.parent / "langgraph-checkpoints.db",
        )
        self.scanner = GitHubScanner(queue, client, evidence_root=evidence_root)

    async def _execute(self, lease: Lease) -> None:
        if lease.task_type == "SCAN_GITHUB":
            summary = await self.scanner.scan(lease)
        else:
            repos = await inventory(self.scanner.client, str(lease.payload.get("owner", "")))
            with self.queue.fence(lease) as session:
                for repo in repos:
                    TaskRepository(session, self.queue.clock).create(
                        TaskCreate(
                            "SCAN_GITHUB",
                            task_status="READY",
                            dedupe_key=f"github_scan:{lease.task_id}:{repo}",
                            payload={"repository": repo},
                        )
                    )
            summary = {"repositories_enqueued": len(repos)}
        await run_blocking(self.queue.complete, lease, summary)
