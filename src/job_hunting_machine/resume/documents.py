"""Google Docs transport. Live construction is explicit; tests inject a local fake."""

import os
import re
from typing import Protocol

import httpx

from job_hunting_machine.orchestration.queue import RetryableError
from job_hunting_machine.resume.native import Json, edit_requests, normalize, paragraphs


class DocumentAdapter(Protocol):
    def get(self, document_id: str) -> Json: ...
    def find(self, key: str) -> str | None: ...
    def copy(self, source_id: str, title: str, key: str) -> str: ...
    def create(self, title: str, key: str) -> str: ...
    def edit(self, document: Json, text: dict[str, str]) -> None: ...
    def fill(self, document: Json, text: str) -> None: ...
    def export(self, document_id: str) -> bytes: ...


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", value):
        raise ValueError("invalid_google_document_id")
    return value


class GoogleDocsHTTP:
    """Native copy, revision-guarded edits, and PDF export. No credential persistence.

    All mutation methods are called by the document ExternalActionService. The
    destination folder must be explicitly supplied and must not be publicly shared.
    No permissions, sharing, email, browser, or submission endpoints are implemented.
    """

    def __init__(self, *, folder_id: str, client: httpx.Client | None = None) -> None:
        if os.environ.get("GOOGLE_DOCS_ALLOW_LIVE") != "1":
            raise ValueError("google_docs_live_requires_explicit_opt_in")
        token = os.environ.get("GOOGLE_ACCESS_TOKEN", "")
        if not token:
            raise ValueError("google_docs_access_token_required")
        self.folder_id = identifier(folder_id)
        self._client = client or httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
        self._headers = {"Authorization": f"Bearer {token}"}

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, service: str, path: str, **kwargs: object) -> httpx.Response:
        # Do not log HTTP payloads, credentials, source documents, or applicant facts.
        try:
            response = self._client.request(
                method,
                f"https://{service}.googleapis.com/{path}",
                headers=self._headers,
                **kwargs,  # type: ignore[arg-type]
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError:
            raise RetryableError("google_docs_transport_error") from None

    def get(self, document_id: str) -> Json:
        result: Json = self._request(
            "GET",
            "docs",
            f"v1/documents/{identifier(document_id)}",
            params={"includeTabsContent": "true"},
        ).json()
        return normalize(result)

    def find(self, key: str) -> str | None:
        identifier(key)
        value = self._request(
            "GET",
            "www",
            "drive/v3/files",
            params={
                "q": f"trashed = false and '{self.folder_id}' in parents and "
                f"appProperties has {{ key='jhm_action' and value='{key}' }}",
                "fields": "files(id),nextPageToken",
                "pageSize": 100,
            },
        ).json()
        if value.get("nextPageToken") or len(value["files"]) > 1:
            raise ValueError("ambiguous_google_document_copy")
        return str(value["files"][0]["id"]) if value["files"] else None

    def copy(self, source_id: str, title: str, key: str) -> str:
        value = self._request(
            "POST",
            "www",
            f"drive/v3/files/{identifier(source_id)}/copy",
            json={
                "name": title,
                "parents": [self.folder_id],
                "appProperties": {"jhm_action": identifier(key)},
            },
        ).json()
        return str(value["id"])

    def create(self, title: str, key: str) -> str:
        value = self._request(
            "POST",
            "www",
            "drive/v3/files",
            json={
                "name": title,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [self.folder_id],
                "appProperties": {"jhm_action": identifier(key)},
            },
        ).json()
        return str(value["id"])

    def _batch(self, document: Json, requests: list[Json]) -> None:
        if requests:
            self._request(
                "POST",
                "docs",
                f"v1/documents/{identifier(document['documentId'])}:batchUpdate",
                json={
                    "writeControl": {"requiredRevisionId": document["revisionId"]},
                    "requests": requests,
                },
            )

    def edit(self, document: Json, text: dict[str, str]) -> None:
        self._batch(document, edit_requests(document, text))

    def fill(self, document: Json, text: str) -> None:
        # Only a service-created cover letter; never a supplied template.
        tab_id = document["tabs"][0]["tabId"]
        current = "".join(p.text for p in paragraphs(document))
        base = {"tabId": tab_id} if tab_id else {}
        requests: list[Json] = []
        if current != "\n":
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            **base,
                            "startIndex": 1,
                            "endIndex": 1 + len(current[:-1].encode("utf-16-le")) // 2,
                        }
                    }
                }
            )
        self._batch(
            document,
            [
                *requests,
                {
                    "insertText": {
                        "location": {"index": 1, **({"tabId": tab_id} if tab_id else {})},
                        "text": text,
                    }
                },
            ],
        )

    def export(self, document_id: str) -> bytes:
        return self._request(
            "GET",
            "www",
            f"drive/v3/files/{identifier(document_id)}/export",
            params={"mimeType": "application/pdf"},
        ).content
