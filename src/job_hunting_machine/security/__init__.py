"""Local filesystem safety primitives."""

from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard, PathGuardError

__all__ = ["PROJECT_ROOT", "PathGuard", "PathGuardError"]
