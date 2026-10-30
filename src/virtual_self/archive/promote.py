from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.domain.types import Trust
from virtual_self.persistence.db import execute, fetch_all, fetch_one, transaction
from virtual_self.services.memories import create_memory, search_memories

EXTRACTOR = "archive_pipeline"
EXTRACTOR_VERSION = "archive-v1"


def _ensure_archive_source(conn, auth: AuthContext, external_id: str) -> UUID:
    row = fetch_one(
        conn,
        """
        SELECT id FROM sources
        WHERE subject_id = %s AND source_type = 'archive_event' AND external_id = %s
        """,
        (auth.subject_id, external_id),
    )
    if row:
        return row["id"]
    sid = uuid7()
    execute(
        conn,
        """
        INSERT INTO sources (id, subject_id, source_type, external_id, source_path, content_hash, sensitivity)
        VALUES (%s, %s, 'archive_event', %s, %s, %s, %s)
        """,
        (sid, auth.subject_id, external_id, external_id, external_id, 1),
    )
    return sid


def _evidence_for_event(conn, auth: AuthContext, event: dict[str, Any]) -> UUID:
    eid = str(event.get("event_id"))
    source_id = _ensure_archive_source(conn, auth, eid)
    existing = fetch_one(
        conn,
        "SELECT id FROM evidence_records WHERE content_hash = %s AND source_id = %s",
        (event.get("content_hash") or eid, source_id),
    )
    if existing:
        return existing["id"]
    ev_id = uuid7()
    plain = f"{event.get('title') or ''}\n{event.get('text') or ''}".strip()
    execute(
        conn,
        """
        INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
        VALUES (%s, 'evidence', %s, %s)
        """,
        (ev_id, auth.subject_id, int(event.get("sensitivity") or 1)),
    )
    execute(
        conn,
        """
        INSERT INTO evidence_records (
          id, source_id, content_plain, structured_content, content_hash, sensitivity, classification_status
        ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, 'classified')
        """,
        (
            ev_id,
            source_id,
            plain[:32000],
            json.dumps({"event_id": eid, "product": event.get("product")}),
            event.get("content_hash") or eid,
            int(event.get("sensitivity") or 1),
        ),
    )
    return ev_id


def _link_memory_evidence(conn, memory_id: UUID, version_id: UUID, evidence_id: UUID) -> None:
    execute(
        conn,
        """
        INSERT INTO memory_evidence (
          memory_id, memory_version_id, evidence_record_id, relationship, extractor, extractor_version
        ) VALUES (%s, %s, %s, 'derived_from', %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (memory_id, version_id, evidence_id, EXTRACTOR, EXTRACTOR_VERSION),
    )


def count_archive_memories(auth: AuthContext) -> int:
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT COUNT(*)::int AS n
            FROM memories m
            JOIN memory_versions mv ON mv.id = m.current_version_id
            JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
            WHERE m.subject_id = %s AND m.scope = 'archive' AND mv.status = 'active'
            """,
            (auth.subject_id,),
        )
    return int(row["n"]) if row else 0


def _memory_exists_for_event(conn, auth: AuthContext, event_id: str) -> bool:
    row = fetch_one(
        conn,
        """
        SELECT 1
        FROM sources s
        JOIN evidence_records er ON er.source_id = s.id
        JOIN memory_evidence me ON me.evidence_record_id = er.id
        JOIN memories m ON m.id = me.memory_id
        JOIN memory_versions mv ON mv.id = m.current_version_id
        WHERE s.subject_id = %s
          AND s.source_type = 'archive_event'
          AND s.external_id = %s
          AND m.scope = 'archive'
          AND mv.status = 'active'
        LIMIT 1
        """,
        (auth.subject_id, event_id),
    )
    return row is not None


def _event_memory_content(ev: dict[str, Any]) -> str | None:
    product = (ev.get("product") or "archive").strip()
    etype = (ev.get("event_type") or "event").strip()
    title = (ev.get("title") or "").strip()
    text = (ev.get("text") or "").strip()
    url = (ev.get("url") or "").strip()
    ts = (ev.get("ts") or "").strip()
    lines: list[str] = []
    if ts:
        lines.append(f"When: {ts[:10]}")
    lines.append(f"Source: {product} ({etype})")
    if title:
        lines.append(title)
    if text and text != title:
        lines.append(text)
    if url:
        lines.append(url)
    body = "\n".join(lines).strip()
    if len(body) < 4:
        return None
    return body[:32_000]


def _event_memory_kind(ev: dict[str, Any]) -> str:
    etype = (ev.get("event_type") or "").lower()
    if etype in ("note", "email_sent"):
        return "episode"
    if etype in ("activity", "visit", "location", "search"):
        return "episode"
    return "episode"


def promote_parsed_events(
    auth: AuthContext,
    events: list[dict[str, Any]],
) -> dict[str, int]:
    """Idempotent: one scope=archive memory per OLAP event_id."""
    stats = {"events_promoted": 0, "events_skipped": 0, "events_duplicate": 0}
    for ev in events:
        eid = str(ev.get("event_id") or "").strip()
        if not eid:
            stats["events_skipped"] += 1
            continue
        content = _event_memory_content(ev)
        if not content:
            stats["events_skipped"] += 1
            continue
        with transaction() as conn:
            if _memory_exists_for_event(conn, auth, eid):
                stats["events_duplicate"] += 1
                continue
        kind = _event_memory_kind(ev)
        sens = int(ev.get("sensitivity") or 1)
        api = create_memory(
            auth,
            kind=kind,
            content=content,
            scope="archive",
            trust=Trust.IMPORTED.value,
            sensitivity=sens,
        )
        mem_id = UUID(api["id"])
        ver_id = UUID(api["current_version"]["id"])
        with transaction() as conn:
            evid = _evidence_for_event(conn, auth, ev)
            _link_memory_evidence(conn, mem_id, ver_id, evid)
        stats["events_promoted"] += 1
    return stats


def archive_memories_for_dataset(auth: AuthContext, limit: int = 5000) -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT mv.content
            FROM memories m
            JOIN memory_versions mv ON mv.id = m.current_version_id
            JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
            WHERE m.subject_id = %s AND m.scope = 'archive' AND mv.status = 'active'
            ORDER BY m.updated_at DESC
            LIMIT %s
            """,
            (auth.subject_id, max(1, min(limit, 50_000))),
        )
    return [{"content": r["content"], "evidence_event_ids": []} for r in rows]


def promote_backfill_from_parquet(
    auth: AuthContext,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    from virtual_self.archive.olap import ensure_duckdb_views

    before = count_archive_memories(auth)
    con = ensure_duckdb_views()
    try:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        events = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()

    would_promote = 0
    for ev in events:
        eid = str(ev.get("event_id") or "").strip()
        if not eid or not _event_memory_content(ev):
            continue
        with transaction() as conn:
            if not _memory_exists_for_event(conn, auth, eid):
                would_promote += 1

    out: dict[str, Any] = {
        "archive_memories_before": before,
        "parquet_events_scanned": len(events),
        "would_promote": would_promote,
        "dry_run": dry_run,
    }
    if dry_run:
        out["archive_memories_after"] = before
        return out

    pstats = promote_parsed_events(auth, events)
    after = count_archive_memories(auth)
    out.update(pstats)
    out["archive_memories_after"] = after
    return out


def promote_verified_candidates(
    auth: AuthContext,
    verified: list[dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    settings = get_settings()
    stats = {"promoted": 0, "rejected": 0, "duplicate": 0, "personality": 0}
    action_map = {int(a.get("candidate_index", -1)): a.get("action") for a in (actions or [])}

    for i, cand in enumerate(verified):
        if cand.get("verdict") == "unsupported":
            stats["rejected"] += 1
            continue
        if float(cand.get("confidence", 0)) < settings.archive_min_confidence:
            stats["rejected"] += 1
            continue
        act = action_map.get(i, "promote")
        if act == "skip_duplicate":
            stats["duplicate"] += 1
            continue
        content = (cand.get("content") or "").strip()
        if not content:
            stats["rejected"] += 1
            continue
        kind = cand.get("kind") or "preference"
        if act == "personality_proposal" or kind in ("identity", "values"):
            with transaction() as conn:
                pid = uuid7()
                execute(
                    conn,
                    """
                    INSERT INTO archive_personality_proposals (
                      id, subject_id, content, evidence_event_ids, confidence, status
                    ) VALUES (%s, %s, %s, %s::jsonb, %s, 'pending')
                    """,
                    (
                        pid,
                        auth.subject_id,
                        content,
                        json.dumps(cand.get("evidence_event_ids") or []),
                        cand.get("confidence"),
                    ),
                )
            stats["personality"] += 1
            continue

        hits = search_memories(auth, content, limit=3)
        if hits and act != "contradiction":
            stats["duplicate"] += 1
            continue

        sens = 1
        for eid in cand.get("evidence_event_ids") or []:
            ev = events_by_id.get(str(eid))
            if ev:
                sens = max(sens, int(ev.get("sensitivity") or 1))

        api = create_memory(
            auth,
            kind=kind if kind in {"preference", "goal", "episode", "identity", "current_state"} else "preference",
            content=content,
            scope="archive",
            trust=Trust.IMPORTED.value,
            sensitivity=sens,
        )
        mem_id = UUID(api["id"])
        ver_id = UUID(api["current_version"]["id"])
        conf = float(cand.get("confidence", 0.8))
        with transaction() as conn:
            execute(
                conn,
                "UPDATE memory_versions SET confidence = %s WHERE id = %s",
                (conf, ver_id),
            )
            for eid in cand.get("evidence_event_ids") or []:
                ev = events_by_id.get(str(eid))
                if not ev:
                    continue
                evid = _evidence_for_event(conn, auth, ev)
                _link_memory_evidence(conn, mem_id, ver_id, evid)
        stats["promoted"] += 1
    return stats
