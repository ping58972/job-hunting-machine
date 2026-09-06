"""Root-local immutable artifact bundles and deterministic PDF validation."""

import hashlib
import io
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from job_hunting_machine.resume.errors import ReviewRequired
from job_hunting_machine.security.paths import PathGuard


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def filename_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", unicodedata.normalize("NFKD", value)).strip("_")
    if not value:
        raise ReviewRequired("company_or_position_requires_filename_review")
    return value[:80]


def pdf_pages(data: bytes, expected_text: list[str] | None = None) -> int:
    """Parse the actual PDF page tree and verify generated text survived export.

    Text checking additionally rejects one-page exports that silently clip header
    content. Unextractable fonts/exports fail closed for human review.
    """
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted_pdf")
        count = len(reader.pages)
        text = "".join(page.extract_text() for page in reader.pages)
    except Exception:
        raise ReviewRequired("invalid_pdf_export") from None

    def compact(value: str) -> str:
        return "".join(unicodedata.normalize("NFKC", value).split())

    if not count or any(
        compact(value) not in compact(text) for value in (expected_text or []) if value.strip()
    ):
        raise ReviewRequired("pdf_content_missing_or_clipped")
    return count


def immutable(path: Path, data: bytes) -> dict[str, str]:
    guard = PathGuard()
    path = guard.validate_write(path)
    guard.mkdir(path.parent, parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise ReviewRequired("artifact_path_content_conflict")
    guard.write_bytes(path, data)
    return {"path": str(path), "sha256": sha256(data)}


def read_record(record: dict[str, Any]) -> bytes:
    content = PathGuard().validate_write(record["path"]).read_bytes()
    if sha256(content) != record["sha256"]:
        raise ReviewRequired("artifact_content_changed")
    return content


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
