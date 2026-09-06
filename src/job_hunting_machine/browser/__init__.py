"""Phase 8 browser preparation APIs. Final submission is intentionally absent."""

from job_hunting_machine.browser.detector import adapter_for, detect_ats
from job_hunting_machine.browser.manager import BrowserManager, BrowserSessionHandle
from job_hunting_machine.browser.types import (
    ATSKind,
    BaseAdapter,
    ChallengeKind,
    FinalSubmissionForbidden,
    HumanBrowserRequired,
    PageSnapshot,
    SemanticField,
    UploadFile,
)

__all__ = [
    "ATSKind",
    "BaseAdapter",
    "BrowserManager",
    "BrowserSessionHandle",
    "ChallengeKind",
    "FinalSubmissionForbidden",
    "HumanBrowserRequired",
    "PageSnapshot",
    "SemanticField",
    "UploadFile",
    "adapter_for",
    "detect_ats",
]
