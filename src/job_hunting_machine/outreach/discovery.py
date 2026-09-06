"""Read-only company contact discovery with root-local public evidence."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from job_hunting_machine.agents.fetch import resolved_url
from job_hunting_machine.outreach.types import ContactReader, DiscoveredContact, PublicPage
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard

MAX_CONTACT_PAGE_BYTES = 2_000_000


class FakeContactReader(ContactReader):
    def __init__(self, pages: dict[str, PublicPage] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[str] = []

    async def read(self, url: str) -> PublicPage:
        self.calls.append(url)
        return self.pages.get(url, PublicPage(url, 404, ""))


class HTTPContactReader(ContactReader):
    def __init__(self) -> None:
        if os.environ.get("CONTACT_DISCOVERY_ALLOW_LIVE") != "1":
            raise ValueError("live_contact_discovery_requires_opt_in")

    async def read(self, url: str) -> PublicPage:
        async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            for _ in range(5):
                normalized, address = await resolved_url(url)
                parsed = httpx.URL(normalized)
                response = await client.get(
                    parsed.copy_with(host=address),
                    headers={
                        "Host": urlsplit(normalized).netloc,
                        "User-Agent": "JHM/0.1 read-only",
                    },
                    extensions={"sni_hostname": parsed.host.encode("ascii")},
                )
                if response.is_redirect:
                    url = urljoin(normalized, response.headers.get("location", ""))
                    continue
                if len(response.content) > MAX_CONTACT_PAGE_BYTES:
                    raise ValueError("contact_page_too_large")
                return PublicPage(normalized, response.status_code, response.text)
        raise ValueError("contact_redirect_limit")


def contact_type(title: str | None) -> str:
    value = (title or "").lower()
    if "recruit" in value or "talent acquisition" in value:
        return "RECRUITER"
    if "human resources" in value or value.strip() == "hr":
        return "HR"
    if "hiring manager" in value:
        return "HIRING_MANAGER"
    if title:
        return "EMPLOYEE"
    return "OTHER"


def extract_contacts(page: PublicPage) -> list[DiscoveredContact]:
    """Extract only literal public values; missing names and titles stay missing."""
    if page.status != 200 or len(page.html.encode()) > MAX_CONTACT_PAGE_BYTES:
        return []
    soup = BeautifulSoup(page.html, "html.parser")
    found: list[DiscoveredContact] = []
    for node in soup.select("[data-jhm-contact]"):
        name = node.get("data-name")
        title = node.get("data-title")
        mail = node.select_one("a[href^='mailto:']")
        linked = node.select_one("a[href*='linkedin.com/']")
        email = str(mail.get("href", "")).removeprefix("mailto:").split("?", 1)[0] if mail else None
        linkedin = str(linked.get("href")) if linked and linked.get("href") else None
        if email or linkedin:
            found.append(
                DiscoveredContact(
                    page.url,
                    str(name).strip() if name else None,
                    str(title).strip() if title else None,
                    contact_type(str(title) if title else None),
                    email,
                    linkedin,
                    0.95 if name and title else 0.8,
                )
            )
    for mail in soup.select("a[href^='mailto:']"):
        if mail.find_parent(attrs={"data-jhm-contact": True}):
            continue
        href = mail.get("href", "")
        email = str(href).removeprefix("mailto:").split("?", 1)[0]
        if email:
            found.append(DiscoveredContact(page.url, None, None, "OTHER", email, None, 0.7))
    return found


@dataclass(frozen=True, slots=True)
class CapturedEvidence:
    page: PublicPage
    path: str
    sha256: str
    contacts: tuple[DiscoveredContact, ...]


class ContactDiscovery:
    def __init__(
        self,
        reader: ContactReader,
        *,
        evidence_root: Path = PROJECT_ROOT / "evidence/contacts",
    ) -> None:
        self.reader = reader
        self.root = PathGuard().validate_write(evidence_root)

    async def capture(self, application_id: str, url: str) -> CapturedEvidence:
        page = await self.reader.read(url)
        content = page.html.encode()
        if len(content) > MAX_CONTACT_PAGE_BYTES:
            raise ValueError("contact_page_too_large")
        digest = hashlib.sha256(content).hexdigest()
        directory = PathGuard().mkdir(self.root / application_id, parents=True, exist_ok=True)
        path = PathGuard().write_bytes(directory / f"{digest}.html", content)
        return CapturedEvidence(
            page, str(path.relative_to(PROJECT_ROOT)), digest, tuple(extract_contacts(page))
        )


def rank_contacts(values: list[DiscoveredContact]) -> list[DiscoveredContact]:
    order = {"RECRUITER": 0, "HIRING_MANAGER": 1, "HR": 2, "EMPLOYEE": 3, "OTHER": 4}
    return sorted(
        values,
        key=lambda item: (
            order[item.contact_type],
            -item.confidence,
            (item.full_name or "").casefold(),
            (item.email or item.linkedin_url or "").casefold(),
        ),
    )
