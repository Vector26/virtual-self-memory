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
    title = data.get("title") or data.get("description")
    ts = parse_ts(
        data.get("photoTakenTime", {}).get("timestamp")
        if isinstance(data.get("photoTakenTime"), dict)
        else data.get("creationTime")
    )
    geo = data.get("geoData") or {}
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    out.append(
        ArchiveEvent(
            event_id=new_event_id(),
            provider=provider,
            product=product,
            event_type="photo_metadata",
            ts=ts,
            actor="self",
            title=str(title)[:300] if title else path.name,
            source_file=rel,
            source_ref=rel,
            geo_lat=float(lat) if lat else None,
            geo_lon=float(lon) if lon else None,
        )
    )
    return out
