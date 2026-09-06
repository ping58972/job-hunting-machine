"""Root-confined monitor evidence persistence."""

import hashlib
import json
from pathlib import Path

from job_hunting_machine.ids import IdKind, validate_id
from job_hunting_machine.monitor.types import GmailMessage, PortalPage
from job_hunting_machine.security.paths import PROJECT_ROOT, PathGuard


class MonitorEvidenceStore:
    def __init__(self, root: Path = PROJECT_ROOT / "evidence/monitor") -> None:
        self.root = PathGuard().validate_write(root)

    def _write(self, application_id: str, source: str, reference: str, value: object) -> str:
        validate_id(application_id, IdKind.APPLICATION)
        name = hashlib.sha256(reference.encode()).hexdigest()
        target = self.root / application_id / source.casefold() / f"{name}.json"
        content = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        guard = PathGuard()
        guard.mkdir(target.parent, parents=True, exist_ok=True)
        guard.write_text(target, content, encoding="utf-8")
        return str(target)

    def email(self, application_id: str, message: GmailMessage) -> str:
        return self._write(
            application_id,
            "EMAIL",
            message.provider_id,
            {
                "provider_id": message.provider_id,
                "thread_id": message.thread_id,
                "sender": message.sender,
                "subject": message.subject,
                "body": message.body,
                "received_at": message.received_at,
            },
        )

    def portal(self, application_id: str, page: PortalPage, reference: str) -> str:
        return self._write(
            application_id,
            "PORTAL",
            reference,
            {
                "url": page.url,
                "status_code": page.status_code,
                "vendor": page.vendor,
                "text": page.text,
            },
        )
