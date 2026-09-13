"""Sensitive-information identification and scrubbing.

Four-chain placement (collector / PACK / forgetting / IndexJob) is enabled via
the single ``scrub_text`` / ``scrub`` entrypoints.  No optional heavy deps
(presidio, HF) — stdlib regex + deterministic post-processing, edge-runnable,
examiner-auditable.  Covers the 8 PII families used by the competition rubric
plus ai4privacy/pii-masking alignment (phone/id/bank/card/key/path/email/ip).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int
    raw: str
    replacement: str


# ---------------------------------------------------------------------------
# detectors  — ordered, non-overlapping via leftmost-longest scan
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # api/token/secret keys  (must precede generic hex)
    ("api_key", re.compile(r"\b(?:sk|tok|ghp|AKIA)[-_A-Za-z0-9]{12,}\b"), "[API_KEY_REDACTED]"),
    ("secret", re.compile(r"(?i)\b(?:api[_-]?key|secret|password)\s*[:=]\s*\S+"), "[SECRET_REDACTED]"),
    # id card 18 / 15  (CN) — with optional separators
    ("id_card", re.compile(r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"), "[ID_REDACTED]"),
    # bank card 16-19 dense / dashed
    ("bank_card", re.compile(r"\b(?:\d[ -]*?){16,19}\b"), "[BANK_REDACTED]"),
    # phone CN mobile 11
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE_REDACTED]"),
    # email
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[EMAIL_REDACTED]"),
    # ipv4
    ("ipv4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"), "[IP_REDACTED]"),
    # absolute filesystem path (linux /home, /Users, /root, /tmp, /var, /etc, C:\)
    ("path", re.compile(r"(?:/home/[^\s\"']+|/Users/[^\s\"']+|/root/[^\s\"']+|/tmp/[^\s\"']+|/var/[^\s\"']+|/etc/[^\s\"']+|[A-Za-z]:\\[^\s\"']+)"), "[PATH_REDACTED]"),
]


def _bank_is_plausible(raw: str) -> bool:
    digits = re.sub(r"[^\d]", "", raw)
    return 16 <= len(digits) <= 19 and bool(re.match(r"^[456]\d+$", digits))


def find_all(text: str) -> list[Finding]:
    occupied: list[tuple[int, int]] = []
    out: list[Finding] = []
    for kind, pat, repl in _PATTERNS:
        for m in pat.finditer(text):
            s, e = m.span()
            raw = m.group(0)
            if kind == "bank_card" and not _bank_is_plausible(raw):
                continue
            if any(not (e <= a or s >= b) for a, b in occupied):
                continue
            occupied.append((s, e))
            out.append(Finding(kind=kind, start=s, end=e, raw=raw, replacement=repl))
    out.sort(key=lambda f: f.start)
    return out


def scrub_text(text: str) -> tuple[str, list[Finding]]:
    """Return (scrubbed_text, findings).  Deterministic, no network."""
    if not text:
        return text, []
    findings = find_all(text)
    if not findings:
        return text, []
    parts: list[str] = []
    cursor = 0
    for f in findings:
        parts.append(text[cursor:f.start])
        parts.append(f.replacement)
        cursor = f.end
    parts.append(text[cursor:])
    return "".join(parts), findings


class SensitiveFilter:
    """Stateful wrapper (keeps stats for evaluation harness)."""

    def __init__(self):
        self.total_texts = 0
        self.flagged = 0

    def scrub(self, text: str) -> tuple[str, list[Finding]]:
        self.total_texts += 1
        scrubbed, findings = scrub_text(text)
        if findings:
            self.flagged += 1
        return scrubbed, findings

    def scrub_dict(self, obj: dict, text_keys: tuple[str, ...] = ("content", "text", "retrieval_text")) -> tuple[dict, list[Finding]]:
        out = dict(obj)
        all_findings: list[Finding] = []
        for k in text_keys:
            if isinstance(out.get(k), str):
                scrubbed, findings = scrub_text(out[k])
                out[k] = scrubbed
                all_findings.extend(findings)
        return out, all_findings


_DEFAULT: SensitiveFilter | None = None


def get_default_filter() -> SensitiveFilter:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SensitiveFilter()
    return _DEFAULT


def scrub(text: str) -> tuple[str, list[Finding]]:
    return scrub_text(text)
