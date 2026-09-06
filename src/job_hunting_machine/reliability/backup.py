"""SQLite-safe, root-confined local backups with hashed manifests."""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from job_hunting_machine.clock import Clock, SystemClock, format_utc
from job_hunting_machine.ids import IdGenerator
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


@dataclass(frozen=True, slots=True)
class BackupResult:
    directory: str
    manifest: str
    files: int


class BackupService:
    def __init__(
        self,
        *,
        root: Path = PROJECT_ROOT / "backups",
        clock: Clock | None = None,
    ) -> None:
        self.guard = PathGuard()
        self.root = self.guard.validate_write(root)
        self.clock = clock or SystemClock()

    @staticmethod
    def _sha(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _sqlite(self, source: Path, destination: Path) -> None:
        checked = self.guard.validate_write(source)
        if not checked.is_file():
            raise ValueError("backup_database_missing")
        self.guard.prepare_private_file(destination)
        source_uri = f"{checked.as_uri()}?mode=ro"
        with (
            sqlite3.connect(source_uri, uri=True, timeout=5) as origin,
            sqlite3.connect(destination) as target,
        ):
            origin.backup(target, pages=256)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise ValueError("backup_integrity_failed")
        os.chmod(destination, 0o600)

    def create(
        self,
        database_path: Path,
        checkpoint_path: Path | None = None,
    ) -> BackupResult:
        now = self.clock.now()
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        directory = self.guard.mkdir(
            self.root / f"{stamp}_{IdGenerator(self.clock).generate_ulid()}",
            parents=True,
            exist_ok=False,
        )
        entries: list[dict[str, object]] = []

        def add(source: Path, relative: Path, *, sqlite: bool = False) -> None:
            target = directory / relative
            self.guard.mkdir(target.parent, parents=True, exist_ok=True)
            if sqlite:
                self._sqlite(source, target)
            else:
                checked = self.guard.validate_write(source)
                if not checked.is_file():
                    raise ValueError("backup_metadata_missing")
                self.guard.write_bytes(target, checked.read_bytes())
            entries.append(
                {
                    "path": relative.as_posix(),
                    "sha256": self._sha(target),
                    "bytes": target.stat().st_size,
                }
            )

        add(database_path, Path("data/job-hunting.db"), sqlite=True)
        if checkpoint_path is not None and self.guard.validate_write(checkpoint_path).exists():
            add(checkpoint_path, Path("data/langgraph-checkpoints.db"), sqlite=True)
        metadata = (
            PROJECT_ROOT / "AGENTS.md",
            PROJECT_ROOT / "alembic.ini",
            PROJECT_ROOT / "pyproject.toml",
            PROJECT_ROOT / "uv.lock",
            PROJECT_ROOT / "docs/architecture-v2.md",
        )
        for source in metadata:
            add(source, source.relative_to(PROJECT_ROOT))
        for source in sorted((PROJECT_ROOT / "config").glob("*.yaml")):
            add(source, source.relative_to(PROJECT_ROOT))
        manifest = directory / "manifest.json"
        self.guard.write_text(
            manifest,
            json.dumps(
                {
                    "version": 1,
                    "created_at": format_utc(now),
                    "source_database": str(self.guard.validate_write(database_path)),
                    "retention_recommendation_days": 30,
                    "files": entries,
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
        return BackupResult(str(directory), str(manifest), len(entries))
