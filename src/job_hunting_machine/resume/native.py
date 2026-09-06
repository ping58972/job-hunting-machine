"""Native document traversal and scoped edits, including header tables and all tabs.

No DOCX round trip, table reconstruction, margin changes, or font compression.
Indexes use Google's UTF-16 code units; final paragraph newlines are never deleted.
"""

import copy
from dataclasses import dataclass
from typing import Any

from job_hunting_machine.models.prompts import digest

Json = dict[str, Any]
SECTIONS = {"PROJECTS", "SKILLS"}
HEADINGS = SECTIONS | {
    "EDUCATION",
    "WORK EXPERIENCE",
    "EXPERIENCE",
    "SUMMARY",
    "CERTIFICATIONS",
    "AWARDS",
    "PUBLICATIONS",
    "INTERESTS",
    "VOLUNTEERING",
    "REFERENCES",
}


def normalize(document: Json) -> Json:
    """Accept the REST API's documentTab form and the connector's normalized form."""
    result = copy.deepcopy(document)
    tabs: list[Json] = []

    def add(items: list[Json], parent: str = "") -> None:
        for index, item in enumerate(items):
            tab = item.get("documentTab", item)
            properties = item.get("tabProperties", item)
            path = f"{parent}/{index}"
            tabs.append(
                {
                    **tab,
                    "tabId": properties.get("tabId", ""),
                    "tabTitle": properties.get("title", properties.get("tabTitle", "")),
                    "tabPath": properties.get("tabPath", path),
                }
            )
            add(item.get("childTabs", []), path)

    add(result.get("tabs", []))
    if not tabs:
        tabs = [{**result, "tabId": ""}]
    result["tabs"] = tabs
    return result


@dataclass
class Paragraph:
    key: str
    tab_id: str
    segment_id: str
    start: int
    paragraph: Json
    section: str | None
    writable: bool

    @property
    def text(self) -> str:
        return "".join(e.get("textRun", {}).get("content", "") for e in self.paragraph["elements"])

    @property
    def style(self) -> Json:
        runs = [e["textRun"] for e in self.paragraph["elements"] if "textRun" in e]
        style: Json = copy.deepcopy(runs[0].get("textStyle", {})) if runs else {}
        style.pop("link", None)  # A replaced project must never retain the old project's URL.
        return style


def paragraphs(document: Json) -> list[Paragraph]:
    result: list[Paragraph] = []
    for tab_index, tab in enumerate(document["tabs"]):
        segments = [("body", "", tab.get("body", {}))]
        for kind in ("headers", "footers"):
            segments.extend(
                (f"{kind}/{i}", key, value)
                for i, (key, value) in enumerate(tab.get(kind, {}).items())
            )
        for name, segment_id, segment in segments:
            section: str | None = None

            def walk(
                content: list[Json],
                path: str,
                first_cell: bool = True,
                tab_id: str = tab["tabId"],
                segment: str = segment_id,
            ) -> None:
                nonlocal section
                for index, element in enumerate(content):
                    key = f"{path}/{index}"
                    if "table" in element:
                        for r, row in enumerate(element["table"]["tableRows"]):
                            for c, cell in enumerate(row["tableCells"]):
                                walk(cell["content"], f"{key}/r{r}/c{c}", first_cell and c == 0)
                    if "paragraph" not in element:
                        continue
                    p = Paragraph(
                        key,
                        tab_id,
                        segment,
                        element.get("startIndex", 0),
                        element["paragraph"],
                        section,
                        first_cell,
                    )
                    heading = p.text.strip().upper()
                    named_style = p.paragraph.get("paragraphStyle", {}).get("namedStyleType", "")
                    if heading in HEADINGS or named_style.startswith("HEADING_"):
                        section = heading if heading in SECTIONS else None
                        p.section = None
                    else:
                        p.section = section
                    result.append(p)

            walk(segment.get("content", []), f"t{tab_index}/{name}")
    return result


def slots(document: Json) -> list[Paragraph]:
    for section in SECTIONS:
        if sum(p.text.strip().upper() == section for p in paragraphs(document)) != 1:
            raise ValueError("ambiguous_template_sections")
    result = [p for p in paragraphs(document) if p.section in SECTIONS]
    if {p.section for p in result} != SECTIONS:
        raise ValueError("template_requires_projects_and_skills")
    for p in result:
        if not p.text.endswith("\n") or any("textRun" not in e for e in p.paragraph["elements"]):
            raise ValueError("unsupported_editable_paragraph")
    return result


def signature(document: Json, *, edited: bool = False) -> str:
    """Compare all native structure/styles, masking only authorized paragraph text.

    Provider-generated IDs and indexes may change on copy/edit. Paragraph/run styles
    outside the two sections, table geometry, lists, and document styles remain bound.
    """
    doc = copy.deepcopy(document)
    for p in paragraphs(doc):
        if edited and p.section in SECTIONS:
            p.paragraph["elements"] = [{"textRun": {"content": "<editable>", "textStyle": p.style}}]

    volatile = {
        "startIndex",
        "endIndex",
        "documentId",
        "revisionId",
        "title",
        "tabId",
        "headerId",
        "footerId",
        "defaultHeaderId",
        "defaultFooterId",
        "firstPageHeaderId",
        "firstPageFooterId",
        "evenPageHeaderId",
        "evenPageFooterId",
        "listId",
        "headingId",
        "suggestionsViewMode",
    }

    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, dict):
            return {
                k: [clean(v) for v in item.values()]
                if k in {"headers", "footers", "lists"}
                else clean(item)
                for k, item in value.items()
                if k not in volatile
            }
        return value

    # Body/header content is contained in tabs, not duplicated from the API's legacy view.
    return digest(
        clean(
            {
                k: v
                for k, v in doc.items()
                if k not in {"body", "headers", "footers", "url", "document_url"}
            }
        )
    )


def replacements(document: Json, projects: list[str], skills: list[str]) -> dict[str, str]:
    available = slots(document)
    project_slots = [p for p in available if p.section == "PROJECTS" and p.writable]
    skill_slots = [p for p in available if p.section == "SKILLS" and p.writable]
    if not projects or not skills or len(projects) > len(project_slots) or not skill_slots:
        raise ValueError("insufficient_template_slots_or_verified_content")
    result = dict.fromkeys((p.key for p in available), "")
    for p, text in zip(project_slots, projects, strict=False):
        result[p.key] = text
    result[skill_slots[0].key] = ", ".join(skills)
    return result


def edit_requests(document: Json, text: dict[str, str]) -> list[Json]:
    available = {p.key: p for p in slots(document)}
    if set(text) != set(available):
        raise ValueError("edit_scope_mismatch")
    requests: list[Json] = []
    for key in sorted(text, key=lambda key: available[key].start, reverse=True):
        p, value = available[key], text[key]
        if any(ord(c) < 32 for c in value):
            raise ValueError("paragraph_control_character")
        if p.text == value + "\n":
            continue
        base: Json = {"tabId": p.tab_id} if p.tab_id else {}
        if p.segment_id:
            base["segmentId"] = p.segment_id
        length = len(p.text[:-1].encode("utf-16-le")) // 2
        if length:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            **base,
                            "startIndex": p.start,
                            "endIndex": p.start + length,
                        }
                    }
                }
            )
        if value:
            requests.append({"insertText": {"location": {**base, "index": p.start}, "text": value}})
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {
                            **base,
                            "startIndex": p.start,
                            "endIndex": p.start + len(value.encode("utf-16-le")) // 2,
                        },
                        "textStyle": p.style,
                        "fields": "*",
                    }
                }
            )
    return requests


def validate_edit(before: Json, after: Json, text: dict[str, str]) -> None:
    if signature(before, edited=True) != signature(after, edited=True):
        raise ValueError("native_template_format_or_protected_content_changed")
    actual = {p.key: p.text[:-1] for p in slots(after)}
    if actual != text:
        raise ValueError("native_document_content_mismatch")
    expected_styles = {p.key: p.style for p in slots(before)}
    for p in slots(after):
        for element in p.paragraph["elements"]:
            run = element["textRun"]
            if run["content"].strip() and run.get("textStyle", {}) != expected_styles[p.key]:
                raise ValueError("generated_text_style_changed")
