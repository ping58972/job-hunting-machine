"""Bounded Gmail search/read transport. It contains no write API."""

import base64
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from job_hunting_machine.monitor.types import GmailMessage
from job_hunting_machine.orchestration.queue import RetryableError


@dataclass
class FakeGmailReader:
    messages: list[GmailMessage] = field(default_factory=list)
    searches: list[tuple[str, int]] = field(default_factory=list)

    async def search(self, query: str, *, limit: int) -> list[GmailMessage]:
        self.searches.append((query, limit))
        return self.messages[:limit]


def _decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _plain_text(payload: dict[str, Any]) -> str:
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data")
        return _decode(data) if isinstance(data, str) else ""
    parts = payload.get("parts")
    if not isinstance(parts, list):
        data = payload.get("body", {}).get("data")
        return _decode(data) if isinstance(data, str) else ""
    return "\n".join(_plain_text(item) for item in parts if isinstance(item, dict))


def _header(payload: dict[str, Any], name: str) -> str:
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return ""
    for item in headers:
        if (
            isinstance(item, dict)
            and str(item.get("name", "")).casefold() == name.casefold()
            and isinstance(item.get("value"), str)
        ):
            return str(item["value"])
    return ""


class GmailRestReader:
    """Read-only Gmail REST client with independent explicit live authorization."""

    def __init__(self, access_token: str | None = None) -> None:
        token = access_token or os.environ.get("GMAIL_MONITOR_ACCESS_TOKEN")
        if os.environ.get("GMAIL_MONITOR_ALLOW_LIVE") != "1" or not token:
            raise ValueError("live_gmail_monitor_requires_opt_in_and_token")
        self._token = token
        self._base = "https://gmail.googleapis.com/gmail/v1/users/me"

    async def search(self, query: str, *, limit: int) -> list[GmailMessage]:
        if not 1 <= limit <= 100:
            raise ValueError("gmail_monitor_limit_invalid")
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.get(
                    f"{self._base}/messages",
                    headers=headers,
                    params={"q": query, "maxResults": limit},
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise RetryableError("gmail_monitor_transient")
                response.raise_for_status()
                summaries = response.json().get("messages", [])
                results: list[GmailMessage] = []
                for summary in summaries[:limit] if isinstance(summaries, list) else []:
                    if not isinstance(summary, dict) or not isinstance(summary.get("id"), str):
                        continue
                    detail = await client.get(
                        f"{self._base}/messages/{summary['id']}",
                        headers=headers,
                        params={"format": "full"},
                    )
                    if detail.status_code == 429 or detail.status_code >= 500:
                        raise RetryableError("gmail_monitor_transient")
                    detail.raise_for_status()
                    item = detail.json()
                    payload = item.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    body = _plain_text(payload)
                    results.append(
                        GmailMessage(
                            provider_id=str(item["id"]),
                            thread_id=str(item["threadId"])
                            if isinstance(item.get("threadId"), str)
                            else None,
                            sender=_header(payload, "From")[:1000],
                            subject=_header(payload, "Subject")[:2000],
                            body=body[:200_000],
                            received_at=_header(payload, "Date")[:200],
                        )
                    )
                return results
        except RetryableError:
            raise
        except httpx.HTTPError:
            raise RetryableError("gmail_monitor_transport") from None
