from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class ArchiveEvent:
    event_id: str
    provider: str
    product: str
    event_type: str
    ts: datetime | None
    actor: str
    title: str | None = None
    text: str | None = None
    url: str | None = None
    domain: str | None = None
    geo_lat: float | None = None
    geo_lon: float | None = None
    entities: list[str] = field(default_factory=list)
    source_file: str = ""
    source_ref: str = ""
    sensitivity: int = 1
    content_hash: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "provider": self.provider,
            "product": self.product,
            "event_type": self.event_type,
            "ts": self.ts.isoformat() if self.ts else None,
            "actor": self.actor,
            "title": self.title,
            "text": self.text,
            "url": self.url,
            "domain": self.domain,
            "geo_lat": self.geo_lat,
            "geo_lon": self.geo_lon,
            "entities": self.entities,
            "source_file": self.source_file,
            "source_ref": self.source_ref,
            "sensitivity": self.sensitivity,
            "content_hash": self.content_hash or _hash_content(self),
            "year": self.ts.year if self.ts else None,
        }


def _hash_content(ev: ArchiveEvent) -> str:
    blob = json.dumps(
        {
            "t": ev.title,
            "x": ev.text,
            "u": ev.url,
            "r": ev.source_ref,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def new_event_id() -> str:
    return str(uuid4())


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e12 else value, tz=timezone.utc)
        except (OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s.replace("+00:00", "Z"), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
