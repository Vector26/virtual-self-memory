from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id


def _parse_ics_dt(line: str) -> datetime | None:
    line = line.strip()
    if line.endswith("Z"):
        line = line[:-1]
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(line, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[ArchiveEvent] = []
    summary = None
    dtstart = None
    idx = 0
    for line in lines:
        if line.startswith("BEGIN:VEVENT"):
            summary = None
            dtstart = None
            idx += 1
        elif line.startswith("SUMMARY:"):
            summary = line[8:].strip()
        elif line.startswith("DTSTART"):
            part = line.split(":", 1)[-1]
            dtstart = _parse_ics_dt(part)
        elif line.startswith("END:VEVENT"):
            out.append(
                ArchiveEvent(
                    event_id=new_event_id(),
                    provider=provider,
                    product=product,
                    event_type="calendar",
                    ts=dtstart,
                    actor="self",
                    title=summary,
                    source_file=rel,
                    source_ref=f"{rel}#event{idx}",
                )
            )
    return out
