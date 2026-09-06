"""Final-click Playwright adapter, importable only by the Submission Agent CLI."""

import re
from contextlib import suppress

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from job_hunting_machine.browser.manager import BrowserManager
from job_hunting_machine.submission.types import SubmissionOutcome, SubmissionResult

_SUBMIT = re.compile(r"\b(submit|send)\b.*\b(application|application now)\b", re.I)
_CONFIRM = re.compile(
    r"application (was |has been )?(submitted|received)|thank you for applying", re.I
)
_CHALLENGE = re.compile(r"captcha|multi[- ]factor|verification code", re.I)


class PlaywrightSubmissionAdapter:
    """Perform one exact final click and verify the resulting portal page."""

    def __init__(self, manager: BrowserManager) -> None:
        self.manager = manager

    @staticmethod
    def _url(review: dict[str, object]) -> str:
        value = review.get("application_url")
        if not isinstance(value, str) or not value:
            raise ValueError("submission_url_missing")
        return value

    @staticmethod
    async def _confirmation(page: Page) -> bool:
        body = await page.locator("body").inner_text()
        return _CONFIRM.search(body) is not None

    @staticmethod
    async def _control(page: Page) -> Locator:
        body = await page.locator("body").inner_text()
        if _CHALLENGE.search(body):
            raise ValueError("submission_challenge_requires_human")
        candidates = page.locator("button, input[type=submit]")
        matches: list[Locator] = []
        for index in range(await candidates.count()):
            item = candidates.nth(index)
            label = (await item.inner_text()) or (await item.get_attribute("value")) or ""
            if await item.is_visible() and await item.is_enabled() and _SUBMIT.search(label):
                matches.append(item)
        if len(matches) != 1:
            raise ValueError("final_submit_control_not_unique")
        return matches[0]

    async def submit(self, application_id: str, review: dict[str, object]) -> SubmissionResult:
        clicked = False
        try:
            async with self.manager.session(application_id, self._url(review)) as handle:
                control = await self._control(handle.page)
                clicked = True
                await control.click()
                with suppress(PlaywrightTimeoutError):
                    await handle.page.wait_for_function(
                        "() => /application (was |has been )?(submitted|received)|"
                        "thank you for applying/i.test(document.body.innerText)",
                        timeout=10_000,
                    )
                await self.manager.save_state(handle)
                if await self._confirmation(handle.page):
                    return SubmissionResult(SubmissionOutcome.CONFIRMED, handle.page.url)
                return SubmissionResult(SubmissionOutcome.UNKNOWN)
        except Exception:
            if clicked:
                return SubmissionResult(SubmissionOutcome.UNKNOWN)
            raise

    async def reconcile(self, application_id: str, review: dict[str, object]) -> SubmissionResult:
        async with self.manager.session(application_id, self._url(review)) as handle:
            if await self._confirmation(handle.page):
                return SubmissionResult(SubmissionOutcome.CONFIRMED, handle.page.url)
            return SubmissionResult(SubmissionOutcome.NOT_SUBMITTED)
