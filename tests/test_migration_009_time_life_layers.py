from __future__ import annotations

from uuid import uuid4

from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, fetch_one
from virtual_self.persistence.migrate import apply_migrations

LIFE_TABLES = {
    "life_strands",
    "life_chapters",
    "life_patterns",
    "chapter_links",
}

STRAND_SLUGS = {"career", "relationships", "mind", "craft", "place"}

MEMORY_TIME_COLS = {
    "authored_at",
    "valid_from",
    "valid_to",
    "time_precision",
    "time_source",
    "ingested_at",
    "invalidated_at",
    "chapter_id",
    "pattern_id",
}

ARCHIVE_TIME_COLS = {
    "authored_at",
    "valid_from",
    "valid_to",
    "time_precision",
    "time_source",
    "invalidated_at",
}


def _table_columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def test_migration_009_time_life_layers_idempotent_and_seeds(ensure_db):
    apply_migrations()
    apply_migrations()

    with connection() as conn:
        tables = {
            r["table_name"]
            for r in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = ANY(%s)
                """,
                (list(LIFE_TABLES),),
            ).fetchall()
        }
        assert tables == LIFE_TABLES

        slugs = {
            r["slug"]
            for r in conn.execute("SELECT slug FROM life_strands ORDER BY sort_order").fetchall()
        }
        assert slugs == STRAND_SLUGS

        mem_cols = _table_columns(conn, "memories")
        assert MEMORY_TIME_COLS <= mem_cols

        src_cols = _table_columns(conn, "sources")
        assert ARCHIVE_TIME_COLS <= src_cols

        ev_cols = _table_columns(conn, "evidence_records")
        assert ARCHIVE_TIME_COLS | {"ingested_at", "chapter_id", "pattern_id"} <= ev_cols

        authored_cols = _table_columns(conn, "authored_texts")
        assert {"chapter_id", "pattern_id", "time_precision", "authored_at"} <= authored_cols

    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")

    with connection() as conn:
        career = fetch_one(conn, "SELECT id FROM life_strands WHERE slug = %s", ("career",))
        chapter_id = uuid7()
        pattern_id = uuid7()
        conn.execute(
            """
            INSERT INTO life_chapters (
              id, subject_id, strand_id, title, status,
              valid_from, time_precision, valid_from_precision, edge_fuzz_days
            ) VALUES (
              %s, %s, %s, 'smoke chapter', 'active',
              NOW(), 'month', 'month', 14
            )
            """,
            (chapter_id, auth.subject_id, career["id"]),
        )
        conn.execute(
            """
            INSERT INTO life_patterns (
              id, subject_id, chapter_id, name, status, time_precision
            ) VALUES (%s, %s, %s, 'smoke pattern', 'active', 'year')
            """,
            (pattern_id, auth.subject_id, chapter_id),
        )

        link_id = uuid7()
        other_chapter = uuid7()
        conn.execute(
            """
            INSERT INTO life_chapters (
              id, subject_id, strand_id, title, status, time_precision
            ) VALUES (%s, %s, %s, 'parallel smoke', 'proposed', 'unknown')
            """,
            (other_chapter, auth.subject_id, career["id"]),
        )
        conn.execute(
            """
            INSERT INTO chapter_links (
              id, subject_id, from_chapter_id, to_chapter_id, relationship
            ) VALUES (%s, %s, %s, %s, 'parallel')
            """,
            (link_id, auth.subject_id, chapter_id, other_chapter),
        )

        dup = conn.execute(
            """
            INSERT INTO chapter_links (
              id, subject_id, from_chapter_id, to_chapter_id, relationship
            ) VALUES (%s, %s, %s, %s, 'parallel')
            ON CONFLICT (from_chapter_id, to_chapter_id, relationship) DO NOTHING
            """,
            (uuid7(), auth.subject_id, chapter_id, other_chapter),
        )
        assert dup.rowcount == 0

        mem_id = uuid7()
        ver_id = uuid7()
        conn.execute(
            """
            INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
            VALUES (%s, 'memory', %s, 1)
            """,
            (mem_id, auth.subject_id),
        )
        conn.execute(
            """
            INSERT INTO memories (
              id, subject_id, kind, chapter_id, pattern_id,
              time_precision, time_source, authored_at
            ) VALUES (%s, %s, 'episode', %s, %s, 'day', 'calendar', NOW())
            """,
            (mem_id, auth.subject_id, chapter_id, pattern_id),
        )
        conn.execute(
            """
            INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
            VALUES (%s, 'memory_version', %s, 1)
            """,
            (ver_id, auth.subject_id),
        )
        conn.execute(
            """
            INSERT INTO memory_versions (
              id, memory_id, version, content, time_precision, time_source
            ) VALUES (%s, %s, 1, %s, 'day', 'calendar')
            """,
            (ver_id, mem_id, f"temporal smoke {uuid4()}"),
        )
        conn.execute(
            "UPDATE memories SET current_version_id = %s WHERE id = %s",
            (ver_id, mem_id),
        )

        src_id = uuid7()
        conn.execute(
            """
            INSERT INTO sources (
              id, subject_id, source_type, external_id, time_precision, time_source
            ) VALUES (%s, %s, 'archive_event', %s, 'instant', 'exif')
            """,
            (src_id, auth.subject_id, f"evt-{uuid4()}"),
        )
        conn.commit()

    assert career is not None
