"""Acceptance and attack-regression tests for the project write boundary."""

from __future__ import annotations

import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard, PathGuardError


@pytest.fixture
def guard(tmp_path: Path) -> PathGuard:
    # The test runner must keep even its test-only adversarial paths in the project.
    assert tmp_path.is_relative_to(PROJECT_ROOT)
    root = tmp_path / "guarded"
    root.mkdir()
    return PathGuard(root)


def test_allowed_path_succeeds(guard: PathGuard) -> None:
    directory = guard.mkdir("artifacts/nested", parents=True)
    target = guard.write_text("artifacts/nested/example.txt", "verified résumé\n")
    assert target == directory / "example.txt"
    assert target.read_text() == "verified résumé\n"
    assert guard.validate_write(target) == target


@pytest.mark.parametrize("path", ["../escaped.txt", "nested/../inside.txt", "a/../../outside"])
def test_parent_traversal_rejected(guard: PathGuard, path: str) -> None:
    with pytest.raises(PathGuardError, match="Parent traversal"):
        guard.write_text(path, "forbidden")


def test_external_absolute_path_rejected(guard: PathGuard) -> None:
    with pytest.raises(PathGuardError, match="escapes"):
        guard.write_text(PROJECT_ROOT.parent / "jhm-forbidden.txt", "forbidden")


def test_sibling_prefix_is_not_inside_root(guard: PathGuard) -> None:
    misleading = guard.root.with_name(f"{guard.root.name}-other") / "file.txt"
    with pytest.raises(PathGuardError, match="escapes"):
        guard.validate_write(misleading)


def test_symlink_escape_rejected(guard: PathGuard, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("original")
    (guard.root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathGuardError, match="Symbolic links"):
        guard.write_text("escape/sentinel.txt", "forbidden")
    assert sentinel.read_text() == "original"


def test_broken_symlink_escape_rejected(guard: PathGuard) -> None:
    (guard.root / "escape").symlink_to(PROJECT_ROOT.parent / "jhm-missing-target")
    with pytest.raises(PathGuardError, match="Symbolic links"):
        guard.write_text("escape", "forbidden")


def test_internal_symlink_is_conservatively_rejected(guard: PathGuard) -> None:
    target = guard.write_text("original.txt", "original")
    (guard.root / "alias.txt").symlink_to(target)
    with pytest.raises(PathGuardError, match="Symbolic links"):
        guard.write_text("alias.txt", "forbidden")
    assert target.read_text() == "original"


def test_guard_root_cannot_widen_project_boundary() -> None:
    with pytest.raises(PathGuardError, match="inside PROJECT_ROOT"):
        PathGuard(PROJECT_ROOT.parent)


def test_guard_root_cannot_be_symlink(guard: PathGuard, tmp_path: Path) -> None:
    linked = tmp_path / "linked-root"
    linked.symlink_to(guard.root, target_is_directory=True)
    with pytest.raises(PathGuardError, match="Symbolic links"):
        PathGuard(linked)


def test_guard_root_is_immutable(guard: PathGuard) -> None:
    with pytest.raises(FrozenInstanceError):
        guard.root = PROJECT_ROOT.parent  # type: ignore[misc]


def test_file_ancestor_rejected(guard: PathGuard) -> None:
    guard.write_text("file.txt", "a file")
    with pytest.raises(PathGuardError, match="not a directory"):
        guard.write_text("file.txt/child.txt", "forbidden")


def test_new_files_and_directories_are_private(guard: PathGuard) -> None:
    directory = guard.mkdir("private/nested", parents=True)
    target = guard.write_bytes("private/nested/data.bin", b"\x00\xff")
    assert target.read_bytes() == b"\x00\xff"
    assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(directory.parent.stat().st_mode) & 0o077 == 0


def test_replacement_is_atomic_and_removes_temporary_file(guard: PathGuard) -> None:
    target = guard.write_text("state.txt", "old")
    old_inode = target.stat().st_ino
    guard.write_text("state.txt", "new")
    assert target.read_text() == "new"
    assert target.stat().st_ino != old_inode
    assert list(guard.root.glob(".jhm-write-*")) == []


def test_hardlink_cannot_mutate_outside_alias(guard: PathGuard, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    os.link(outside, guard.root / "alias.txt")
    with pytest.raises(PathGuardError, match="Hard-linked"):
        guard.write_text("alias.txt", "forbidden")
    assert outside.read_text() == "original"


def test_missing_parent_fails_without_creating_file(guard: PathGuard) -> None:
    with pytest.raises(PathGuardError):
        guard.write_text("missing/file.txt", "forbidden")
    assert not (guard.root / "missing").exists()


def test_mkdir_existing_real_directory(guard: PathGuard) -> None:
    target = guard.mkdir("existing")
    assert guard.mkdir(target, exist_ok=True) == target
    with pytest.raises(PathGuardError):
        guard.mkdir(target)


def test_mkdir_cannot_accept_existing_file(guard: PathGuard) -> None:
    guard.write_text("file.txt", "original")
    with pytest.raises(PathGuardError, match="not a real directory"):
        guard.mkdir("file.txt", exist_ok=True)


def test_write_cannot_replace_root(guard: PathGuard) -> None:
    with pytest.raises(PathGuardError, match="root cannot be replaced"):
        guard.write_text(guard.root, "forbidden")


def test_parent_symlink_swap_after_validation_is_rejected(
    guard: PathGuard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = guard.mkdir("parent")
    outside = tmp_path / "outside"
    outside.mkdir()
    original_validate = PathGuard.validate_write

    def validate_then_swap(self: PathGuard, path: str | Path) -> Path:
        result = original_validate(self, path)
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(PathGuard, "validate_write", validate_then_swap)
    with pytest.raises(PathGuardError):
        guard.write_text("parent/new.txt", "forbidden")
    assert not (outside / "new.txt").exists()


def test_unsupported_platform_fails_closed(
    guard: PathGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(PathGuardError, match="POSIX"):
        guard.write_text("file.txt", "forbidden")
    assert not (guard.root / "file.txt").exists()
