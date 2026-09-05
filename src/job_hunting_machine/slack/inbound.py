"""Normalize untrusted Slack envelopes before persistence; never store tokens/response URLs."""

import json
import re
from typing import Any
from urllib.parse import urlsplit

from job_hunting_machine.slack.config import SlackInputError, SlackSettings

_ID = re.compile(r"^[A-Z][A-Z0-9]{1,63}$")
_TS = re.compile(r"^\d{10,16}\.\d{1,6}$")
_SECRET = re.compile(
    r"(?i)(xox[baprs]-|xapp-|sk-[a-z0-9_-]{12,}|-----BEGIN|"
    r"password\s*[:=]|secret\s*[:=]|(?:api[_-]?key|access[_-]?token)\s*[:=]|"
    r"authorization\s*:|bearer\s+[a-z0-9]|cookie\s*[:=])"
)


def safe_answer(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2000
        or _SECRET.search(value)
    ):
        raise SlackInputError("answer_requires_local_entry")
    return value.strip()


def _string(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SlackInputError("malformed_slack_interaction")
    return value


def normalize(body: dict[str, Any], settings: SlackSettings) -> dict[str, Any]:
    try:
        if len(json.dumps(body, allow_nan=False).encode()) > 128000:
            raise SlackInputError("slack_payload_too_large")
        kind = body["type"]
        team = _string(body["team"]["id"] if kind == "block_actions" else body["team_id"], _ID)
        app = _string(body["api_app_id"], _ID)
        if kind == "event_callback":
            event = body["event"]
            if (
                event.get("type") not in {"message", "app_mention"}
                or event.get("subtype")
                or event.get("bot_id")
            ):
                raise SlackInputError("unsupported_slack_event")
            user = _string(event["user"], _ID)
            channel = _string(event["channel"], _ID)
            stamp = _string(event["ts"], _TS)
            settings.authorize(team=team, app=app, channel=channel, user=user)
            text = event.get("text", "")
            if not isinstance(text, str):
                raise SlackInputError("malformed_slack_message")
            urls: list[str] = []
            for found in re.findall(r"https?://[^\s<>|]+", text):
                url = found.rstrip(".,;!?)")
                parts = urlsplit(url)
                if (
                    not parts.hostname
                    or parts.username
                    or parts.password
                    or len(url) > 4096
                    or _SECRET.search(url)
                    or re.search(r"(?i)[?&](token|key|password|secret|signature|sig|code)=", url)
                ):
                    continue
                if url not in urls:
                    urls.append(url)
                if len(urls) >= 25:
                    break
            return {
                "kind": "urls",
                "provider_event_id": _string(
                    body["event_id"], re.compile(r"^[A-Za-z0-9_-]{1,128}$")
                ),
                "team": team,
                "app": app,
                "channel": channel,
                "user": user,
                "message_ts": stamp,
                "thread_ts": _string(event["thread_ts"], _TS) if event.get("thread_ts") else None,
                "urls": urls,
            }
        if kind != "block_actions" or len(body["actions"]) != 1:
            raise SlackInputError("malformed_slack_interaction")
        action = body["actions"][0]
        if action["type"] != "button" or action["action_id"] not in {
            "jhm_approve",
            "jhm_reject",
            "jhm_answer",
        }:
            raise SlackInputError("unsupported_slack_action")
        user = _string(body["user"]["id"], _ID)
        channel = _string(body["channel"]["id"], _ID)
        stamp = _string(body["container"]["message_ts"], _TS)
        if (
            body["container"].get("type") != "message"
            or body["container"].get("channel_id") != channel
        ):
            raise SlackInputError("interaction_container_mismatch")
        settings.authorize(team=team, app=app, channel=channel, user=user)
        result = {
            "kind": action["action_id"],
            "team": team,
            "app": app,
            "channel": channel,
            "user": user,
            "message_ts": stamp,
            "action_ts": _string(action["action_ts"], _TS),
            "external_action_id": _string(
                action["value"], re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
            ),
        }
        if action["action_id"] == "jhm_answer":
            result["answer"] = safe_answer(
                body["state"]["values"]["jhm_answer_block"]["jhm_answer_input"]["value"]
            )
        return result
    except SlackInputError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SlackInputError("malformed_slack_interaction") from None
