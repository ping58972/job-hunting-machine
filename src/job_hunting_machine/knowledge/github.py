"""GET-only fixed-origin GitHub API and deterministic fixture transport."""

import base64
import hashlib
import logging
import os
import re
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from job_hunting_machine.orchestration.queue import RetryableError


class GitHubReader(Protocol):
    async def get(self, path: str) -> Any: ...


def repository_name(value: str) -> str:
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value
    ) or value.split("/")[-1] in {".", ".."}:
        raise ValueError("invalid_github_repository")
    return value.lower()


def sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("invalid_git_sha")
    return value


class FakeGitHub:
    def __init__(self, fixtures: dict[str, Any] | None = None) -> None:
        self.fixtures = fixtures or {}
        self.calls: list[str] = []

    async def get(self, path: str) -> Any:
        self.calls.append(path)
        if path not in self.fixtures:
            raise ValueError("github_fixture_missing")
        return self.fixtures[path]


class GitHubHTTP:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if transport is None and os.environ.get("GITHUB_ALLOW_LIVE") != "1":
            raise ValueError("github_live_requires_explicit_opt_in")
        self.transport = transport
        self.token = os.environ.get("GITHUB_TOKEN") if transport is None else None
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            logger.setLevel(logging.CRITICAL + 1)
            logger.propagate = False

    async def get(self, path: str) -> Any:
        if (
            not path.startswith(("/repos/", "/users/"))
            or ".." in path
            or "\\" in path
            or "#" in path
        ):
            raise ValueError("invalid_github_api_path")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport, timeout=20, trust_env=False, follow_redirects=False
                ) as client,
                client.stream("GET", "https://api.github.com" + path, headers=headers) as response,
            ):
                if response.status_code in {403, 429} or response.status_code >= 500:
                    raise RetryableError("github_rate_or_server_error")
                if response.status_code != 200:
                    raise ValueError("github_resource_unavailable")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 8_000_000:
                        raise ValueError("github_response_too_large")
                import json

                return json.loads(data)
        except httpx.HTTPError:
            raise RetryableError("github_transport_error") from None


async def inventory(client: GitHubReader, owner: str) -> list[str]:
    repository_name(f"{owner}/placeholder")
    repos: set[str] = set()
    for page in range(1, 11):
        rows = await client.get(f"/users/{owner}/repos?per_page=100&page={page}&type=owner")
        if not isinstance(rows, list):
            raise ValueError("invalid_github_inventory")
        for row in rows:
            name = repository_name(row["full_name"])
            if name.split("/")[0] == owner.lower():
                repos.add(name)
        if len(rows) < 100:
            return sorted(repos)
    raise ValueError("github_inventory_limit")


async def head(client: GitHubReader, repository: str) -> dict[str, Any]:
    repository = repository_name(repository)
    metadata = await client.get(f"/repos/{repository}")
    if repository_name(metadata["full_name"]) != repository:
        raise ValueError("github_repository_identity_changed")
    branch = metadata["default_branch"]
    if not isinstance(branch, str) or not branch:
        raise ValueError("github_default_branch_missing")
    commit = await client.get(f"/repos/{repository}/commits/{quote(branch, safe='')}")
    return {
        "repository": repository,
        "commit": sha(commit["sha"]),
        "tree": sha(commit["commit"]["tree"]["sha"]),
        "description": metadata.get("description"),
        "topics": metadata.get("topics", []),
    }


async def blob(client: GitHubReader, repository: str, blob_sha: str) -> bytes:
    data = await client.get(f"/repos/{repository_name(repository)}/git/blobs/{sha(blob_sha)}")
    if data.get("sha") != blob_sha or data.get("encoding") != "base64":
        raise ValueError("github_blob_identity_mismatch")
    raw = base64.b64decode("".join(data["content"].split()), validate=True)
    if (
        len(raw) > 200_000
        or hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() != blob_sha
    ):
        raise ValueError("github_blob_integrity_failure")
    raw.decode("utf-8")
    return raw
