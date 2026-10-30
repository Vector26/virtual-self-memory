from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from virtual_self.config import get_settings
from virtual_self.observability.metrics import DB_ERRORS

_pool: ConnectionPool | None = None


def init_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=get_settings().database_url,
            kwargs={"row_factory": dict_row, "autocommit": False},
            min_size=1,
            max_size=10,
            timeout=30,
            max_lifetime=1800,
            reconnect_timeout=60,
            check=ConnectionPool.check_connection,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool:
    return init_pool()


@contextmanager
def connection() -> Iterator[Connection]:
    p = pool()
    with p.connection() as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[Connection]:
    with connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def fetch_one(conn: Connection, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    try:
        cur = conn.execute(sql, params)
        return cur.fetchone()
    except Exception:
        DB_ERRORS.labels(operation="fetch_one").inc()
        raise


def fetch_all(conn: Connection, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(sql, params)
        return list(cur.fetchall())
    except Exception:
        DB_ERRORS.labels(operation="fetch_all").inc()
        raise


def execute(conn: Connection, sql: str, params: tuple | dict = ()) -> int:
    try:
        cur = conn.execute(sql, params)
        return cur.rowcount
    except Exception:
        DB_ERRORS.labels(operation="execute").inc()
        raise


def health_check() -> bool:
    try:
        with connection() as conn:
            row = fetch_one(conn, "SELECT 1 AS ok")
            return row is not None and row.get("ok") == 1
    except Exception:
        return False
