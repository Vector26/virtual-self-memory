from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from virtual_self.ingestion.frontmatter import notion_id_from_meta, split_frontmatter
from virtual_self.policy.my_life_import import ImportDecision, classify_page


@dataclass(frozen=True)
class ScannedPage:
    rel_path: str
    abs_path: Path
    notion_id: str | None
    title: str | None
    decision: ImportDecision
    content_hash: str
    body: str
    meta: dict[str, Any]
    properties: dict[str, Any]


def iter_my_life_pages(root: Path) -> Iterator[ScannedPage]:
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"My Life root not found: {root}")

    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        raw = path.read_text(encoding="utf-8", errors="replace")
        meta, body = split_frontmatter(raw)
        props = meta.get("properties") if isinstance(meta.get("properties"), dict) else {}
        export_mode = meta.get("export_mode")
        if isinstance(export_mode, str):
            export_mode = export_mode.strip('"')
        decision = classify_page(
            rel,
            export_mode=export_mode,
            body=body,
            properties=props,
        )
        normalized = _normalized_payload(rel, meta, body, decision)
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        yield ScannedPage(
            rel_path=rel,
            abs_path=path,
            notion_id=notion_id_from_meta(meta, rel),
            title=str(meta.get("title") or "").strip('"') or None,
            decision=decision,
            content_hash=content_hash,
            body=body,
            meta=meta,
            properties=props,
        )


def _normalized_payload(rel: str, meta: dict, body: str, decision: ImportDecision) -> str:
    if decision.disposition.value == "metadata_only":
        safe = {k: meta.get(k) for k in ("title", "notion_id", "created_time", "last_edited_time", "properties")}
        return f"{rel}\n{safe}"
    return f"{rel}\n{body}"
