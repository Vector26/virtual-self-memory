from __future__ import annotations

import json
from pathlib import Path

from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id, parse_ts


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[ArchiveEvent] = []
    parts = rel.split("/")
    sub_product = parts[1] if len(parts) > 2 else "activity"
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("titleUrl")
        ts = parse_ts(row.get("time") or row.get("timestamp"))
        text = row.get("title") or row.get("description")
        out.append(
            ArchiveEvent(
                event_id=new_event_id(),
                provider=provider,
                product=f"my_activity_{sub_product}",
                event_type="activity",
                ts=ts,
                actor="self",
                title=str(title)[:500] if title else None,
                text=str(text)[:4000] if text else None,
                url=str(row.get("titleUrl") or row.get("url") or "")[:2000] or None,
                source_file=rel,
                source_ref=f"{rel}#{i}",
            )
        )
    return out
