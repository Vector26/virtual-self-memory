from __future__ import annotations

import json
from pathlib import Path

from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id, parse_ts


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    notes = data if isinstance(data, list) else [data]
    out: list[ArchiveEvent] = []
    for i, row in enumerate(notes):
        if not isinstance(row, dict):
            continue
        text = row.get("textContent") or row.get("text") or row.get("title")
        ts = parse_ts(row.get("createdTimestampUsec") or row.get("userEditedTimestampUsec"))
        out.append(
            ArchiveEvent(
                event_id=new_event_id(),
                provider=provider,
                product=product,
                event_type="note",
                ts=ts,
                actor="self",
                title=str(row.get("title") or "")[:200] or None,
                text=str(text)[:8000] if text else None,
                source_file=rel,
                source_ref=f"{rel}#{i}",
            )
        )
    return out
