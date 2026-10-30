from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id, parse_ts


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data if isinstance(data, list) else data.get("Browser History") or []
    out: list[ArchiveEvent] = []
    for i, row in enumerate(items):
        if not isinstance(row, dict):
            continue
        url = row.get("url") or row.get("Url")
        title = row.get("title") or row.get("Title")
        ts = parse_ts(row.get("time_usec") or row.get("last_visit_time") or row.get("time"))
        domain = urlparse(str(url)).netloc if url else None
        out.append(
            ArchiveEvent(
                event_id=new_event_id(),
                provider=provider,
                product=product,
                event_type="browse",
                ts=ts,
                actor="self",
                title=str(title)[:500] if title else None,
                url=str(url)[:2000] if url else None,
                domain=domain,
                source_file=rel,
                source_ref=f"{rel}#{i}",
            )
        )
    return out
