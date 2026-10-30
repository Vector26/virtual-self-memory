from __future__ import annotations

import mailbox
import re
from email.utils import parsedate_to_datetime
from pathlib import Path

from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id

_SUBJ = re.compile(r"^Subject:\s*(.+)$", re.I | re.M)
_FROM = re.compile(r"^From:\s*(.+)$", re.I | re.M)


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    out: list[ArchiveEvent] = []
    try:
        mbox = mailbox.mbox(str(path))
    except OSError:
        return []
    for i, msg in enumerate(mbox):
        subj = msg.get("Subject", "")
        frm = msg.get("From", "")
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")[:4000]
                    break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")[:4000]
        ts = None
        try:
            ts = parsedate_to_datetime(msg.get("Date", ""))
        except (TypeError, ValueError):
            ts = None
        actor = "self" if "@" in str(frm) and "me" in str(frm).lower() else "other"
        etype = "email_sent" if actor == "self" else "email_received"
        out.append(
            ArchiveEvent(
                event_id=new_event_id(),
                provider=provider,
                product=product,
                event_type=etype,
                ts=ts,
                actor=actor,
                title=str(subj)[:500],
                text=body[:4000] if body else None,
                source_file=rel,
                source_ref=f"{rel}#{i}",
            )
        )
    return out
