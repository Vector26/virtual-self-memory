from __future__ import annotations

import json
from pathlib import Path

from virtual_self.archive.detect import repair_meta_text
from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id, parse_ts


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    try:
        raw = path.read_bytes()
        text = repair_meta_text(raw)
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return []
    messages = data.get("messages") or []
    out: list[ArchiveEvent] = []
    for i, row in enumerate(messages):
        if not isinstance(row, dict):
            continue
        sender = row.get("sender_name") or ""
        content = row.get("content") or ""
        ts = parse_ts(row.get("timestamp_ms") or row.get("timestamp"))
        actor = "self" if sender and "you" in str(sender).lower() else "other"
        out.append(
            ArchiveEvent(
                event_id=new_event_id(),
                provider=provider,
                product=product,
                event_type="message",
                ts=ts,
                actor=actor,
                title=str(sender)[:200],
                text=str(content)[:4000] if content else None,
                source_file=rel,
                source_ref=f"{rel}#{i}",
            )
        )
    return out
