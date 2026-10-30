from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from virtual_self.config import get_settings
from virtual_self.observability.logging import get_logger
from virtual_self.persistence.db import execute, fetch_all, fetch_one, transaction
from virtual_self.providers.embeddings import embed_texts, vector_literal

log = get_logger(__name__)
BATCH = 32


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def index_memory_version(conn, memory_version_id: UUID, content: str) -> bool:
    settings = get_settings()
    ch = content_hash(content)
    existing = fetch_one(
        conn,
        """
        SELECT content_hash FROM memory_embeddings WHERE memory_version_id = %s
        """,
        (memory_version_id,),
    )
    if existing and existing["content_hash"] == ch:
        _update_search_vector(conn, memory_version_id, content)
        return False

    vec_list = embed_texts([content])
    vec = vec_list[0] if vec_list else None
    if vec is None or len(vec) != settings.embed_dims:
        log.warning("embed_failed", memory_version_id=str(memory_version_id))
        _update_search_vector(conn, memory_version_id, content)
        return False

    execute(
        conn,
        """
        INSERT INTO memory_embeddings (memory_version_id, model, dims, content_hash, embedding)
        VALUES (%s, %s, %s, %s, %s::vector)
        ON CONFLICT (memory_version_id) DO UPDATE SET
          model = EXCLUDED.model,
          dims = EXCLUDED.dims,
          content_hash = EXCLUDED.content_hash,
          embedding = EXCLUDED.embedding,
          created_at = NOW()
        """,
        (memory_version_id, settings.embed_model, len(vec), ch, vector_literal(vec)),
    )
    _update_search_vector(conn, memory_version_id, content)
    return True


def _update_search_vector(conn, memory_version_id: UUID, content: str) -> None:
    execute(
        conn,
        """
        UPDATE memory_versions
        SET search_vector = to_tsvector('english', %s)
        WHERE id = %s
        """,
        (content[:50000], memory_version_id),
    )


def reindex_subject(subject_id: UUID, *, only_missing: bool = False) -> dict[str, Any]:
    settings = get_settings()
    indexed = 0
    skipped = 0
    failed = 0
    with transaction() as conn:
        if only_missing:
            rows = fetch_all(
                conn,
                """
                SELECT mv.id, mv.content
                FROM memories m
                JOIN memory_versions mv ON mv.id = m.current_version_id
                JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
                LEFT JOIN memory_embeddings me ON me.memory_version_id = mv.id
                WHERE m.subject_id = %s AND mv.status = 'active' AND me.memory_version_id IS NULL
                ORDER BY m.updated_at DESC
                """,
                (subject_id,),
            )
        else:
            rows = fetch_all(
                conn,
                """
                SELECT mv.id, mv.content
                FROM memories m
                JOIN memory_versions mv ON mv.id = m.current_version_id
                JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
                WHERE m.subject_id = %s AND mv.status = 'active'
                ORDER BY m.updated_at DESC
                """,
                (subject_id,),
            )

    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        texts = [r["content"] for r in batch]
        vectors = embed_texts(texts)
        with transaction() as conn:
            for row, vec in zip(batch, vectors):
                vid = row["id"]
                content = row["content"]
                ch = content_hash(content)
                if vec is None or len(vec) != settings.embed_dims:
                    failed += 1
                    _update_search_vector(conn, vid, content)
                    continue
                execute(
                    conn,
                    """
                    INSERT INTO memory_embeddings (memory_version_id, model, dims, content_hash, embedding)
                    VALUES (%s, %s, %s, %s, %s::vector)
                    ON CONFLICT (memory_version_id) DO UPDATE SET
                      model = EXCLUDED.model,
                      dims = EXCLUDED.dims,
                      content_hash = EXCLUDED.content_hash,
                      embedding = EXCLUDED.embedding,
                      created_at = NOW()
                    """,
                    (vid, settings.embed_model, len(vec), ch, vector_literal(vec)),
                )
                _update_search_vector(conn, vid, content)
                indexed += 1
    return {
        "candidates": len(rows),
        "indexed": indexed,
        "failed_embed": failed,
        "skipped_batches": skipped,
        "model": settings.embed_model,
    }


def index_stats(subject_id: UUID) -> dict[str, Any]:
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT
              COUNT(*) FILTER (WHERE mv.status = 'active') AS active_versions,
              COUNT(me.memory_version_id) FILTER (WHERE mv.status = 'active') AS embedded
            FROM memories m
            JOIN memory_versions mv ON mv.id = m.current_version_id
            JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
            LEFT JOIN memory_embeddings me ON me.memory_version_id = mv.id
            WHERE m.subject_id = %s
            """,
            (subject_id,),
        )
    active = int(row["active_versions"] or 0)
    embedded = int(row["embedded"] or 0)
    return {
        "active_memories": active,
        "embedded": embedded,
        "coverage_pct": round(100.0 * embedded / active, 1) if active else 0.0,
        "fully_indexed": active > 0 and embedded == active,
    }
