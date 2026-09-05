"""Constrain workflow writes to the fixed Architecture v2 project root.

``validate_write`` is a preflight check, not a write capability: callers must
use the guarded helpers when creating local files. The helpers traverse directories
with ``dir_fd`` and ``O_NOFOLLOW`` and replace files atomically, so neither symlink
substitution nor an existing hard link can redirect a file-content write.

The project root and its ancestor directories must remain trusted. Portable
userspace code cannot protect against a privileged mount change or an adversary
moving an already-open project directory outside the root during an operation.
Third-party writers will need equivalent confinement when introduced later.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path("/Users/ping58972/Documents/job-hunting-machine")


class PathGuardError(ValueError):
    """A path or filesystem operation cannot satisfy the write boundary."""


@dataclass(frozen=True, slots=True)
class PathGuard:
    """An immutable write boundary that may narrow, but never widen, PROJECT_ROOT.

    A narrower existing directory is useful for isolated tests. Relative write
    targets are always interpreted against ``root``, independent of the process
    working directory. All symlinks are rejected, including internal symlinks.
    """

    root: Path = PROJECT_ROOT

    def __post_init__(self) -> None:
        root = Path(self.root)
        self._reject_traversal(root)
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        if not root.is_relative_to(PROJECT_ROOT):
            raise PathGuardError("The write root must remain inside PROJECT_ROOT")
        self._inspect(root)
        if not root.is_dir():
            raise PathGuardError("The write root must be an existing directory")
        if not root.resolve().is_relative_to(PROJECT_ROOT):
            raise PathGuardError("The resolved write root escapes PROJECT_ROOT")
        object.__setattr__(self, "root", root)

    @staticmethod
    def _reject_traversal(path: Path) -> None:
        if ".." in path.parts:
            raise PathGuardError("Parent traversal is forbidden in write paths")

    @staticmethod
    def _inspect(path: Path) -> None:
        """Reject links and unsafe existing components without following links."""
        current = Path(path.anchor)
        for index, component in enumerate(path.parts[1:], start=1):
            current /= component
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                # Missing descendants have no filesystem entries to inspect yet.
                break
            except OSError as exc:
                raise PathGuardError(f"Cannot safely inspect write path: {path}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise PathGuardError(f"Symbolic links are forbidden in write paths: {current}")
            if index < len(path.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise PathGuardError(f"A write-path ancestor is not a directory: {current}")
            if not stat.S_ISDIR(metadata.st_mode):
                if not stat.S_ISREG(metadata.st_mode):
                    raise PathGuardError(f"Special files are forbidden write targets: {current}")
                if metadata.st_nlink != 1:
                    raise PathGuardError(
                        f"Hard-linked files are forbidden write targets: {current}"
                    )

    def validate_write(self, path: str | Path) -> Path:
        """Return an absolute permitted path; reject traversal and link escapes.

        This does not reserve the path. Use ``write_text``, ``write_bytes`` or
        ``mkdir`` for a write protected against subsequent symlink substitution.
        """
        candidate = Path(path)
        self._reject_traversal(candidate)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        if not candidate.is_relative_to(self.root):
            raise PathGuardError(f"Write path escapes the configured root: {candidate}")
        self._inspect(candidate)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PathGuardError(f"Cannot safely resolve write path: {candidate}") from exc
        if not resolved.is_relative_to(self.root):
            raise PathGuardError(f"Resolved write path escapes the configured root: {candidate}")
        return candidate

    @staticmethod
    def _require_descriptor_support() -> None:
        required = {os.open, os.mkdir, os.stat, os.rename, os.unlink}
        if (
            os.name != "posix"
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or not required.issubset(os.supports_dir_fd)
            or os.stat not in os.supports_follow_symlinks
        ):
            raise PathGuardError("Guarded writes require POSIX no-follow directory descriptors")

    @contextmanager
    def _parent_descriptor(self, target: Path, *, parents: bool = False) -> Iterator[int]:
        self._require_descriptor_support()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.root.anchor, flags)
        try:
            # Open every root component separately: O_NOFOLLOW applies only to
            # the final component of each individual open operation.
            for component in self.root.parts[1:]:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            for component in target.relative_to(self.root).parts[:-1]:
                if parents:
                    with suppress(FileExistsError):
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            yield descriptor
        except OSError as exc:
            raise PathGuardError(f"Guarded write failed for: {target}") from exc
        finally:
            os.close(descriptor)

    @staticmethod
    def _check_file_destination(name: str, descriptor: int) -> None:
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PathGuardError("File destination must be a regular, non-hard-linked file")

    def write_bytes(self, path: str | Path, data: bytes) -> Path:
        """Atomically create/replace a private file; its parent must already exist.

        New files have at most 0600 permissions. Replacement creates a new inode
        and does not modify the old inode, including any late-created hard links.
        """
        target = self.validate_write(path)
        if target == self.root:
            raise PathGuardError("The write root cannot be replaced with a file")
        with self._parent_descriptor(target) as descriptor:
            self._check_file_destination(target.name, descriptor)
            temporary = f".jhm-write-{secrets.token_hex(16)}"
            temporary_descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            try:
                with os.fdopen(temporary_descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._check_file_destination(target.name, descriptor)
                os.replace(
                    temporary,
                    target.name,
                    src_dir_fd=descriptor,
                    dst_dir_fd=descriptor,
                )
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=descriptor)
        return target

    def write_text(self, path: str | Path, data: str, *, encoding: str = "utf-8") -> Path:
        """Encode text and atomically write it through the shared path guard."""
        return self.write_bytes(path, data.encode(encoding))

    def mkdir(self, path: str | Path, *, parents: bool = False, exist_ok: bool = False) -> Path:
        """Create guarded directories with at most 0700 permissions."""
        target = self.validate_write(path)
        if target == self.root:
            if not exist_ok:
                raise PathGuardError("The write root already exists")
            return target
        with self._parent_descriptor(target, parents=parents) as descriptor:
            try:
                os.mkdir(target.name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                if not exist_ok:
                    raise
                metadata = os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise PathGuardError(
                        "Existing mkdir destination is not a real directory"
                    ) from None
        return target
