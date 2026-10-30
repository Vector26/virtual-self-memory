from __future__ import annotations

from pathlib import Path

from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[ArchiveEvent] = []
    name = None
    idx = 0
    for line in text.splitlines():
        if line.startswith("BEGIN:VCARD"):
            name = None
            idx += 1
        elif line.upper().startswith("FN:"):
            name = line.split(":", 1)[-1].strip()
        elif line.startswith("END:VCARD") and name:
            out.append(
                ArchiveEvent(
                    event_id=new_event_id(),
                    provider=provider,
                    product=product,
                    event_type="contact",
                    ts=None,
                    actor="self",
                    title=name[:200],
                    text=name[:200],
                    source_file=rel,
                    source_ref=f"{rel}#vcard{idx}",
                )
            )
    return out
