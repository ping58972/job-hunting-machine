"""Root-confined Playwright lifecycle, recovery, and private storage state."""

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    StorageState,
    async_playwright,
)

from job_hunting_machine.agents.fetch import public_url
from job_hunting_machine.browser.config import BrowserSettings
from job_hunting_machine.browser.fake import FAKE_ATS_ORIGIN, FakeATSApplication
from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


@dataclass(slots=True)
class BrowserSessionHandle:
    application_id: str
    url: str
    page: Page
    context: BrowserContext
    storage_state_path: Path


class BrowserManager:
    """Own Chromium contexts and one modifying session per domain in this process."""

    _domains: ClassVar[set[str]] = set()

    def __init__(
        self,
        settings: BrowserSettings,
        *,
        fake: FakeATSApplication | None = None,
        root: Path = PROJECT_ROOT / "data/browser-sessions",
    ) -> None:
        self.settings = settings
        self.fake = fake
        self.guard = PathGuard()
        self.root = self.guard.validate_write(root)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        if self._browser and self._browser.is_connected():
            return
        temporary = self.guard.mkdir(".tmp/form-browser", parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.headless,
            downloads_path=str(temporary),
            env={**os.environ, "TMPDIR": str(temporary)},
        )

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None

    async def _checked_url(self, url: str) -> str:
        if self.settings.runtime_mode is RuntimeMode.STAGING:
            if not url.startswith(FAKE_ATS_ORIGIN + "/") or self.fake is None:
                raise ValueError("staging_browser_allows_fake_ats_only")
            return url
        if self.settings.runtime_mode is RuntimeMode.DRY_RUN:
            raise ValueError("dry_run_browser_is_offline")
        if not self.settings.allow_live:
            raise ValueError("live_form_browser_requires_all_opt_ins")
        return await public_url(url)

    def state_path(self, application_id: str) -> Path:
        validate_id(application_id, IdKind.APPLICATION)
        directory = self.guard.mkdir(self.root / application_id, parents=True, exist_ok=True)
        return self.guard.validate_write(directory / "storage-state.json")

    def _lock_domain(self, domain: str) -> int:
        directory = self.guard.mkdir(".tmp/form-browser-locks", parents=True, exist_ok=True)
        name = hashlib.sha256(domain.encode()).hexdigest() + ".lock"
        path = self.guard.validate_write(directory / name)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise ValueError("browser_domain_lock_invalid")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise ValueError("browser_domain_already_has_modifying_session") from None
        return descriptor

    def read_state(self, application_id: str) -> dict[str, object] | None:
        path = self.state_path(application_id)
        if not path.exists():
            return None
        if path.stat().st_size > 5_000_000:
            raise ValueError("browser_storage_state_too_large")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("browser_storage_state_invalid")
        return value

    async def save_state(self, handle: BrowserSessionHandle) -> Path:
        state = await handle.context.storage_state(indexed_db=True)
        return self.guard.write_text(
            handle.storage_state_path,
            json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False),
        )

    @asynccontextmanager
    async def session(self, application_id: str, url: str) -> AsyncIterator[BrowserSessionHandle]:
        checked = await self._checked_url(url)
        domain = urlsplit(checked).hostname
        if not domain:
            raise ValueError("browser_url_has_no_host")
        if domain in self._domains:
            raise ValueError("browser_domain_already_has_modifying_session")
        lock = self._lock_domain(domain)
        self._domains.add(domain)
        try:
            await self.start()
            assert self._browser is not None
            context = await self._browser.new_context(
                accept_downloads=False,
                service_workers="block",
                storage_state=cast(StorageState | None, self.read_state(application_id)),
            )
            try:
                if self.settings.runtime_mode is RuntimeMode.STAGING:
                    assert self.fake is not None

                    async def fulfill(route: Route) -> None:
                        assert self.fake is not None
                        await self.fake.fulfill(route)

                    await context.route("**/*", fulfill)
                    await context.route_web_socket("**/*", lambda ws: ws.close())
                page = await context.new_page()
                await page.goto(checked, wait_until="domcontentloaded")
                yield BrowserSessionHandle(
                    application_id, checked, page, context, self.state_path(application_id)
                )
            finally:
                await context.close()
        finally:
            self._domains.discard(domain)
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
