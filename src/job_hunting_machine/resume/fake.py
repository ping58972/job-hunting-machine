"""Restart-safe local Google Docs fake. PDF rendering is injected by the test harness."""

import copy
import json
from collections.abc import Callable
from pathlib import Path

from job_hunting_machine.models.prompts import digest
from job_hunting_machine.resume.documents import identifier
from job_hunting_machine.resume.native import Json, edit_requests, normalize, paragraphs
from job_hunting_machine.security.paths import PathGuard


class FakeDocuments:
    def __init__(self, root: Path, exporter: Callable[[Json], bytes]) -> None:
        self.root = PathGuard().mkdir(root, parents=True, exist_ok=True)
        self.exporter = exporter
        self.copies = 0
        self.edits = 0

    def put(self, document: Json) -> None:
        PathGuard().write_text(
            self.root / f"{identifier(document['documentId'])}.json", json.dumps(document)
        )

    def get(self, document_id: str) -> Json:
        value: Json = json.loads(
            PathGuard().validate_write(self.root / f"{identifier(document_id)}.json").read_text()
        )
        return value

    def find(self, key: str) -> str | None:
        path = self.root / f"fake_{identifier(key)}.json"
        return f"fake_{key}" if PathGuard().validate_write(path).exists() else None

    def copy(self, source_id: str, title: str, key: str) -> str:
        document = copy.deepcopy(self.get(source_id))
        document.update(documentId=f"fake_{identifier(key)}", title=title, revisionId="initial")
        self.put(document)
        self.copies += 1
        return str(document["documentId"])

    def create(self, title: str, key: str) -> str:
        document = normalize(
            {
                "documentId": f"fake_{identifier(key)}",
                "title": title,
                "revisionId": "initial",
                "body": {
                    "content": [
                        {
                            "startIndex": 1,
                            "endIndex": 2,
                            "paragraph": {
                                "elements": [{"textRun": {"content": "\n", "textStyle": {}}}],
                            },
                        }
                    ]
                },
            }
        )
        self.put(document)
        self.copies += 1
        return str(document["documentId"])

    def _current(self, document: Json) -> Json:
        current = self.get(document["documentId"])
        if current["revisionId"] != document["revisionId"]:
            raise ValueError("fake_revision_conflict")
        return current

    def edit(self, document: Json, text: dict[str, str]) -> None:
        current = self._current(document)
        edit_requests(document, text)  # Exercise the same edit-scope validation as HTTP.
        for p in paragraphs(current):
            if p.key in text:
                p.paragraph["elements"] = [
                    {"textRun": {"content": text[p.key] + "\n", "textStyle": p.style}}
                ]
        current["revisionId"] = digest(current)
        self.put(current)
        self.edits += 1

    def fill(self, document: Json, text: str) -> None:
        current = self._current(document)
        current["tabs"][0]["body"]["content"] = [
            {
                "startIndex": 1,
                "endIndex": len(text) + 2,
                "paragraph": {
                    "elements": [{"textRun": {"content": text + "\n", "textStyle": {}}}],
                },
            }
        ]
        current["revisionId"] = digest(current)
        self.put(current)
        self.edits += 1

    def export(self, document_id: str) -> bytes:
        return self.exporter(self.get(document_id))
