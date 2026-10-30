from __future__ import annotations

import json
from pathlib import Path

from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id, parse_ts


def parse_file(path: Path, rel: str, provider: str, product: str) -> list[ArchiveEvent]:
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return []
        locations = data.get("locations") or data.get("timelineObjects") or []
        out: list[ArchiveEvent] = []
        for i, row in enumerate(locations):
            if not isinstance(row, dict):
                continue
            lat = row.get("latitudeE7") or row.get("lat")
            lon = row.get("longitudeE7") or row.get("lng")
            if lat and lon:
                lat_f = float(lat) / (1e7 if abs(float(lat)) > 90 else 1)
                lon_f = float(lon) / (1e7 if abs(float(lon)) > 180 else 1)
            else:
                lat_f = lon_f = None
            ts = parse_ts(row.get("timestamp") or row.get("timestampMs"))
            out.append(
                ArchiveEvent(
                    event_id=new_event_id(),
                    provider=provider,
                    product=product,
                    event_type="location",
                    ts=ts,
                    actor="self",
                    title=row.get("name") or row.get("placeLocation"),
                    geo_lat=lat_f,
                    geo_lon=lon_f,
                    source_file=rel,
                    source_ref=f"{rel}#{i}",
                )
            )
        return out
    return []
