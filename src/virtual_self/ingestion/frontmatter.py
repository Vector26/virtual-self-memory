from __future__ import annotations

from typing import Any

import yaml


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_raw = parts[1]
    body = parts[2].lstrip("\n")
    meta = yaml.safe_load(meta_raw) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def notion_id_from_meta(meta: dict[str, Any], path_hint: str) -> str | None:
    nid = meta.get("notion_id")
    if nid:
        return str(nid).strip('"')
    import re

    m = re.search(r"\[([0-9a-f]{8})\]", path_hint)
    return m.group(1) if m else None
