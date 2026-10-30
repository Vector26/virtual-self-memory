from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from virtual_self.config import get_settings
from virtual_self.persistence.db import fetch_all, fetch_one
from virtual_self.providers.embeddings import embed_one, vector_literal
from virtual_self.services.retrieval_queries import queries_for_retrieval

_LEX_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "again",
        "all",
        "also",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "being",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "had",
        "has",
        "have",
        "her",
        "here",
        "him",
        "his",
        "how",
        "if",
        "into",
        "is",
        "it",
        "its",
        "just",
        "know",
        "like",
        "may",
        "me",
        "more",
        "most",
        "much",
        "must",
        "my",
        "new",
        "not",
        "now",
        "of",
        "on",
        "one",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "same",
        "say",
        "see",
        "she",
        "should",
        "so",
        "some",
        "such",
        "take",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "up",
        "use",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_NAME_LIKE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*$")

_ARCHIVEISH = re.compile(
    r"\b("
    r"archive|archives|takeout|google\s+takeout|chrome\s+history|gmail|"
    r"youtube|location\s+history|my\s+activity|export|timeline|"
    r"years?\s+ago|old\s+photos|backup|take\s+out"
    r")\b",
    re.IGNORECASE,
)

_ILIKE_BOOST_PER_TOKEN = 0.08
_TSQUERY_FN: str | None = None


def high_signal_tokens(query: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in re.findall(r"\b\w+\b", query):
        if len(raw) < 3:
            continue
        key = raw.casefold()
        if key in _LEX_STOPWORDS or key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def is_person_query(query: str) -> bool:
    variants = queries_for_retrieval(query)
    if not variants:
        return False
    for variant in variants:
        core = variant.rstrip("?").strip()
        if _NAME_LIKE.match(core):
            return True
    return False


def is_archiveish_query(query: str) -> bool:
    return bool(_ARCHIVEISH.search(query))


def scope_rank_multiplier(scope: str, *, person_query: bool, archiveish: bool) -> float:
    """Prefer intentional scopes over ambient archive unless the query targets archive data."""
    if archiveish:
        return 1.0
    if scope == "archive":
        return 0.35
    return 1.0


def _lexical_tsquery_fn(conn) -> str:
    global _TSQUERY_FN
    if _TSQUERY_FN is not None:
        return _TSQUERY_FN
    row = fetch_one(
        conn,
        "SELECT to_regprocedure('websearch_to_tsquery(regconfig,text)') IS NOT NULL AS supported",
        (),
    )
    supported = bool(row and row.get("supported"))
    _TSQUERY_FN = "websearch_to_tsquery" if supported else "plainto_tsquery"
    return _TSQUERY_FN


def _ilike_boost_sql(tokens: list[str], *, content_sql: str) -> tuple[str, list[str]]:
    if not tokens:
        return "0::float8", []
    parts: list[str] = []
    params: list[str] = []
    for token in tokens:
        parts.append(
            f"(CASE WHEN {content_sql} ILIKE '%%' || %s || '%%' THEN {_ILIKE_BOOST_PER_TOKEN}::float8 ELSE 0 END)"
        )
        params.append(token.casefold())
    return " + ".join(parts), params


def _scope_mult_sql(
    *,
    archiveish: bool,
    scope_sql: str,
) -> tuple[str, list[Any]]:
    return (
        f"""CASE {scope_sql}
          WHEN 'archive' THEN CASE WHEN %s THEN 1.0::float8 ELSE 0.35::float8 END
          ELSE 1.0::float8
        END""",
        [archiveish],
    )


def _validity_sql(*, alias: str = "m") -> str:
    a = alias
    return f"""
      AND ({a}.valid_from IS NULL OR {a}.valid_from <= %s)
      AND ({a}.valid_to IS NULL OR {a}.valid_to > %s)
      AND ({a}.invalidated_at IS NULL OR {a}.invalidated_at > %s)
    """


def _chapter_scope_sql(*, alias: str = "m") -> str:
    a = alias
    return f"""
      AND (
        {a}.chapter_id = ANY(%s::uuid[])
        OR {a}.id IN (
          SELECT memory_id FROM life_chapter_evidence
          WHERE chapter_id = ANY(%s::uuid[])
        )
      )
    """


def chapter_scoped_hybrid_search(
    conn,
    subject_id: UUID,
    query: str,
    *,
    chapter_ids: list[UUID],
    sensitivity_max: int,
    limit: int = 20,
    as_of: datetime | None = None,
    prefer_open_validity: bool = False,
    apply_validity: bool = True,
) -> list[dict[str, Any]]:
    """Hybrid search limited to chapters; validity-aware; no recency tie-break."""
    q = query.strip()
    limit = max(1, min(limit, 100))
    if not chapter_ids:
        return []

    archiveish = is_archiveish_query(q)
    tokens = high_signal_tokens(q)
    ts_fn = _lexical_tsquery_fn(conn)
    validity_clause = _validity_sql() if apply_validity and as_of is not None else ""
    validity_params: list[Any] = [as_of, as_of, as_of] if validity_clause else []
    chapter_params: list[Any] = [chapter_ids, chapter_ids]
    open_boost_m = (
        " + CASE WHEN m.valid_to IS NULL THEN 0.15::float8 ELSE 0::float8 END"
        if prefer_open_validity
        else ""
    )
    open_boost_fused = (
        " + CASE WHEN COALESCE(l.valid_to, s.valid_to) IS NULL THEN 0.15::float8 ELSE 0::float8 END"
        if prefer_open_validity
        else ""
    )

    ilike_sql, ilike_params = _ilike_boost_sql(tokens, content_sql="COALESCE(l.content, s.content)")
    scope_mult_sql, scope_mult_params = _scope_mult_sql(
        archiveish=archiveish,
        scope_sql="COALESCE(l.scope, s.scope)",
    )

    if not q:
        rows = fetch_all(
            conn,
            f"""
            SELECT m.id AS memory_id, mv.content, mv.trust, mv.sensitivity, m.kind,
                   m.updated_at, m.chapter_id, m.valid_from, m.valid_to, m.time_precision,
                   (1.0::float8{open_boost_m}) AS rank_score
            FROM memories m
            JOIN memory_versions mv ON mv.id = m.current_version_id
            JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
            WHERE m.subject_id = %s AND mv.status = 'active' AND mv.sensitivity <= %s
              {_chapter_scope_sql()}
              {validity_clause}
            ORDER BY rank_score DESC
            LIMIT %s
            """,
            (
                subject_id,
                sensitivity_max,
                *chapter_params,
                *validity_params,
                limit,
            ),
        )
        return [_row(r, "chapter_scope") for r in rows]

    qvec = embed_one(q)
    if qvec is None or len(qvec) != get_settings().embed_dims:
        return _chapter_scoped_lexical(
            conn,
            subject_id,
            q,
            chapter_ids=chapter_ids,
            sensitivity_max=sensitivity_max,
            limit=limit,
            ts_fn=ts_fn,
            tokens=tokens,
            archiveish=archiveish,
            as_of=as_of,
            apply_validity=apply_validity,
            prefer_open_validity=prefer_open_validity,
        )

    vec_lit = vector_literal(qvec)
    rows = fetch_all(
        conn,
        f"""
        WITH lexical AS (
          SELECT mv.id AS version_id, m.id AS memory_id, mv.content, mv.trust, mv.sensitivity,
                 m.kind, m.updated_at, m.scope, m.chapter_id, m.valid_from, m.valid_to,
                 m.time_precision,
                 ts_rank_cd(mv.search_vector, {ts_fn}('english', %s)) AS lex_score,
                 ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(mv.search_vector, {ts_fn}('english', %s)) DESC
                 ) AS lex_rank
          FROM memories m
          JOIN memory_versions mv ON mv.id = m.current_version_id
          JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
          WHERE m.subject_id = %s AND mv.status = 'active' AND mv.sensitivity <= %s
            {_chapter_scope_sql()}
            {validity_clause}
            AND mv.search_vector @@ {ts_fn}('english', %s)
          LIMIT 50
        ),
        semantic AS (
          SELECT mv.id AS version_id, m.id AS memory_id, mv.content, mv.trust, mv.sensitivity,
                 m.kind, m.updated_at, m.scope, m.chapter_id, m.valid_from, m.valid_to,
                 m.time_precision,
                 1 - (me.embedding <=> %s::vector) AS sem_score,
                 ROW_NUMBER() OVER (ORDER BY me.embedding <=> %s::vector) AS sem_rank
          FROM memories m
          JOIN memory_versions mv ON mv.id = m.current_version_id
          JOIN memory_embeddings me ON me.memory_version_id = mv.id
          JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
          WHERE m.subject_id = %s AND mv.status = 'active' AND mv.sensitivity <= %s
            {_chapter_scope_sql()}
            {validity_clause}
          ORDER BY me.embedding <=> %s::vector
          LIMIT 50
        ),
        fused AS (
          SELECT COALESCE(l.memory_id, s.memory_id) AS memory_id,
                 COALESCE(l.content, s.content) AS content,
                 COALESCE(l.trust, s.trust) AS trust,
                 COALESCE(l.sensitivity, s.sensitivity) AS sensitivity,
                 COALESCE(l.kind, s.kind) AS kind,
                 COALESCE(l.updated_at, s.updated_at) AS updated_at,
                 COALESCE(l.chapter_id, s.chapter_id) AS chapter_id,
                 COALESCE(l.valid_from, s.valid_from) AS valid_from,
                 COALESCE(l.valid_to, s.valid_to) AS valid_to,
                 COALESCE(l.time_precision, s.time_precision) AS time_precision,
                 (
                   COALESCE(1.0 / (60 + l.lex_rank), 0) + COALESCE(1.0 / (60 + s.sem_rank), 0)
                 ) * ({scope_mult_sql}) + ({ilike_sql}){open_boost_fused} AS rrf
          FROM lexical l
          FULL OUTER JOIN semantic s ON l.memory_id = s.memory_id
        )
        SELECT * FROM fused ORDER BY rrf DESC LIMIT %s
        """,
        (
            q,
            q,
            subject_id,
            sensitivity_max,
            *chapter_params,
            *validity_params,
            q,
            vec_lit,
            vec_lit,
            subject_id,
            sensitivity_max,
            *chapter_params,
            *validity_params,
            vec_lit,
            *scope_mult_params,
            *ilike_params,
            limit,
        ),
    )
    if rows:
        return [_row(r, "chapter_hybrid") for r in rows]
    return _chapter_scoped_lexical(
        conn,
        subject_id,
        q,
        chapter_ids=chapter_ids,
        sensitivity_max=sensitivity_max,
        limit=limit,
        ts_fn=ts_fn,
        tokens=tokens,
        archiveish=archiveish,
        as_of=as_of,
        apply_validity=apply_validity,
        prefer_open_validity=prefer_open_validity,
    )


def _chapter_scoped_lexical(
    conn,
    subject_id,
    q,
    *,
    chapter_ids: list[UUID],
    sensitivity_max: int,
    limit: int,
    ts_fn: str,
    tokens: list[str],
    archiveish: bool,
    as_of: datetime | None,
    apply_validity: bool,
    prefer_open_validity: bool,
) -> list[dict[str, Any]]:
    validity_clause = _validity_sql() if apply_validity and as_of is not None else ""
    validity_params: list[Any] = [as_of, as_of, as_of] if validity_clause else []
    chapter_params: list[Any] = [chapter_ids, chapter_ids]
    open_boost_m = (
        " + CASE WHEN m.valid_to IS NULL THEN 0.15::float8 ELSE 0::float8 END"
        if prefer_open_validity
        else ""
    )
    ilike_sql, ilike_params = _ilike_boost_sql(tokens, content_sql="mv.content")
    scope_mult_sql, scope_mult_params = _scope_mult_sql(
        archiveish=archiveish,
        scope_sql="m.scope",
    )
    token_or = " OR ".join("mv.content ILIKE '%%' || %s || '%%'" for _ in tokens) if tokens else "FALSE"
    token_params = [t.casefold() for t in tokens]

    rows = fetch_all(
        conn,
        f"""
        SELECT m.id AS memory_id, mv.content, mv.trust, mv.sensitivity, m.kind, m.updated_at,
               m.chapter_id, m.valid_from, m.valid_to, m.time_precision,
               (
                 COALESCE(ts_rank_cd(mv.search_vector, {ts_fn}('english', %s)), 0)
                 + ({ilike_sql})
               ) * ({scope_mult_sql}){open_boost_m} AS rank_score
        FROM memories m
        JOIN memory_versions mv ON mv.id = m.current_version_id
        JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
        WHERE m.subject_id = %s AND mv.status = 'active' AND mv.sensitivity <= %s
          {_chapter_scope_sql()}
          {validity_clause}
          AND (
            mv.search_vector @@ {ts_fn}('english', %s)
            OR mv.content ILIKE '%%' || %s || '%%'
            OR ({token_or})
          )
        ORDER BY rank_score DESC NULLS LAST
        LIMIT %s
        """,
        (
            q,
            *ilike_params,
            *scope_mult_params,
            subject_id,
            sensitivity_max,
            *chapter_params,
            *validity_params,
            q,
            q,
            *token_params,
            limit,
        ),
    )
    return [_row(r, "chapter_lexical") for r in rows]


def hybrid_search(
    conn,
    subject_id: UUID,
    query: str,
    *,
    sensitivity_max: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    q = query.strip()
    limit = max(1, min(limit, 100))

    if not q:
        return _recent(conn, subject_id, sensitivity_max, limit)

    person_query = is_person_query(q)
    archiveish = is_archiveish_query(q)
    tokens = high_signal_tokens(q)
    ts_fn = _lexical_tsquery_fn(conn)

    qvec = embed_one(q)
    if qvec is None or len(qvec) != get_settings().embed_dims:
        return _lexical_only(
            conn,
            subject_id,
            q,
            sensitivity_max,
            limit,
            ts_fn=ts_fn,
            tokens=tokens,
            person_query=person_query,
            archiveish=archiveish,
        )

    vec_lit = vector_literal(qvec)
    ilike_sql, ilike_params = _ilike_boost_sql(
        tokens, content_sql="COALESCE(l.content, s.content)"
    )
    scope_mult_sql, scope_mult_params = _scope_mult_sql(
        archiveish=archiveish,
        scope_sql="COALESCE(l.scope, s.scope)",
    )

    rows = fetch_all(
        conn,
        f"""
        WITH lexical AS (
          SELECT mv.id AS version_id, m.id AS memory_id, mv.content, mv.trust, mv.sensitivity,
                 m.kind, m.updated_at, m.scope,
                 ts_rank_cd(mv.search_vector, {ts_fn}('english', %s)) AS lex_score,
                 ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(mv.search_vector, {ts_fn}('english', %s)) DESC
                 ) AS lex_rank
          FROM memories m
          JOIN memory_versions mv ON mv.id = m.current_version_id
          JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
          WHERE m.subject_id = %s AND mv.status = 'active' AND mv.sensitivity <= %s
            AND mv.search_vector @@ {ts_fn}('english', %s)
          LIMIT 50
        ),
        semantic AS (
          SELECT mv.id AS version_id, m.id AS memory_id, mv.content, mv.trust, mv.sensitivity,
                 m.kind, m.updated_at, m.scope,
                 1 - (me.embedding <=> %s::vector) AS sem_score,
                 ROW_NUMBER() OVER (ORDER BY me.embedding <=> %s::vector) AS sem_rank
          FROM memories m
          JOIN memory_versions mv ON mv.id = m.current_version_id
          JOIN memory_embeddings me ON me.memory_version_id = mv.id
          JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
          WHERE m.subject_id = %s AND mv.status = 'active' AND mv.sensitivity <= %s
          ORDER BY me.embedding <=> %s::vector
          LIMIT 50
        ),
        fused AS (
          SELECT COALESCE(l.memory_id, s.memory_id) AS memory_id,
                 COALESCE(l.content, s.content) AS content,
                 COALESCE(l.trust, s.trust) AS trust,
                 COALESCE(l.sensitivity, s.sensitivity) AS sensitivity,
                 COALESCE(l.kind, s.kind) AS kind,
                 COALESCE(l.updated_at, s.updated_at) AS updated_at,
                 (
                   COALESCE(1.0 / (60 + l.lex_rank), 0) + COALESCE(1.0 / (60 + s.sem_rank), 0)
                 ) * ({scope_mult_sql}) + ({ilike_sql}) AS rrf
          FROM lexical l
          FULL OUTER JOIN semantic s ON l.memory_id = s.memory_id
        )
        SELECT * FROM fused ORDER BY rrf DESC LIMIT %s
        """,
        (
            q,
            q,
            subject_id,
            sensitivity_max,
            q,
            vec_lit,
            vec_lit,
            subject_id,
            sensitivity_max,
            vec_lit,
            *scope_mult_params,
            *ilike_params,
            limit,
        ),
    )
    if rows:
        return [_row(r, "hybrid") for r in rows]
    return _lexical_only(
        conn,
        subject_id,
        q,
        sensitivity_max,
        limit,
        ts_fn=ts_fn,
        tokens=tokens,
        person_query=person_query,
        archiveish=archiveish,
    )


def _lexical_only(
    conn,
    subject_id,
    q,
    sensitivity_max,
    limit,
    *,
    ts_fn: str | None = None,
    tokens: list[str] | None = None,
    person_query: bool | None = None,
    archiveish: bool | None = None,
):
    ts_fn = ts_fn or _lexical_tsquery_fn(conn)
    tokens = tokens if tokens is not None else high_signal_tokens(q)
    person_query = is_person_query(q) if person_query is None else person_query
    archiveish = is_archiveish_query(q) if archiveish is None else archiveish

    ilike_sql, ilike_params = _ilike_boost_sql(tokens, content_sql="mv.content")
    scope_mult_sql, scope_mult_params = _scope_mult_sql(
        archiveish=archiveish,
        scope_sql="m.scope",
    )
    token_or = " OR ".join("mv.content ILIKE '%%' || %s || '%%'" for _ in tokens) if tokens else "FALSE"
    token_params = [t.casefold() for t in tokens]

    rows = fetch_all(
        conn,
        f"""
        SELECT m.id AS memory_id, mv.content, mv.trust, mv.sensitivity, m.kind, m.updated_at,
               (
                 COALESCE(ts_rank_cd(mv.search_vector, {ts_fn}('english', %s)), 0)
                 + ({ilike_sql})
               ) * ({scope_mult_sql}) AS rank_score
        FROM memories m
        JOIN memory_versions mv ON mv.id = m.current_version_id
        JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
        WHERE m.subject_id = %s AND mv.status = 'active' AND mv.sensitivity <= %s
          AND (
            mv.search_vector @@ {ts_fn}('english', %s)
            OR mv.content ILIKE '%%' || %s || '%%'
            OR ({token_or})
          )
        ORDER BY rank_score DESC NULLS LAST, m.updated_at DESC
        LIMIT %s
        """,
        (
            q,
            *ilike_params,
            *scope_mult_params,
            subject_id,
            sensitivity_max,
            q,
            q,
            *token_params,
            limit,
        ),
    )
    return [_row(r, "lexical") for r in rows]


def _recent(conn, subject_id, sensitivity_max, limit):
    rows = fetch_all(
        conn,
        """
        SELECT m.id AS memory_id, mv.content, mv.trust, mv.sensitivity, m.kind, m.updated_at
        FROM memories m
        JOIN memory_versions mv ON mv.id = m.current_version_id
        JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
        WHERE m.subject_id = %s AND mv.status = 'active' AND mv.sensitivity <= %s
        ORDER BY m.updated_at DESC
        LIMIT %s
        """,
        (subject_id, sensitivity_max, limit),
    )
    return [_row(r, "recency") for r in rows]


def _row(r: dict, mode: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(r["memory_id"]),
        "kind": r["kind"],
        "content": r["content"],
        "trust": r["trust"],
        "sensitivity": r["sensitivity"],
        "updated_at": r["updated_at"].isoformat(),
        "retrieval": mode,
    }
    if r.get("chapter_id") is not None:
        out["chapter_id"] = str(r["chapter_id"])
    if r.get("valid_from") is not None:
        out["valid_from"] = r["valid_from"].isoformat()
    if r.get("valid_to") is not None:
        out["valid_to"] = r["valid_to"].isoformat()
    if r.get("time_precision") is not None:
        out["time_precision"] = r["time_precision"]
    return out
