"""Conservative, read-only portal adapters and deterministic vendor selection."""

import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from job_hunting_machine.agents.fetch import resolved_url
from job_hunting_machine.monitor.types import PortalPage
from job_hunting_machine.orchestration.queue import RetryableError


@dataclass
class FakePortalReader:
    pages: dict[str, PortalPage] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def read(self, url: str) -> PortalPage:
        self.calls.append(url)
        return self.pages.get(url, PortalPage(url, 404, ""))


class HTTPPortalReader:
    """GET-only portal transport; construction and use are explicitly live-gated."""

    def __init__(self) -> None:
        if os.environ.get("PORTAL_MONITOR_ALLOW_LIVE") != "1":
            raise ValueError("live_portal_monitor_requires_opt_in")

    async def read(self, url: str) -> PortalPage:
        normalized, address = await resolved_url(url)
        parsed = httpx.URL(normalized)
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.get(
                    parsed.copy_with(host=address),
                    headers={
                        "Host": parsed.netloc.decode(),
                        "User-Agent": "JobHuntingMachine/0.1 (read-only monitor)",
                    },
                    extensions={"sni_hostname": parsed.host.encode("ascii")},
                    follow_redirects=False,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetryableError("portal_monitor_transient")
                return PortalPage(normalized, response.status_code, response.text[:500_000])
        except RetryableError:
            raise
        except httpx.HTTPError:
            raise RetryableError("portal_monitor_transport") from None


class PortalAdapter(Protocol):
    @property
    def kind(self) -> str: ...

    def supports(self, ats_type: str | None, url: str, page: PortalPage) -> bool: ...

    def evidence_text(self, page: PortalPage) -> str: ...


@dataclass(frozen=True, slots=True)
class TextPortalAdapter:
    kind: str
    hosts: tuple[str, ...]

    def supports(self, ats_type: str | None, url: str, page: PortalPage) -> bool:
        return (ats_type or "").casefold() == self.kind.casefold() or any(
            host in url.casefold() for host in self.hosts
        )

    def evidence_text(self, page: PortalPage) -> str:
        return page.text


class PortalAdapterRegistry:
    def __init__(self) -> None:
        self.adapters = (
            TextPortalAdapter("GREENHOUSE", ("greenhouse.io", "greenhouse")),
            TextPortalAdapter("LEVER", ("lever.co", "lever")),
            TextPortalAdapter("ASHBY", ("ashbyhq.com", "ashby")),
            TextPortalAdapter("WORKDAY", ("myworkdayjobs.com", "workday")),
            TextPortalAdapter("SMARTRECRUITERS", ("smartrecruiters.com",)),
            TextPortalAdapter("ICIMS", ("icims.com",)),
            TextPortalAdapter("GENERIC", ()),
        )

    def select(self, ats_type: str | None, url: str, page: PortalPage) -> PortalAdapter:
        for adapter in self.adapters[:-1]:
            if adapter.supports(ats_type, url, page):
                return adapter
        return self.adapters[-1]
