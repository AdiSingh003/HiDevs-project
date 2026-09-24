"""Message-level Safe AI filters (deterministic, fail-closed).

These mirror the Lyzr RAI policy (keywords / prompt injection / toxicity / PII) so the arbiter works
offline; when Lyzr credentials are configured the RAI service runs as an additional layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.utility import PrivateNumber

LEAK_REPLACEMENT = "[redacted by Legal Arbiter: confidential mandate information]"
INJECTION_REPLACEMENT = "[neutralised by Legal Arbiter: instruction-like content removed]"
PII_REPLACEMENT = "[contact details redacted]"

# Phrase-level leaks need a *disclosure* ("our budget is ...", "our floor stands at ..."): neutral mentions such
# as "align with our budget" reveal nothing and are left alone (numbers matching private values always redact).
DISCLOSE = r"\s+(is|are|was|of|at|stands?|sits?|allows?|caps?|tops? out|would be|=|:)\b"
LEAK_PHRASES = re.compile(
    r"\b(walk[\s-]?away|reservation (price|value|point)|bottom[\s-]?line|batna|"
    r"(our|my) (absolute |internal |approved |real )?(maximum|max|ceiling|floor|minimum|limit|budget|budget cap|mandate)"
    + DISCLOSE + r"|"
    r"absolute (max|maximum|min|minimum|floor|ceiling)\b|"
    r"(lowest|highest|most) we can (go|do|pay|accept)|can(?:not|'t) go (any )?(below|above|lower|higher|beyond)|"
    r"break[\s-]?even|our (unit |production )?(cost|margin)s?" + DISCLOSE + r"|cost price|internal target)",
    re.IGNORECASE,
)

INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |any )?(of )?(the |your )?(previous|prior|above|earlier|preceding) (instructions|prompts|rules|messages|guidance)",
        r"disregard (all |any )?(of )?(the |your )?(previous|prior|above|system)? ?(instructions|rules|guidelines|policy|policies)",
        r"\b(system|developer|admin) (prompt|message|override|mode|instruction)",
        r"\byou are now\b",
        r"\bact as (?:an? )?(?:unrestricted|jailbroken|different)",
        r"reveal (your|the) (budget|limit|limits|reservation|walk[\s-]?away|instructions|system prompt|mandate|floor|ceiling)",
        r"(accept|approve|sign) (this|the|our) (offer|deal|contract|proposal) (immediately|right now|now|without (review|checks|question))",
        r"override (your |all |the )?(policy|policies|guardrails|limits|rules|mandate)",
        r"</?\s*(system|assistant|instructions?)\s*>",
        r"\[/?INST\]",
        r"\bBEGIN (SYSTEM|ADMIN|OVERRIDE)\b",
        r"\bjailbreak\b",
    )
]

TOXIC_TERMS = re.compile(
    r"\b(idiots?|stupid|morons?|clowns?|scammers?|liars?|cheats?|incompetent|pathetic|garbage|trash|shut up|damn)\b",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE_RE = re.compile(r"(?:\+\d[\d\s().-]{8,}\d)|\b\d{10}\b")

NUMBER_RE = re.compile(
    r"(?<![\w.])(?:USD|EUR|INR|GBP|US\$|\$|€|₹|£)?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(k|K|m|M|mn|million|thousand|lakh|crore)?(?![\w])"
)
MULTIPLIERS = {"k": 1e3, "K": 1e3, "thousand": 1e3, "m": 1e6, "M": 1e6, "mn": 1e6, "million": 1e6,
               "lakh": 1e5, "crore": 1e7}
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")  # never splits inside decimals such as 0.95


@dataclass
class Finding:
    rule_id: str
    original: str
    replacement: str
    reason: str


def extract_numbers(text: str) -> list[float]:
    out = []
    for m in NUMBER_RE.finditer(text):
        whole, frac, suffix = m.group(1), m.group(2) or "", m.group(3)
        value = float(whole.replace(",", "") + frac)
        if suffix:
            value *= MULTIPLIERS.get(suffix, 1.0)
        out.append(value)
    return out


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT.split(text) if s and s.strip()]


def _matches(value: float, candidates: list[PrivateNumber]) -> PrivateNumber | None:
    for pn in candidates:
        if abs(value - pn.value) <= pn.tolerance:
            return pn
    return None


def _is_public(value: float, public_numbers: list[float]) -> bool:
    for p in public_numbers:
        if abs(value - p) <= max(1e-6, abs(p) * 1e-4):
            return True
    return False


def detect_leaks(message: str, private: list[PrivateNumber], public_numbers: list[float]) -> tuple[str, list[Finding]]:
    """Redact sentences that disclose private mandate numbers or reservation language."""
    findings: list[Finding] = []
    clean = message
    for sentence in sentences(message):
        reason = None
        for value in extract_numbers(sentence):
            pn = _matches(value, private)
            if pn and not _is_public(value, public_numbers):
                reason = f"discloses private {pn.label}"
                break
        if reason is None and LEAK_PHRASES.search(sentence):
            reason = "reservation / mandate language"
        if reason:
            findings.append(Finding("SAFE-LEAK", sentence.strip(), LEAK_REPLACEMENT, reason))
            clean = clean.replace(sentence.strip(), LEAK_REPLACEMENT, 1)
    return clean, findings


def neutralise_injection(message: str) -> tuple[str, list[Finding]]:
    findings: list[Finding] = []
    clean = message
    for sentence in sentences(message):
        if any(p.search(sentence) for p in INJECTION_PATTERNS):
            findings.append(Finding("SAFE-INJECTION", sentence.strip(), INJECTION_REPLACEMENT,
                                    "prompt-injection attempt aimed at the counterpart agent"))
            clean = clean.replace(sentence.strip(), INJECTION_REPLACEMENT, 1)
    return clean, findings


def scrub_toxicity(message: str) -> tuple[str, list[Finding]]:
    findings: list[Finding] = []

    def _sub(m: re.Match[str]) -> str:
        findings.append(Finding("SAFE-TOXICITY", m.group(0), "***", "abusive language"))
        return "***"

    return TOXIC_TERMS.sub(_sub, message), findings


def scrub_pii(message: str) -> tuple[str, list[Finding]]:
    findings: list[Finding] = []

    def _sub(m: re.Match[str]) -> str:
        findings.append(Finding("SAFE-PII", m.group(0), PII_REPLACEMENT, "personal contact data"))
        return PII_REPLACEMENT

    message = EMAIL_RE.sub(_sub, message)
    message = PHONE_RE.sub(_sub, message)
    return message, findings


def sanitise_message(message: str, private: list[PrivateNumber], public_numbers: list[float]) -> tuple[str, list[Finding]]:
    """Full outbound pipeline: injection -> leaks -> PII -> toxicity."""
    findings: list[Finding] = []
    text = message or ""
    for step in (neutralise_injection,):
        text, found = step(text)
        findings += found
    text, found = detect_leaks(text, private, public_numbers)
    findings += found
    for step in (scrub_pii, scrub_toxicity):
        text, found = step(text)
        findings += found
    return text.strip(), findings
