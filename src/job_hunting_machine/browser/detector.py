"""Deterministic ATS detection from URL and stable DOM markers."""

from playwright.async_api import Page

from job_hunting_machine.browser.adapters import ADAPTERS, GenericAdapter
from job_hunting_machine.browser.types import ATSKind

_ORDER = (
    ATSKind.GREENHOUSE,
    ATSKind.LEVER,
    ATSKind.ASHBY,
    ATSKind.WORKDAY,
    ATSKind.SMARTRECRUITERS,
    ATSKind.ICIMS,
)
_HOST_MARKERS = {
    ATSKind.GREENHOUSE: ("greenhouse.io", "boards.greenhouse"),
    ATSKind.LEVER: ("lever.co", "jobs.lever"),
    ATSKind.ASHBY: ("ashbyhq.com", "jobs.ashby"),
    ATSKind.WORKDAY: ("myworkdayjobs.com", "workday.com"),
    ATSKind.SMARTRECRUITERS: ("smartrecruiters.com",),
    ATSKind.ICIMS: ("icims.com",),
}


async def detect_ats(page: Page) -> ATSKind:
    url = page.url.lower()
    marker = (await page.locator("body").get_attribute("data-ats") or "").upper()
    for kind in _ORDER:
        if marker == kind.value or any(value in url for value in _HOST_MARKERS[kind]):
            return kind
    html = (await page.locator("html").inner_html()).lower()
    dom_markers = {
        ATSKind.GREENHOUSE: ("greenhouse", "grnhse_app"),
        ATSKind.LEVER: ("lever-job", "lever.co"),
        ATSKind.ASHBY: ("ashby",),
        ATSKind.WORKDAY: ("data-automation-id", "workday"),
        ATSKind.SMARTRECRUITERS: ("smartrecruiters",),
        ATSKind.ICIMS: ("icims",),
    }
    for kind in _ORDER:
        if any(value in html for value in dom_markers[kind]):
            return kind
    return ATSKind.GENERIC


async def adapter_for(page: Page) -> GenericAdapter:
    return ADAPTERS[await detect_ats(page)]
