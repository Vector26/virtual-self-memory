from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from virtual_self.archive.parsers.base import ArchiveEvent
from virtual_self.archive.paths import duckdb_path, parquet_events_dir

EVENTS_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("provider", pa.string()),
        ("product", pa.string()),
        ("event_type", pa.string()),
        ("ts", pa.string()),
        ("actor", pa.string()),
        ("title", pa.string()),
        ("text", pa.string()),
        ("url", pa.string()),
        ("domain", pa.string()),
        ("geo_lat", pa.float64()),
        ("geo_lon", pa.float64()),
        ("entities", pa.list_(pa.string())),
        ("source_file", pa.string()),
        ("source_ref", pa.string()),
        ("sensitivity", pa.int32()),
        ("content_hash", pa.string()),
        ("year", pa.int32()),
    ]
)


def batch_parquet_paths(batch_tag: str) -> list[Path]:
    return sorted(parquet_events_dir().rglob(f"part-{batch_tag}.parquet"))


def batch_has_events(batch_tag: str) -> bool:
    return bool(batch_parquet_paths(batch_tag))


def load_event_rows_for_batch(batch_tag: str) -> list[dict[str, Any]]:
    paths = batch_parquet_paths(batch_tag)
    if not paths:
        return []
    con = duckdb.connect(":memory:")
    files = ", ".join(f"'{p}'" for p in paths)
    try:
        cur = con.execute(
            f"SELECT * FROM read_parquet([{files}], union_by_name=true)"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except duckdb.Error:
        return []


def read_events_connection_for_batch(batch_tag: str) -> duckdb.DuckDBPyConnection:
    """Parquet reads scoped to one archive zip batch (part-{batch_tag}.parquet)."""
    paths = batch_parquet_paths(batch_tag)
    con = duckdb.connect(":memory:")
    if paths:
        files = ", ".join(f"'{p}'" for p in paths)
        con.execute(
            f"""
            CREATE VIEW events AS
            SELECT * FROM read_parquet([{files}], union_by_name=true)
            """
        )
    else:
        con.execute(_empty_events_view_sql())
    return con


def write_events_parquet(events: list[ArchiveEvent], batch_tag: str) -> Path:
    if not events:
        return parquet_events_dir() / f"empty_{batch_tag}.parquet"
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for ev in events:
        row = ev.to_row()
        key = (row["provider"], row["product"], int(row["year"] or 0))
        grouped.setdefault(key, []).append(row)
    out_path: Path | None = None
    for (provider, product, year), rows in grouped.items():
        out_dir = (
            parquet_events_dir()
            / f"provider={provider}"
            / f"product={product}"
            / f"year={year}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"part-{batch_tag}.parquet"
        table = pa.Table.from_pylist(rows, schema=EVENTS_SCHEMA)
        pq.write_table(table, out_path)
    assert out_path is not None
    return out_path


def _parquet_glob() -> str:
    return str(parquet_events_dir() / "provider=*" / "product=*" / "year=*" / "*.parquet")


def _read_parquet_sql(glob: str) -> str:
    """Hive-style paths share column names with parquet; disable path overrides."""
    return f"read_parquet('{glob}', union_by_name=true, hive_partitioning=false)"


def _has_parquet_files() -> bool:
    return any(parquet_events_dir().rglob("*.parquet"))


def _empty_events_view_sql() -> str:
    return """
            CREATE OR REPLACE VIEW events AS
            SELECT
              CAST(NULL AS VARCHAR) AS event_id,
              CAST(NULL AS VARCHAR) AS provider,
              CAST(NULL AS VARCHAR) AS product,
              CAST(NULL AS VARCHAR) AS event_type,
              CAST(NULL AS VARCHAR) AS ts,
              CAST(NULL AS VARCHAR) AS actor,
              CAST(NULL AS VARCHAR) AS title,
              CAST(NULL AS VARCHAR) AS text,
              CAST(NULL AS VARCHAR) AS url,
              CAST(NULL AS VARCHAR) AS domain,
              CAST(NULL AS DOUBLE) AS geo_lat,
              CAST(NULL AS DOUBLE) AS geo_lon,
              CAST(NULL AS VARCHAR[]) AS entities,
              CAST(NULL AS VARCHAR) AS source_file,
              CAST(NULL AS VARCHAR) AS source_ref,
              CAST(NULL AS INTEGER) AS sensitivity,
              CAST(NULL AS VARCHAR) AS content_hash,
              CAST(NULL AS INTEGER) AS year
            WHERE 1=0
            """


def read_events_connection() -> duckdb.DuckDBPyConnection:
    """Public alias for parquet-backed reads (no DuckDB file lock)."""
    return _memory_events_connection()


def _memory_events_connection() -> duckdb.DuckDBPyConnection:
    """Query parquet in-process without locking the on-disk DuckDB file."""
    con = duckdb.connect(":memory:")
    if _has_parquet_files():
        glob = _parquet_glob()
        con.execute(
            f"""
            CREATE VIEW events AS
            SELECT * FROM {_read_parquet_sql(glob)}
            """
        )
    else:
        con.execute(_empty_events_view_sql())
    return con


def _connect_duckdb_file(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    db_path = duckdb_path()
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return duckdb.connect(str(db_path), read_only=read_only)
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower():
                raise
            last_exc = exc
            time.sleep(0.4 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def ensure_duckdb_views() -> duckdb.DuckDBPyConnection:
    try:
        con = _connect_duckdb_file(read_only=False)
    except duckdb.IOException:
        try:
            con = _connect_duckdb_file(read_only=True)
        except duckdb.IOException:
            return _memory_events_connection()
    if _has_parquet_files():
        glob = _parquet_glob()
        con.execute(
            f"""
            CREATE OR REPLACE VIEW events AS
            SELECT * FROM {_read_parquet_sql(glob)}
            """
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE TABLE events_empty AS
            SELECT
              CAST(NULL AS VARCHAR) AS event_id,
              CAST(NULL AS VARCHAR) AS provider,
              CAST(NULL AS VARCHAR) AS product,
              CAST(NULL AS VARCHAR) AS event_type,
              CAST(NULL AS VARCHAR) AS ts,
              CAST(NULL AS VARCHAR) AS actor,
              CAST(NULL AS VARCHAR) AS title,
              CAST(NULL AS VARCHAR) AS text,
              CAST(NULL AS VARCHAR) AS url,
              CAST(NULL AS VARCHAR) AS domain,
              CAST(NULL AS DOUBLE) AS geo_lat,
              CAST(NULL AS DOUBLE) AS geo_lon,
              CAST(NULL AS VARCHAR[]) AS entities,
              CAST(NULL AS VARCHAR) AS source_file,
              CAST(NULL AS VARCHAR) AS source_ref,
              CAST(NULL AS INTEGER) AS sensitivity,
              CAST(NULL AS VARCHAR) AS content_hash,
              CAST(NULL AS INTEGER) AS year
            WHERE 1=0
            """
        )
        con.execute("CREATE OR REPLACE VIEW events AS SELECT * FROM events_empty")
    con.execute(
        """
        CREATE OR REPLACE VIEW daily_activity AS
        SELECT CAST(ts AS DATE) AS day, product, COUNT(*) AS n
        FROM events WHERE ts IS NOT NULL
        GROUP BY 1, 2
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW top_domains AS
        SELECT domain, COUNT(*) AS n FROM events
        WHERE domain IS NOT NULL AND domain != ''
        GROUP BY 1 ORDER BY n DESC LIMIT 100
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW top_channels AS
        SELECT product, COUNT(*) AS n FROM events GROUP BY 1 ORDER BY n DESC
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW search_queries_by_month AS
        SELECT strftime(CAST(ts AS TIMESTAMP), '%Y-%m') AS month, COUNT(*) AS n
        FROM events
        WHERE event_type = 'activity' AND (title ILIKE '%search%' OR text ILIKE '%search%')
        GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW places_visited AS
        SELECT title, geo_lat, geo_lon, COUNT(*) AS n
        FROM events WHERE event_type = 'location'
        GROUP BY 1, 2, 3
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW sent_mail_by_contact AS
        SELECT title, COUNT(*) AS n FROM events
        WHERE product = 'mail' AND event_type = 'email_sent'
        GROUP BY 1 ORDER BY n DESC LIMIT 200
        """
    )
    return con


def parquet_product_counts() -> dict[str, int]:
    """Row counts by `product` column across all parquet batches (ground truth)."""
    if not _has_parquet_files():
        return {}
    con = duckdb.connect(":memory:")
    glob = _parquet_glob()
    try:
        rows = con.execute(
            f"""
            SELECT product, COUNT(*)::BIGINT AS n
            FROM {_read_parquet_sql(glob)}
            GROUP BY 1
            """
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}
    except duckdb.Error:
        return {}


def product_counts() -> dict[str, int]:
    return parquet_product_counts()


def olap_refresh() -> dict[str, Any]:
    """Rebuild on-disk DuckDB views and return counts vs parquet."""
    ensure_duckdb_views().close()
    disk = parquet_product_counts()
    views = product_counts()
    return {
        "parquet_batches": sum(1 for _ in parquet_events_dir().rglob("*.parquet")),
        "product_counts": views,
        "matches_disk": views == disk,
    }


def fetch_events_by_ids(event_ids: list[str]) -> list[dict[str, Any]]:
    if not event_ids:
        return []
    con = _memory_events_connection()
    placeholders = ",".join("?" for _ in event_ids)
    try:
        cur = con.execute(
            f"SELECT * FROM events WHERE event_id IN ({placeholders})",
            event_ids,
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except duckdb.Error:
        return []


def sql_stats(product: str) -> dict[str, Any]:
    con = _memory_events_connection()
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM events WHERE product = ?", [product]
        ).fetchone()[0]
        minmax = con.execute(
            "SELECT MIN(ts), MAX(ts) FROM events WHERE product = ?", [product]
        ).fetchone()
        return {"count": int(total), "min_ts": minmax[0], "max_ts": minmax[1]}
    except duckdb.Error:
        return {"count": 0}
