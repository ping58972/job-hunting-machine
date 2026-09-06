"""Bounded HTTP-first evidence capture and optional read-only browser rendering."""

import asyncio
import hashlib
import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.async_api import Route, async_playwright
from playwright.async_api import TimeoutError as BrowserTimeout

from job_hunting_machine.agents.extraction import JobFacts, extract
from job_hunting_machine.agents.retrieve_links import canonicalize
from job_hunting_machine.orchestration.queue import RetryableError
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Page:
    url: str
    status: int
    html: str
    content_type: str = "text/html"


class Reader(Protocol):
    async def read(self, url: str) -> Page: ...


class FakeReader:
    def __init__(self, pages: dict[str, Page] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    async def read(self, url: str) -> Page:
        self.calls.append(url)
        return self.pages.get(url, Page(url, 503, "Fixture not configured"))


async def resolved_url(url: str) -> tuple[str, str]:
    normalized = canonicalize(url)
    host = urlsplit(normalized).hostname
    assert host is not None
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        raise ValueError("linkedin_automation_disabled")
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("nonpublic_fetch_address")
    return normalized, str(addresses[0][4][0])


async def public_url(url: str) -> str:
    return (await resolved_url(url))[0]


class HTTPReader:
    def __init__(self) -> None:
        if os.environ.get("JOB_FETCH_ALLOW_LIVE") != "1":
            raise ValueError("live_job_fetch_requires_opt_in")

    async def read(self, url: str) -> Page:
        try:
            async with httpx.AsyncClient(
                timeout=20, trust_env=False, follow_redirects=False
            ) as client:
                for _ in range(6):
                    url, address = await resolved_url(url)
                    parsed = httpx.URL(url)
                    destination = parsed.copy_with(host=address)
                    async with client.stream(
                        "GET",
                        destination,
                        headers={
                            "Host": urlsplit(url).netloc,
                            "User-Agent": "JobHuntingMachine/0.1 (read-only)",
                        },
                        extensions={"sni_hostname": parsed.host.encode("ascii")},
                    ) as response:
                        if response.is_redirect:
                            url = urljoin(url, response.headers.get("location", ""))
                            continue
                        if response.status_code == 429 or response.status_code >= 500:
                            raise RetryableError("job_fetch_transient")
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_BYTES:
                                raise ValueError("job_page_too_large")
                        return Page(
                            url,
                            response.status_code,
                            bytes(content).decode("utf-8", errors="replace"),
                            response.headers.get("content-type", "text/html"),
                        )
        except httpx.HTTPError:
            raise RetryableError("job_fetch_transport") from None
        raise ValueError("job_redirect_limit")


class PlaywrightReader:
    def __init__(self) -> None:
        if (
            os.environ.get("JOB_BROWSER_ALLOW_LIVE") != "1"
            or os.environ.get("JOB_FETCH_ALLOW_LIVE") != "1"
        ):
            raise ValueError("live_job_browser_requires_opt_in")

    async def read(self, url: str) -> Page:
        url = await public_url(url)
        guard = PathGuard()
        temporary = guard.mkdir(".tmp/job-browser", parents=True, exist_ok=True)
        # Nonpersistent profile, downloads and browser temporary writes stay root-local.
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                downloads_path=str(temporary),
                env={**os.environ, "TMPDIR": str(temporary)},
            )
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block")
                count = 0

                async def read_only(route: Route) -> None:
                    nonlocal count
                    count += 1
                    if (
                        count > 100
                        or route.request.method != "GET"
                        or route.request.resource_type
                        not in {"document", "script", "stylesheet", "xhr", "fetch"}
                    ):
                        await route.abort()
                        return
                    try:
                        result = await HTTPReader().read(route.request.url)
                    except (ValueError, OSError, RetryableError):
                        await route.abort()
                    else:
                        await route.fulfill(
                            status=result.status, body=result.html, content_type=result.content_type
                        )

                await context.route("**/*", read_only)
                await context.route_web_socket("**/*", lambda ws: ws.close())
                page = await context.new_page()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(500)
                html = await page.content()
                if len(html.encode()) > MAX_BYTES:
                    raise ValueError("job_page_too_large")
                return Page(page.url, response.status if response else 200, html)
            finally:
                await browser.close()


@dataclass(frozen=True)
class Evidence:
    facts: JobFacts
    raw_path: str
    text_path: str
    sha256: str
    attempts: list[dict[str, object]]


class JobFetcher:
    def __init__(
        self,
        http: Reader | None = None,
        browser: Reader | None = None,
        *,
        evidence_root: Path = PROJECT_ROOT / "evidence/jobs",
    ) -> None:
        self.http = http or FakeReader()
        self.browser = browser
        self.root = PathGuard().validate_write(evidence_root)

    async def fetch(self, url: str, job_id: str) -> Evidence:
        from job_hunting_machine.ids import IdKind, validate_id

        validate_id(job_id, IdKind.JOB)
        attempts: list[dict[str, object]] = []
        readers = [self.http] + ([self.browser] if self.browser else [])
        for index, reader in enumerate(readers):
            try:
                page = await reader.read(url)
            except BrowserTimeout:
                raise RetryableError("job_browser_timeout") from None
            if len(page.html.encode()) > MAX_BYTES:
                raise ValueError("job_page_too_large")
            facts = extract(page.html, page.status)
            sha = hashlib.sha256(page.html.encode()).hexdigest()
            guard = PathGuard()
            directory = guard.mkdir(self.root / job_id, parents=True, exist_ok=True)
            raw = directory / f"{sha}.html"
            plain = directory / f"{sha}.txt"
            guard.write_text(raw, page.html)
            guard.write_text(plain, facts.text)
            attempts.append(
                {
                    "url": page.url,
                    "status": page.status,
                    "path": str(raw),
                    "sha256": sha,
                    "transport": "http" if index == 0 else "browser",
                }
            )
            # Challenges and explicit closure are never bypassed through a browser.
            if (
                facts.application_open is False
                or "human_challenge" in facts.issues
                or (facts.job_title and facts.company_name and len(facts.text) > 100)
            ):
                break
        return Evidence(facts, str(raw), str(plain), sha, attempts)
