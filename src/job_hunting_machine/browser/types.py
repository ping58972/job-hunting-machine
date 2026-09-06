"""Semantic browser types. None exposes a final-submit capability."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from playwright.async_api import Page


class ATSKind(StrEnum):
    GREENHOUSE = "GREENHOUSE"
    LEVER = "LEVER"
    ASHBY = "ASHBY"
    WORKDAY = "WORKDAY"
    SMARTRECRUITERS = "SMARTRECRUITERS"
    ICIMS = "ICIMS"
    GENERIC = "GENERIC"


class ChallengeKind(StrEnum):
    CAPTCHA = "CAPTCHA"
    MFA = "MFA"


ControlKind = Literal["text", "textarea", "select", "checkbox", "radio", "file"]


@dataclass(frozen=True, slots=True)
class SemanticField:
    field_key: str
    label: str
    control: ControlKind
    name: str | None = None
    autocomplete: str | None = None
    options: tuple[str, ...] = ()
    required: bool = False
    current_value: str | bool | None = None


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    page_key: str
    url: str
    fields: tuple[SemanticField, ...]
    challenges: tuple[ChallengeKind, ...] = ()
    final_control_present: bool = False
    supported_for_mutation: bool = True


@dataclass(frozen=True, slots=True)
class UploadFile:
    name: str
    mime_type: str
    content: bytes


class FinalSubmissionForbidden(RuntimeError):
    """A requested browser operation could activate final submission."""


class HumanBrowserRequired(RuntimeError):
    """Browser preparation must pause for a human."""


class BaseAdapter(Protocol):
    """The complete ATS adapter surface; final submission is absent by design."""

    kind: ATSKind
    supported_for_mutation: bool

    async def inspect(self, page: Page) -> PageSnapshot: ...

    async def fill(self, page: Page, field: SemanticField, value: object) -> None: ...

    async def upload(self, page: Page, field: SemanticField, file: UploadFile) -> None: ...

    async def advance(self, page: Page) -> bool: ...
