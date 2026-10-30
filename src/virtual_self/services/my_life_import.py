from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.crypto.fields import encrypt_text
from virtual_self.domain.types import MemoryStatus, Trust
from virtual_self.ingestion.my_life_scanner import ScannedPage, iter_my_life_pages
from virtual_self.observability.metrics import Counter
from virtual_self.persistence.db import execute, fetch_one, transaction
from virtual_self.policy.my_life_import import ImportDisposition
from virtual_self.policy.sensitivity import classify_source_path
from virtual_self.config import get_settings
from virtual_self.enrichment.constants import CURSOR_AGENT_PRODUCER, PROMPT_VERSION
from virtual_self.services import audit, jobs, suppression
from virtual_self.services.indexing import index_memory_version
from virtual_self.services.page_enrichment import resolve_enrichment
from uuid6 import uuid7

IMPORT_PAGES = Counter("vsm_my_life_import_pages_total", "My Life import pages", ["result"])


@dataclass
class ImportStats:
    scanned: int = 0
    skipped: int = 0
    metadata_only: int = 0
    full: int = 0
    sources_created: int = 0
    sources_updated: int = 0
    sources_unchanged: int = 0
    memories_created: int = 0
    memories_updated: int = 0
    memories_pending_enrichment: int = 0
    memories_skipped_suppressed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "skipped": self.skipped,
            "metadata_only": self.metadata_only,
            "full": self.full,
            "sources_created": self.sources_created,
            "sources_updated": self.sources_updated,
            "sources_unchanged": self.sources_unchanged,
            "memories_created": self.memories_created,
            "memories_updated": self.memories_updated,
            "memories_pending_enrichment": self.memories_pending_enrichment,
            "memories_skipped_suppressed": self.memories_skipped_suppressed,
            "errors": self.errors,
        }


def plan_import(root: Path) -> dict[str, Any]:
    stats = ImportStats()
    samples: dict[str, list[str]] = {"skip": [], "metadata_only": [], "full": []}
    for page in iter_my_life_pages(root):
        stats.scanned += 1
        _tally_page(stats, page.decision.disposition)
        key = page.decision.disposition.value
        if len(samples[key]) < 5:
            samples[key].append(f"{page.rel_path} ({page.decision.reason})")
    return {"root": str(root), "stats": stats.to_dict(), "samples": samples}


def run_import(
    auth: AuthContext,
    root: Path,
    *,
    dry_run: bool = True,
    promote_memories: bool = True,
    enrich_on_import: bool | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    stats = ImportStats()
    run_id = uuid7()
    if dry_run:
        out = plan_import(root)
        out["dry_run"] = True
        out["run_id"] = str(run_id)
        return out

    with transaction() as conn:
        execute(
            conn,
            """
            INSERT INTO import_runs (id, subject_id, root_path, dry_run, stats)
            VALUES (%s, %s, %s, FALSE, '{}'::jsonb)
            """,
            (run_id, auth.subject_id, str(root)),
        )

    settings = get_settings()
    do_enrich = enrich_on_import if enrich_on_import is not None else settings.enrich_on_import
    enrichment_run = None
    if do_enrich:
        from virtual_self.services.enrichment_batch import enrich_corpus

        enrichment_run = enrich_corpus(
            auth.subject_id,
            root,
            only_missing=True,
            thaw_chat_model=get_settings().enrich_thaw_cold_model,
        )

    for page in iter_my_life_pages(root):
        stats.scanned += 1
        disp = page.decision.disposition
        _tally_page(stats, disp)
        if disp == ImportDisposition.SKIP:
            IMPORT_PAGES.labels("skipped").inc()
            continue
        try:
            with transaction() as conn:
                changed, mem_created = _upsert_page(
                    conn,
                    auth,
                    page,
                    promote_memories=promote_memories,
                )
                if changed == "created":
                    stats.sources_created += 1
                    IMPORT_PAGES.labels("created").inc()
                elif changed == "updated":
                    stats.sources_updated += 1
                    IMPORT_PAGES.labels("updated").inc()
                else:
                    stats.sources_unchanged += 1
                    IMPORT_PAGES.labels("unchanged").inc()
                if mem_created == 1:
                    stats.memories_created += 1
                elif mem_created == 2:
                    stats.memories_updated += 1
                elif mem_created == -1:
                    stats.memories_pending_enrichment += 1
        except Exception as exc:
            stats.errors.append(f"{page.rel_path}: {exc}")
            IMPORT_PAGES.labels("error").inc()

    with transaction() as conn:
        execute(
            conn,
            """
            UPDATE import_runs SET stats = %s::jsonb, finished_at = NOW() WHERE id = %s
            """,
            (json.dumps(stats.to_dict()), run_id),
        )
        audit.log_event(
            conn,
            request_id=request_id,
            actor_id=auth.actor_id,
            operation="import.my_life",
            target_type="import_run",
            target_id=run_id,
            metadata={"root": str(root), "stats": stats.to_dict()},
        )

    from virtual_self.services.indexing import index_stats as index_status, reindex_subject
    from virtual_self.services.memory_refresh import refresh_memories_from_enrichments
    from virtual_self.services.personality import bootstrap_personality

    refresh = refresh_memories_from_enrichments(auth, root)
    bootstrap_personality(auth)
    index_stats = reindex_subject(auth.subject_id, only_missing=True)

    return {
        "dry_run": False,
        "run_id": str(run_id),
        "root": str(root),
        "stats": stats.to_dict(),
        "enrichment": enrichment_run,
        "memory_refresh": refresh,
        "index": index_stats,
        "index_status": index_status(auth.subject_id),
    }


def _tally_page(stats: ImportStats, disp: ImportDisposition) -> None:
    if disp == ImportDisposition.SKIP:
        stats.skipped += 1
    elif disp == ImportDisposition.METADATA_ONLY:
        stats.metadata_only += 1
    elif disp == ImportDisposition.FULL:
        stats.full += 1


def _upsert_page(
    conn,
    auth: AuthContext,
    page: ScannedPage,
    *,
    promote_memories: bool,
) -> tuple[str, int]:
    external_id = page.notion_id or page.rel_path
    sensitivity = classify_source_path(page.rel_path)
    existing = fetch_one(
        conn,
        """
        SELECT id, content_hash FROM sources
        WHERE subject_id = %s AND source_type = 'my_life_md' AND external_id = %s
        """,
        (auth.subject_id, external_id),
    )
    if existing and existing["content_hash"] == page.content_hash:
        return "unchanged", 0

    source_id = existing["id"] if existing else uuid7()
    if existing is None:
        execute(
            conn,
            """
            INSERT INTO sources (
              id, subject_id, source_type, external_id, source_path, content_hash, sensitivity
            ) VALUES (%s, %s, 'my_life_md', %s, %s, %s, %s)
            """,
            (source_id, auth.subject_id, external_id, page.rel_path, page.content_hash, sensitivity),
        )
        action = "created"
    else:
        execute(
            conn,
            """
            UPDATE sources SET content_hash = %s, source_path = %s, sensitivity = %s, ingested_at = NOW()
            WHERE id = %s
            """,
            (page.content_hash, page.rel_path, sensitivity, source_id),
        )
        action = "updated"

    plain, structured = _payload_for_page(page)
    cipher, plain_store = encrypt_text(plain)
    evidence_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
        VALUES (%s, 'evidence', %s, %s)
        """,
        (evidence_id, auth.subject_id, sensitivity),
    )
    execute(
        conn,
        """
        INSERT INTO evidence_records (
          id, source_id, content_ciphertext, content_plain, structured_content,
          content_hash, sensitivity, classification_status
        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, 'classified')
        """,
        (
            evidence_id,
            source_id,
            cipher,
            plain_store,
            json.dumps(structured),
            page.content_hash,
            sensitivity,
        ),
    )

    mem_created = 0
    if promote_memories and page.decision.disposition == ImportDisposition.FULL:
        mem_created = _maybe_promote_memory(conn, auth, page, evidence_id, plain)

    jobs.enqueue(
        conn,
        "projection.invalidate",
        {"source_id": str(source_id)},
        dedupe_key=f"import:{source_id}",
    )
    return action, mem_created


def _payload_for_page(page: ScannedPage) -> tuple[str, dict[str, Any]]:
    if page.decision.disposition == ImportDisposition.METADATA_ONLY:
        structured = {
            "title": page.title,
            "notion_id": page.notion_id,
            "rel_path": page.rel_path,
            "properties": page.properties,
            "reason": page.decision.reason,
        }
        plain = page.title or page.rel_path
        return plain, structured
    structured = {
        "title": page.title,
        "notion_id": page.notion_id,
        "rel_path": page.rel_path,
        "properties": page.properties,
    }
    body = page.body.strip()
    if page.title and not body.startswith("#"):
        plain = f"# {page.title}\n\n{body}"
    else:
        plain = body
    if len(plain) > 48_000:
        plain = plain[:48_000] + "\n…[truncated]"
    return plain, structured


def _maybe_promote_memory(
    conn,
    auth: AuthContext,
    page: ScannedPage,
    evidence_id: UUID,
    plain: str,
) -> int:
    settings = get_settings()
    enrichment = resolve_enrichment(
        auth.subject_id,
        page,
        plain,
        allow_live_agent=settings.enrich_on_import,
    )
    if enrichment is None:
        if settings.require_agent_enrichment:
            return -1
        return 0

    kind = enrichment["memory_kind"]
    summary = enrichment["summary"]
    structured = json.dumps(
        {
            "labels": enrichment.get("labels") or [],
            "producer": CURSOR_AGENT_PRODUCER,
            "prompt_version": enrichment.get("prompt_version") or PROMPT_VERSION,
            "rel_path": page.rel_path,
        }
    )
    if suppression.is_suppressed(conn, auth.subject_id, kind, summary):
        return 0

    existing = fetch_one(
        conn,
        """
        SELECT m.id, mv.id AS version_id, mv.content, m.kind
        FROM sources s
        JOIN evidence_records er ON er.source_id = s.id
        JOIN memory_evidence me ON me.evidence_record_id = er.id
        JOIN memories m ON m.id = me.memory_id
        JOIN memory_versions mv ON mv.id = m.current_version_id
        WHERE s.subject_id = %s AND s.source_path = %s AND mv.status = 'active'
        ORDER BY m.updated_at DESC
        LIMIT 1
        """,
        (auth.subject_id, page.rel_path),
    )
    sensitivity = classify_source_path(page.rel_path)
    if existing:
        if existing["content"] == summary and existing["kind"] == kind:
            return 0
        new_ver_id = uuid7()
        cur = fetch_one(
            conn,
            "SELECT COALESCE(MAX(version), 0) AS v FROM memory_versions WHERE memory_id = %s",
            (existing["id"],),
        )
        new_version = int(cur["v"] if cur else 0) + 1
        execute(
            conn,
            "UPDATE memory_versions SET status = %s, valid_to = NOW() WHERE id = %s",
            (MemoryStatus.SUPERSEDED.value, existing["version_id"]),
        )
        execute(
            conn,
            "INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity) VALUES (%s, 'memory_version', %s, %s)",
            (new_ver_id, auth.subject_id, sensitivity),
        )
        execute(
            conn,
            """
            INSERT INTO memory_versions (
              id, memory_id, version, content, structured_content, status, trust, sensitivity, created_by, change_reason
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            """,
            (
                new_ver_id,
                existing["id"],
                new_version,
                summary,
                structured,
                MemoryStatus.ACTIVE.value,
                Trust.IMPORTED.value,
                sensitivity,
                auth.actor_id,
                "cursor_agent_enrichment",
            ),
        )
        execute(conn, "UPDATE memories SET kind = %s, current_version_id = %s, updated_at = NOW() WHERE id = %s",
                (kind, new_ver_id, existing["id"]))
        execute(
            conn,
            """
            INSERT INTO memory_evidence (memory_id, memory_version_id, evidence_record_id, relationship, extractor)
            VALUES (%s, %s, %s, 'derived_from', 'cursor_agent')
            ON CONFLICT DO NOTHING
            """,
            (existing["id"], new_ver_id, evidence_id),
        )
        index_memory_version(conn, new_ver_id, summary)
        return 2

    memory_id = uuid7()
    version_id = uuid7()
    execute(conn, "INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity) VALUES (%s, 'memory', %s, %s)",
            (memory_id, auth.subject_id, sensitivity))
    execute(conn, "INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity) VALUES (%s, 'memory_version', %s, %s)",
            (version_id, auth.subject_id, sensitivity))
    execute(conn, "INSERT INTO memories (id, subject_id, kind, scope) VALUES (%s, %s, %s, 'imported')",
            (memory_id, auth.subject_id, kind))
    execute(
        conn,
        """
        INSERT INTO memory_versions (
          id, memory_id, version, content, structured_content, status, trust, sensitivity, created_by
        ) VALUES (%s, %s, 1, %s, %s::jsonb, %s, %s, %s, %s)
        """,
        (
            version_id,
            memory_id,
            summary,
            structured,
            MemoryStatus.ACTIVE.value,
            Trust.IMPORTED.value,
            sensitivity,
            auth.actor_id,
        ),
    )
    execute(conn, "UPDATE memories SET current_version_id = %s, updated_at = NOW() WHERE id = %s",
            (version_id, memory_id))
    execute(
        conn,
        """
        INSERT INTO memory_evidence (memory_id, memory_version_id, evidence_record_id, relationship, extractor)
        VALUES (%s, %s, %s, 'derived_from', 'cursor_agent')
        """,
        (memory_id, version_id, evidence_id),
    )
    execute(
        conn,
        """
        INSERT INTO derivation_edges (parent_artifact_id, child_artifact_id, relationship)
        VALUES (%s, %s, 'derived_from')
        """,
        (evidence_id, version_id),
    )
    index_memory_version(conn, version_id, summary)
    return 1
