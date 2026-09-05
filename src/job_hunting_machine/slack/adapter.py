"""Slack Web API transport. Invoked only by ExternalActionService, never callbacks."""

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


@dataclass(frozen=True)
class Receipt:
    channel: str
    message_ts: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,63}", self.channel) or not re.fullmatch(
            r"\d{10,16}\.\d{1,6}", self.message_ts
        ):
            raise ValueError("Invalid Slack delivery receipt")


class DeliveryError(RuntimeError):
    def __init__(self, *, unknown: bool = True) -> None:
        super().__init__("slack_delivery_failed")
        self.unknown = unknown


class SlackAdapter(Protocol):
    def post(self, message: dict[str, Any], delivery_key: str) -> Receipt: ...


class FakeSlackAdapter:
    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, Receipt] = {}

    def post(self, message: dict[str, Any], delivery_key: str) -> Receipt:
        if delivery_key not in self.receipts:
            self.messages[delivery_key] = message
            self.receipts[delivery_key] = Receipt(
                message["channel"], f"1700000000.{len(self.messages):06d}"
            )
        return self.receipts[delivery_key]


class _SlackWebAdapter:
    def __init__(self, client: WebClient) -> None:
        self.client = client
        for name in ("slack_sdk", "slack_bolt"):
            logger = logging.getLogger(name)
            logger.setLevel(logging.CRITICAL + 1)
            logger.propagate = False

    def post(self, message: dict[str, Any], delivery_key: str) -> Receipt:
        try:
            provider_key = str(
                UUID(hex=hashlib.sha256(delivery_key.encode()).hexdigest()[:32], version=4)
            )
            response = self.client.chat_postMessage(**message, client_msg_id=provider_key)
            return Receipt(str(response["channel"]), str(response["ts"]))
        except SlackApiError:
            raise DeliveryError(unknown=False) from None
        except Exception:
            raise DeliveryError() from None
