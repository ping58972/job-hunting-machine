"""Conservative semantic ATS adapters with an unconditional submit guard."""

import re
from typing import cast

from playwright.async_api import Locator, Page

from job_hunting_machine.browser.types import (
    ATSKind,
    ChallengeKind,
    ControlKind,
    FinalSubmissionForbidden,
    HumanBrowserRequired,
    PageSnapshot,
    SemanticField,
    UploadFile,
)

_FINAL = re.compile(r"\b(submit(?:\s+(?:my|this|the))?\s+application|apply\s+now)\b", re.I)
_ADVANCE = re.compile(r"^(?:next|continue|save\s*(?:and|&)\s*continue)$", re.I)


def reject_final_semantics(text: str) -> None:
    if _FINAL.search(" ".join(text.split())):
        raise FinalSubmissionForbidden("final_submission_control_forbidden")


async def _label(locator: Locator) -> str:
    result = await locator.evaluate(
        """el => {
          const own = el.getAttribute('aria-label') || el.getAttribute('data-jhm-label');
          if (own) return own;
          if (el.labels && el.labels.length) return [...el.labels].map(x => x.innerText).join(' ');
          const id = el.id;
          if (id) {
            const l = document.querySelector(`label[for="${CSS.escape(id)}"]`);
            if (l) return l.innerText;
          }
          return el.name || el.placeholder || '';
        }"""
    )
    return str(result).strip()


class GenericAdapter:
    kind = ATSKind.GENERIC
    supported_for_mutation = True

    async def inspect(self, page: Page) -> PageSnapshot:
        body = await page.locator("body").inner_text()
        lowered = body.lower()
        challenges: list[ChallengeKind] = []
        if any(token in lowered for token in ("captcha", "recaptcha", "hcaptcha")):
            challenges.append(ChallengeKind.CAPTCHA)
        if any(
            token in lowered
            for token in ("multi-factor", "two-factor", "verification code", "one-time code")
        ):
            challenges.append(ChallengeKind.MFA)
        controls = page.locator("input:not([type=hidden]), textarea, select")
        fields: list[SemanticField] = []
        for index in range(await controls.count()):
            control = controls.nth(index)
            tag = await control.evaluate("el => el.tagName.toLowerCase()")
            raw_type = (await control.get_attribute("type") or "text").lower()
            kind = "textarea" if tag == "textarea" else "select" if tag == "select" else raw_type
            if kind not in {"text", "textarea", "select", "checkbox", "radio", "file"}:
                kind = "text"
            label = await _label(control)
            name = await control.get_attribute("name")
            identifier = (
                await control.get_attribute("data-jhm-field")
                or name
                or await control.get_attribute("id")
            )
            field_key = identifier or f"field_{index}"
            options: tuple[str, ...] = ()
            if tag == "select":
                options = tuple(await control.locator("option").all_text_contents())
            value: str | bool | None
            if kind in {"checkbox", "radio"}:
                value = await control.is_checked()
            elif kind == "file":
                value = await control.evaluate("el => el.files?.[0]?.name || null")
            else:
                value = await control.input_value()
            fields.append(
                SemanticField(
                    field_key=str(field_key),
                    label=label or str(field_key),
                    control=cast(ControlKind, kind),
                    name=name,
                    autocomplete=await control.get_attribute("autocomplete"),
                    options=options,
                    required=await control.get_attribute("required") is not None,
                    current_value=value,
                )
            )
        buttons = page.get_by_role("button")
        final = False
        for index in range(await buttons.count()):
            text = (await buttons.nth(index).inner_text()).strip()
            final = final or bool(_FINAL.search(text))
        page_key = await page.locator("body").get_attribute("data-page-key") or page.url
        return PageSnapshot(
            str(page_key),
            page.url,
            tuple(fields),
            tuple(challenges),
            final,
            self.supported_for_mutation,
        )

    async def _locator(self, page: Page, field: SemanticField) -> Locator:
        locator = page.locator(f'[data-jhm-field="{field.field_key}"]')
        if await locator.count() == 0 and field.name:
            locator = page.locator(f'[name="{field.name}"]')
        if await locator.count() != 1:
            raise HumanBrowserRequired("field_no_longer_unambiguous")
        return locator

    async def fill(self, page: Page, field: SemanticField, value: object) -> None:
        reject_final_semantics(f"{field.field_key} {field.label} {field.name or ''}")
        if field.control == "file":
            raise ValueError("file_field_requires_upload")
        locator = await self._locator(page, field)
        if field.control == "select":
            await locator.select_option(label=str(value))
        elif field.control in {"checkbox", "radio"}:
            await locator.set_checked(bool(value))
        else:
            await locator.fill(str(value))

    async def upload(self, page: Page, field: SemanticField, file: UploadFile) -> None:
        reject_final_semantics(f"{field.field_key} {field.label} {field.name or ''}")
        if field.control != "file":
            raise ValueError("upload_requires_file_field")
        await (await self._locator(page, field)).set_input_files(
            {"name": file.name, "mimeType": file.mime_type, "buffer": file.content}
        )

    async def advance(self, page: Page) -> bool:
        buttons = page.get_by_role("button")
        candidates: list[Locator] = []
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            text = (await button.inner_text()).strip()
            reject_final_semantics(text)
            if _ADVANCE.fullmatch(text):
                candidates.append(button)
        if len(candidates) > 1:
            raise HumanBrowserRequired("advance_control_ambiguous")
        if not candidates:
            return False
        await candidates[0].click()
        await page.wait_for_load_state("domcontentloaded")
        return True


class GreenhouseAdapter(GenericAdapter):
    kind = ATSKind.GREENHOUSE


class LeverAdapter(GenericAdapter):
    kind = ATSKind.LEVER


class AshbyAdapter(GenericAdapter):
    kind = ATSKind.ASHBY


class UnsupportedAdapter(GenericAdapter):
    supported_for_mutation = False


class WorkdayAdapter(UnsupportedAdapter):
    kind = ATSKind.WORKDAY


class SmartRecruitersAdapter(UnsupportedAdapter):
    kind = ATSKind.SMARTRECRUITERS


class ICIMSAdapter(UnsupportedAdapter):
    kind = ATSKind.ICIMS


ADAPTERS: dict[ATSKind, GenericAdapter] = {
    ATSKind.GREENHOUSE: GreenhouseAdapter(),
    ATSKind.LEVER: LeverAdapter(),
    ATSKind.ASHBY: AshbyAdapter(),
    ATSKind.WORKDAY: WorkdayAdapter(),
    ATSKind.SMARTRECRUITERS: SmartRecruitersAdapter(),
    ATSKind.ICIMS: ICIMSAdapter(),
    ATSKind.GENERIC: GenericAdapter(),
}
