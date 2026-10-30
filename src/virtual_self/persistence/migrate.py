from __future__ import annotations

from pathlib import Path

from virtual_self.persistence.db import connection


_MIGRATION_ADVISORY_LOCK_KEY = 0x56534D5F4D4947  # "VSM_MIG"


def apply_migrations() -> None:
    root = Path(__file__).resolve().parents[3]
    mig_dir = root / "migrations"
    files = sorted(mig_dir.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations in {mig_dir}")
    with connection() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_ADVISORY_LOCK_KEY,))
        try:
            for sql_path in files:
                conn.execute(sql_path.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_ADVISORY_LOCK_KEY,))
