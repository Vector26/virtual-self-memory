from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.enrichment.constants import (
    ALLOWED_MEMORY_KINDS,
    CURSOR_AGENT_PRODUCER,
    PROMPT_VERSION,
)
from virtual_self.enrichment.grounding import adjust_confidence, passes_grounding
from virtual_self.enrichment.prompts import PAGE_ENRICHMENT_SYSTEM
from virtual_self.ingestion.my_life_scanner import ScannedPage, iter_my_life_pages
from virtual_self.policy.my_life_import import ImportDecision, ImportDisposition
from virtual_self.observability.logging import get_logger
from virtual_self.persistence.db import execute, fetch_one, transaction
from virtual_self.providers.agent_chat import chat_json
from uuid6 import uuid7

log = get_logger(__name__)

def _area_hint(rel_path: str) -> str:
    top = rel_path.replace("\\", "/").split("/")[0]
    return top.split(" [")[0] if top else "unknown"


def _user_prompt(page: ScannedPage, plain: str) -> str:
    props = json.dumps(page.properties, default=str)[:2000]
    body = plain[:12000]
    status = page.properties.get("Status") or page.properties.get("status") or ""
    node_type = page.properties.get("Node Type") or page.properties.get("Type") or ""
    return (
        f"corpus_area: {_area_hint(page.rel_path)}\n"
        f"rel_path: {page.rel_path}\n"
        f"title: {page.title or ''}\n"
        f"import_reason: {page.decision.reason}\n"
        f"notion_status: {status}\n"
        f"notion_node_type: {node_type}\n"
        f"notion_properties: {props}\n\n"
        f"--- source markdown ---\n{body}"
    )


def _properties_text(properties: dict[str, Any]) -> str:
    parts = []
    for key, value in properties.items():
        val = str(value).strip()
        if val and key.lower() != "name":
            parts.append(f"{key}: {val[:200]}")
    return ". ".join(parts)


def _extractive_stub_summary(title: str, plain: str, props_text: str) -> str:
    t = title.strip() or "untitled"
    body = plain.strip().lstrip("#").strip().replace("\n", " ")
    if body.lower() == t.lower():
        body = ""
    pieces = [t]
    if body:
        pieces.append(body[:220])
    if props_text:
        pieces.append(props_text[:400])
    summary = ". ".join(pieces)
    if len(summary) < 20:
        summary = f"Notion page titled {t} (title only)."
    return summary[:4000]


def _infer_stub_kind(rel_path: str, plain: str) -> str:
    area = _area_hint(rel_path).lower()
    if "quest" in area or "career" in area:
        return "goal"
    if "skill" in area:
        return "skill"
    if "win" in area:
        return "episode"
    if len(plain.strip()) < 80:
        return "current_state"
    return "episode"


def fallback_enrichment_payload(page: ScannedPage, plain: str) -> dict[str, Any] | None:
    """Grounded enrichment when the LLM cannot pass grounding (thin Notion stubs)."""
    area = _area_hint(page.rel_path)
    title = page.title or area
    props_text = _properties_text(page.properties)
    summary = _extractive_stub_summary(title, plain, props_text)
    grounding_source = f"{plain}\n{props_text}\nNotion page titled {title} (title only)"
    data = {
        "summary": summary,
        "memory_kind": _infer_stub_kind(page.rel_path, plain),
        "labels": [area.lower()[:48], "notion_export"],
        "confidence": 0.42,
    }
    for min_score in (0.35, 0.22):
        payload = _validate_payload(
            data,
            source_plain=grounding_source,
            title=page.title,
            min_grounding=min_score,
        )
        if payload is not None:
            return payload
    return None


def _validate_payload(
    data: dict[str, Any],
    *,
    source_plain: str = "",
    title: str | None = None,
    min_grounding: float = 0.35,
) -> dict[str, Any] | None:
    summary = str(data.get("summary") or "").strip()
    kind = str(data.get("memory_kind") or data.get("kind") or "").strip().lower()
    labels = data.get("labels")
    if not summary or len(summary) < 20:
        return None
    if kind not in ALLOWED_MEMORY_KINDS:
        return None
    if not isinstance(labels, list):
        labels = []
    labels = [str(x).strip().lower()[:48] for x in labels if str(x).strip()][:8]
    conf = data.get("confidence")
    try:
        confidence = float(conf) if conf is not None else 0.75
    except (TypeError, ValueError):
        confidence = 0.75
    if source_plain and not passes_grounding(
        summary, source_plain, title, min_score=min_grounding
    ):
        log.warning("enrich_grounding_reject", rel_path=title, score="low")
        return None
    if source_plain:
        from virtual_self.enrichment.grounding import grounding_score

        score = grounding_score(summary, source_plain, title)
        confidence = adjust_confidence(summary, confidence, score)
    else:
        confidence = max(0.0, min(1.0, confidence))
    return {
        "summary": summary[:4000],
        "memory_kind": kind,
        "labels": labels,
        "confidence": confidence,
    }


def enrich_page_with_agent(page: ScannedPage, plain: str) -> dict[str, Any] | None:
    raw = chat_json(PAGE_ENRICHMENT_SYSTEM, _user_prompt(page, plain))
    if raw is None:
        return None
    return _validate_payload(raw, source_plain=plain, title=page.title)


def get_stored_enrichment(subject_id: UUID, rel_path: str, content_hash: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT summary, memory_kind, labels, producer, content_hash, prompt_version, confidence
            FROM page_enrichments
            WHERE subject_id = %s AND rel_path = %s AND content_hash = %s
              AND producer = %s
            """,
            (subject_id, rel_path, content_hash, CURSOR_AGENT_PRODUCER),
        )
    return dict(row) if row else None


def upsert_enrichment(
    subject_id: UUID,
    page: ScannedPage,
    payload: dict[str, Any],
    *,
    model: str | None,
) -> UUID:
    eid = uuid7()
    with transaction() as conn:
        execute(
            conn,
            """
            INSERT INTO page_enrichments (
              id, subject_id, rel_path, content_hash, summary, memory_kind, labels,
              producer, model, prompt_version, confidence
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT (subject_id, rel_path, content_hash) DO UPDATE SET
              summary = EXCLUDED.summary,
              memory_kind = EXCLUDED.memory_kind,
              labels = EXCLUDED.labels,
              producer = EXCLUDED.producer,
              model = EXCLUDED.model,
              prompt_version = EXCLUDED.prompt_version,
              confidence = EXCLUDED.confidence,
              created_at = NOW()
            """,
            (
                eid,
                subject_id,
                page.rel_path,
                page.content_hash,
                payload["summary"],
                payload["memory_kind"],
                json.dumps(payload["labels"]),
                CURSOR_AGENT_PRODUCER,
                model,
                PROMPT_VERSION,
                payload["confidence"],
            ),
        )
    return eid


def resolve_enrichment(
    subject_id: UUID,
    page: ScannedPage,
    plain: str,
    *,
    allow_live_agent: bool,
) -> dict[str, Any] | None:
    stored = get_stored_enrichment(subject_id, page.rel_path, page.content_hash)
    if (
        stored
        and stored.get("producer") == CURSOR_AGENT_PRODUCER
        and stored.get("prompt_version") == PROMPT_VERSION
    ):
        return stored
    if not allow_live_agent:
        return None
    from virtual_self.config import get_settings

    live = enrich_page_with_agent(page, plain)
    if live is None:
        return None
    upsert_enrichment(subject_id, page, live, model=get_settings().chat_model)
    return get_stored_enrichment(subject_id, page.rel_path, page.content_hash)


def load_enrichment_jsonl(path: Path, subject_id: UUID) -> int:
    """Apply agent-produced JSONL (offline Cursor batches)."""
    count = 0
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        rel = row["rel_path"]
        ch = row["content_hash"]
        payload = _validate_payload(row)
        if payload is None:
            continue
        page_stub = ScannedPage(
            rel_path=rel,
            abs_path=Path(rel),
            notion_id=row.get("notion_id"),
            title=row.get("title"),
            decision=ImportDecision(ImportDisposition.FULL, "jsonl"),
            content_hash=ch,
            body="",
            meta={},
            properties=row.get("properties") or {},
        )
        upsert_enrichment(subject_id, page_stub, payload, model=row.get("model") or "cursor_batch")
        count += 1
    return count


def count_enriched_full_pages(subject_id: UUID, root: Path) -> tuple[int, int]:
    """Pages with FULL import disposition that have enrichment for the current content hash."""
    full = 0
    enriched = 0
    for page in iter_my_life_pages(root):
        if page.decision.disposition != ImportDisposition.FULL:
            continue
        full += 1
        stored = get_stored_enrichment(subject_id, page.rel_path, page.content_hash)
        if stored and stored.get("prompt_version") == PROMPT_VERSION:
            enriched += 1
    return enriched, full


def enrichment_stats(subject_id: UUID, *, root: Path | None = None) -> dict[str, Any]:
    from virtual_self.config import get_settings

    scan_root = root or Path(get_settings().my_life_root)
    enriched, full = count_enriched_full_pages(subject_id, scan_root)
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT COUNT(*) AS n FROM page_enrichments
            WHERE subject_id = %s AND producer = %s
            """,
            (subject_id, CURSOR_AGENT_PRODUCER),
        )
    return {
        "enriched_pages": enriched,
        "full_pages": full,
        "enrichment_rows": int(row["n"] or 0),
        "producer": CURSOR_AGENT_PRODUCER,
    }
