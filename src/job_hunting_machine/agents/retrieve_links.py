"""Deterministic Slack URL intake. No model or network access."""

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_hunting_machine.database.repositories import (
    JobCreate,
    JobRepository,
    TaskCreate,
    TaskRepository,
)
from job_hunting_machine.orchestration.queue import Lease, QueueService

TRACKING = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"}
SECRET_KEYS = {
    "token",
    "access_token",
    "key",
    "api_key",
    "password",
    "secret",
    "signature",
    "sig",
    "code",
}


def canonicalize(url: str) -> str:
    """Remove only known tracking parameters; preserve job identifiers and path case."""
    if len(url) > 4096 or re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError("invalid_job_url")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".").encode("idna").decode()
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not host
        or parts.username
        or parts.password
    ):
        raise ValueError("invalid_job_url")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("nonpublic_job_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("nonpublic_job_url") from None
    else:
        if not address.is_global:
            raise ValueError("nonpublic_job_url")
    query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=100)
    if any(key.lower() in SECRET_KEYS for key, _ in query):
        raise ValueError("credential_job_url")
    query = [
        (key, value)
        for key, value in query
        if key.lower() not in TRACKING and not key.lower().startswith("utm_")
    ]
    scheme = parts.scheme.lower()
    port = parts.port
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    # Decode unreserved percent escapes only; encoded slashes retain their meaning.
    path = re.sub(
        r"%([0-9a-fA-F]{2})",
        lambda m: (
            chr(int(m[1], 16))
            if chr(int(m[1], 16))
            in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
            else m[0].upper()
        ),
        parts.path or "/",
    )
    return urlunsplit((scheme, authority, path, urlencode(sorted(query)), ""))


def parse_slack_urls(text: str) -> list[str]:
    if len(text) > 128_000:
        raise ValueError("intake_too_large")
    return list(dict.fromkeys(re.findall(r"https?://[^\s<>|]+", text)))[:25]


def retrieve(queue: QueueService, lease: Lease) -> list[str]:
    """Create NEW jobs and one qualification task per URL atomically under the lease."""
    values = lease.payload.get("urls", [])
    if (
        not isinstance(values, list)
        or len(values) > 25
        or not all(isinstance(v, str) for v in values)
    ):
        raise ValueError("invalid_intake_payload")
    jobs: list[str] = []
    with queue.fence(lease) as session:
        repository = JobRepository(session, queue.clock)
        for raw in values:
            for url in parse_slack_urls(str(raw)):
                try:
                    normalized = canonicalize(url)
                except ValueError:
                    continue
                job = repository.create(
                    JobCreate(
                        url,
                        normalized,
                        "SLACK",
                        source_reference=str(lease.payload.get("slack_event_id", lease.task_id)),
                    )
                )
                TaskRepository(session, queue.clock).create(
                    TaskCreate(
                        "QUALIFY_JOB",
                        job_id=job.job_id,
                        task_status="READY",
                        dedupe_key=f"qualify_job:{job.job_id}",
                        payload={"job_id": job.job_id},
                    )
                )
                if job.job_id not in jobs:
                    jobs.append(job.job_id)
    return jobs
