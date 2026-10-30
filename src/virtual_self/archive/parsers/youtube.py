from __future__ import annotations

import json
from pathlib import Path

from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id, parse_ts


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return []
    rows = data if isinstance(data, list) else data.get("Array") or []
    out: list[ArchiveEvent] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("snippet", {}).get("title")
        ts = parse_ts(row.get("time") or row.get("timestamp"))
        out.append(
            ArchiveEvent(
                event_id=new_event_id(),
                provider=provider,
                product=product,
                event_type="watch",
                ts=ts,
                actor="self",
                title=str(title)[:500] if title else None,
                url=str(row.get("titleUrl") or row.get("url") or "")[:2000] or None,
                source_file=rel,
                source_ref=f"{rel}#{i}",
            )
        )
    return out
