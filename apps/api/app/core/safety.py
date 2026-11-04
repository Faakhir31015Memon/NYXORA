"""Safety Engine: prompt-injection defense, secret-leak detection, refusal policy.

The one rule everything else follows: text that arrived from a document, a tool
result, a web page, or a model is DATA. It can never grant permission, change
policy, or issue a command.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel

INJECTION_PATTERNS: tuple[str, ...] = (
    r"ignore\s+(all\s+|the\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)",
    r"you\s+are\s+now\s+in\s+(admin|developer|dan|jailbreak|god)\s*mode",
    r"pretend\s+(that\s+)?you\s+have\s+no\s+(restrictions|rules|limits)",
    r"override\s+(the\s+)?(safety|security|permission)\s*(policy|controls|system)?",
    r"^\s*(system|assistant)\s*:\s*",
    r"delete\s+all\s+(files|data|everything)",
    r"rm\s+-rf\s+/",
    r"exfiltrate|send\s+(the\s+)?( secrets| api keys?)\s+to\s+",
    r"grant\s+(yourself|the\s+assistant)\s+(all\s+)?permissions",
    r"new\s+instructions\s*:",
    r"do\s+not\s+tell\s+the\s+user",
    r"reveal\s+(your\s+)?(system\s+prompt|hidden\s+instructions)",
)

_INJECTION_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in INJECTION_PATTERNS]

HARMFUL_REQUEST_PATTERNS: tuple[str, ...] = (
    r"(?i)\b(build|make|construct|assemble)\b[\s\S]{0,30}\b(bomb|ied|explosive device|pipe gun)\b",
    r"(?i)\bbomb\s+(construction|recipe|instructions)\b",
    r"(?i)\bchemical\s+weapon\b",
    r"(?i)\bchild\s*(sexual|abuse|porn)",
    r"(?i)\bsteal\s+(credit\s+card|identity)\s+data\b",
    r"(?i)\bmoney\s+laundering\s+how\s+to\b",
    r"(?i)\bransomware\b[\s\S]{0,40}\b(source|code|write|build)\b",
    r"(?i)\b(write|build|generate)\b[\s\S]{0,30}\bmalware\b",
)
_HARMFUL_RE = [re.compile(p, re.IGNORECASE) for p in HARMFUL_REQUEST_PATTERNS]

_SECRET_ECHO_RE = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

UNTRUSTED_OPEN = '<untrusted_data source="{src}">'
UNTRUSTED_CLOSE = "</untrusted_data>"


class SafetyVerdict(StrEnum):
    CLEAN = "clean"
    INJECTION_SUSPECTED = "injection_suspected"
    SECRET_LEAK = "secret_leak"
    REFUSE = "refuse"


class SafetyReport(BaseModel):
    verdict: SafetyVerdict
    matched: list[str] = []
    action_taken: str = "none"
    detail: str | None = None


def scan_for_injection(text: str) -> SafetyReport:
    hits = [p.pattern for p in _INJECTION_RE if p.search(text or "")]
    if hits:
        return SafetyReport(
            verdict=SafetyVerdict.INJECTION_SUSPECTED,
            matched=hits,
            action_taken="quarantined_as_data",
            detail="Instruction-like text found in untrusted content. It is treated as data "
            "and cannot grant permission or alter policy.",
        )
    return SafetyReport(verdict=SafetyVerdict.CLEAN)


def scan_for_harmful_request(text: str) -> SafetyReport:
    hits = [p.pattern for p in _HARMFUL_RE if p.search(text or "")]
    if hits:
        return SafetyReport(
            verdict=SafetyVerdict.REFUSE,
            matched=hits,
            action_taken="refused",
            detail="This request is outside what NYXORA will help with.",
        )
    return SafetyReport(verdict=SafetyVerdict.CLEAN)


def scan_output_for_secrets(text: str) -> SafetyReport:
    hits = [p.pattern for p in _SECRET_ECHO_RE if p.search(text or "")]
    if hits:
        return SafetyReport(
            verdict=SafetyVerdict.SECRET_LEAK,
            matched=hits,
            action_taken="redacted",
            detail="A credential-shaped string appeared in model output and was redacted.",
        )
    return SafetyReport(verdict=SafetyVerdict.CLEAN)


def wrap_untrusted(text: str, source: str) -> str:
    """Label retrieved content so the model cannot confuse it with instructions."""
    safe_source = re.sub(r"[^A-Za-z0-9._\-/]", "_", source)[:80]
    sanitized = strip_control(text)
    return f"{UNTRUSTED_OPEN.format(src=safe_source)}\n{sanitized}\n{UNTRUSTED_CLOSE}"


def strip_control(text: str) -> str:
    """Drop non-printables and obvious instruction-granting preambles from documents."""
    cleaned = "".join(ch for ch in (text or "") if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    for pattern in _INJECTION_RE:
        cleaned = pattern.sub("[redacted-instruction-pattern]", cleaned)
    return cleaned


def sanitize_tool_output(text: str, tool_name: str) -> str:
    """Tool results are untrusted too - route them through the same labeling."""
    return wrap_untrusted(text, f"tool:{tool_name}")
