from __future__ import annotations

import re
from dataclasses import dataclass

from virtual_self.archive.parsers.base import ArchiveEvent

FINANCE_RE = re.compile(
    r"\b(invoice|payment|paypal|stripe|bank|otp|2fa|password|ssn|aadhaar|pan card|"
    r"credit card|debit|upi|ifsc)\b",
    re.I,
)
HEALTH_RE = re.compile(r"\b(diagnosis|prescription|lab result|blood pressure|bmi)\b", re.I)
GOV_ID_RE = re.compile(r"\b(passport|driver.?s license|voter id)\b", re.I)

RECEIVED_MAIL_RE = re.compile(r"^Received:", re.I)


@dataclass
class PolicyResult:
    include: bool
    sensitivity: int
    reason: str | None = None


def classify_event(event: ArchiveEvent) -> PolicyResult:
    text = f"{event.title or ''} {event.text or ''} {event.url or ''}"
    if event.product == "fit":
        return PolicyResult(include=True, sensitivity=3, reason="health_fit_private")
    if FINANCE_RE.search(text):
        return PolicyResult(include=False, sensitivity=3, reason="finance_excluded")
    if HEALTH_RE.search(text):
        return PolicyResult(include=False, sensitivity=3, reason="health_excluded")
    if GOV_ID_RE.search(text):
        return PolicyResult(include=False, sensitivity=3, reason="gov_id_excluded")
    if event.event_type == "email_received" or (
        event.actor == "other" and event.product == "mail"
    ):
        return PolicyResult(include=True, sensitivity=3, reason="third_party_mail")
    if event.actor == "other" and event.product == "messages":
        return PolicyResult(include=True, sensitivity=3, reason="third_party_message")
    if event.product == "contacts" and event.text:
        return PolicyResult(include=True, sensitivity=2, reason="contact_name_only")
    return PolicyResult(include=True, sensitivity=1, reason=None)


def apply_policy(events: list[ArchiveEvent]) -> tuple[list[ArchiveEvent], dict[str, int]]:
    kept: list[ArchiveEvent] = []
    skipped: dict[str, int] = {}
    for ev in events:
        pr = classify_event(ev)
        if not pr.include:
            skipped[pr.reason or "excluded"] = skipped.get(pr.reason or "excluded", 0) + 1
            continue
        ev.sensitivity = pr.sensitivity
        kept.append(ev)
    return kept, skipped
