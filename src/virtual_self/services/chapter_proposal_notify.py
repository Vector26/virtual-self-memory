"""configured notifier copy for proposed life chapters (T-5)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from virtual_self.observability.notify import NotifyCategory, NotifySeverity, notify_user


def _short_id(chapter_id: UUID) -> str:
    return str(chapter_id).split("-", 1)[0]


def _span(valid_from: datetime | None, valid_to: datetime | None) -> str:
    if valid_from is None and valid_to is None:
        return "dates unknown"
    start = valid_from.date().isoformat() if valid_from else "?"
    end = valid_to.date().isoformat() if valid_to else "present"
    return f"{start}–{end}"


def format_chapter_proposal_message(
    *,
    chapter_id: UUID,
    strand: str,
    title: str,
    summary: str | None,
    valid_from: datetime | None,
    valid_to: datetime | None,
    evidence_count: int,
) -> str:
    short = _short_id(chapter_id)
    summary_bit = f" — {summary}" if summary else ""
    return (
        f"chapter proposal {short}: {strand} — \"{title}\"{summary_bit} "
        f"({_span(valid_from, valid_to)}, {evidence_count} memories). "
        f"/approve {short} · /reject {short} · /edit {short} <new title>"
    )


def format_personality_proposal_message(
    *,
    proposal_id: UUID,
    facet_type: str,
    proposed_content: str,
    origin_mode: str | None,
    reason: str | None = None,
) -> str:
    short = _short_id(proposal_id)
    mode = (origin_mode or "Default").strip()
    reason_bit = f" — {reason}" if reason else ""
    snippet = proposed_content.strip().replace("\n", " ")[:160]
    return (
        f"proposal {short}: {facet_type} — {snippet}{reason_bit}. "
        f"/approve {short} · /reject {short} · /edit {short} <revised text>"
    )


def notify_personality_proposal(
    *,
    proposal_id: UUID,
    facet_type: str,
    proposed_content: str,
    origin_mode: str | None,
    reason: str | None = None,
) -> bool:
    mode = (origin_mode or "Default").strip()
    body = format_personality_proposal_message(
        proposal_id=proposal_id,
        facet_type=facet_type,
        proposed_content=proposed_content,
        origin_mode=mode,
        reason=reason,
    )
    return notify_user(
        body,
        category=NotifyCategory.DEFAULT,
        severity=NotifySeverity.USER_ACTIONABLE,
        source_label=mode,
    )


def notify_chapter_proposal(
    *,
    chapter_id: UUID,
    strand: str,
    title: str,
    summary: str | None,
    valid_from: datetime | None,
    valid_to: datetime | None,
    evidence_count: int,
) -> bool:
    body = format_chapter_proposal_message(
        chapter_id=chapter_id,
        strand=strand,
        title=title,
        summary=summary,
        valid_from=valid_from,
        valid_to=valid_to,
        evidence_count=evidence_count,
    )
    return notify_user(
        body,
        category=NotifyCategory.CHAPTER_PROPOSAL,
        severity=NotifySeverity.USER_ACTIONABLE,
        source_label="Self",
    )
