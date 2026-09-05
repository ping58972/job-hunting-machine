"""Opt-in Slack Bolt Socket Mode transport. Durable receive precedes acknowledgment."""

import logging
import os
import signal
from threading import Event
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_bolt.authorization import AuthorizeResult
from slack_sdk import WebClient

from job_hunting_machine.database.engine import Database
from job_hunting_machine.slack.actions import ExternalActionService
from job_hunting_machine.slack.adapter import _SlackWebAdapter
from job_hunting_machine.slack.config import SlackInputError, SlackSettings, load_slack_settings
from job_hunting_machine.slack.control import SlackControlPlane


def build_bolt_app(control: SlackControlPlane, *, client: WebClient | None = None) -> App:
    # Socket Mode validates the connection with the app token. This app is not an HTTP endpoint.
    # Supplying authorize prevents Bolt's implicit auth.test call during offline tests.
    def authorize(team_id: str, **kwargs: Any) -> AuthorizeResult:
        return AuthorizeResult(
            enterprise_id=None, team_id=team_id, bot_token=client.token if client else "fake"
        )

    logger = logging.getLogger("jhm_slack_transport")
    logger.disabled = True  # SDK diagnostics can contain raw messages, tokens or response URLs.
    app = App(
        client=client or WebClient(token="fake", retry_handlers=[], logger=logger),
        authorize=authorize,
        process_before_response=True,
        logger=logger,
        token_verification_enabled=False,
    )

    def receive(ack: Any, body: dict[str, Any]) -> None:
        try:
            control.receive(body)
        except SlackInputError:
            ack()  # Permanently malformed/unauthorized delivery: no raw text echoed.
            return
        # Storage errors deliberately escape without a successful acknowledgment.
        ack()

    app.event("message")(receive)
    app.event("app_mention")(receive)
    for action in ("jhm_approve", "jhm_reject", "jhm_answer"):
        app.action(action)(receive)
    return app


def run_live(database: Database, *, settings: SlackSettings | None = None) -> None:
    if os.environ.get("SLACK_ALLOW_LIVE") != "1":
        raise SlackInputError("live_slack_requires_SLACK_ALLOW_LIVE_1")
    settings = settings or load_slack_settings()
    bot_token, app_token = (
        os.environ.get("SLACK_BOT_TOKEN", ""),
        os.environ.get("SLACK_APP_TOKEN", ""),
    )
    if not (
        bot_token.startswith("xoxb-")
        and app_token.startswith("xapp-")
        and settings.team_id
        and settings.app_id
        and settings.authorized_user_ids
        and settings.notification_channel_id
    ):
        raise SlackInputError("live_slack_configuration_incomplete")
    logger = logging.getLogger("jhm_slack_transport")
    logger.disabled = True
    client = WebClient(token=bot_token, timeout=15, retry_handlers=[], logger=logger)
    actions = ExternalActionService(database, settings, adapter=_SlackWebAdapter(client))
    control = SlackControlPlane(database, settings, actions=actions)
    bolt = build_bolt_app(control, client=client)
    handler = SocketModeHandler(bolt, app_token=app_token, logger=logger)
    stopping = Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: stopping.set())
        control.recover()
        handler.connect()  # type: ignore[no-untyped-call]  # Bolt omits the return annotation.
        while not stopping.is_set():
            control.recover()
            control.process_one()
            actions.deliver_one()
            stopping.wait(0.5)
    finally:
        handler.close()  # type: ignore[no-untyped-call]  # Bolt omits the return annotation.
        for sig, old in previous.items():
            signal.signal(sig, old)
