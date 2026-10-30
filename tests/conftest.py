from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
_TEST_DB_URL = "postgresql://virtual_self:virtual_self@127.0.0.1:54329/virtual_self_test"
# Isolate ledger/archive tests from prod unless explicitly opted in.
if os.environ.get("VSM_PYTEST_USE_PROD_DB") != "1":
    os.environ["VSM_DATABASE_URL"] = _TEST_DB_URL
else:
    os.environ.setdefault(
        "VSM_DATABASE_URL",
        "postgresql://virtual_self:virtual_self@127.0.0.1:54329/virtual_self",
    )
os.environ.setdefault("VSM_BOOTSTRAP_TOKEN", "test-bootstrap-token")
os.environ.setdefault("VSM_METRICS_ENABLED", "false")
os.environ.setdefault("VSM_ENRICH_ON_IMPORT", "false")
os.environ.setdefault("VSM_REQUIRE_AGENT_ENRICHMENT", "false")
os.environ.setdefault("VSM_ENRICH_NOTIFY_MILESTONES", "false")
os.environ.setdefault("VSM_NOTIFY_ENABLED", "false")

_DB_LOCK_PATH = Path("/tmp/vsm_pytest_shared_db.lock")
# request.fixturenames omits transitive deps; include fixtures that pull in ensure_db.
_DB_SERIAL_FIXTURES = frozenset({"ensure_db", "client", "auth"})


def _db_up() -> bool:
    try:
        import psycopg

        with psycopg.connect(os.environ["VSM_DATABASE_URL"], connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def ensure_db():
    if not _db_up():
        subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, check=False)
        import time

        for _ in range(30):
            if _db_up():
                break
            time.sleep(1)
    if not _db_up():
        pytest.skip("postgres not available on 54329")
    from virtual_self.persistence.db import init_pool
    from virtual_self.persistence.migrate import apply_migrations

    _DB_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_DB_LOCK_PATH, "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            init_pool()
            apply_migrations()
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


@pytest.fixture(autouse=True)
def _vsm_db_serial_guard(request):
    """Serialize tests that share the single pytest Postgres database."""
    if os.environ.get("VSM_PYTEST_USE_PROD_DB") == "1":
        yield
        return
    if not _DB_SERIAL_FIXTURES.intersection(request.fixturenames):
        yield
        return
    _DB_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_DB_LOCK_PATH, "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


@pytest.fixture
def client(ensure_db):
    from virtual_self.config import get_settings
    from virtual_self.persistence.db import close_pool

    get_settings.cache_clear()
    close_pool()
    from virtual_self.api.app import create_app

    with TestClient(create_app()) as c:
        yield c
    close_pool()


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {os.environ['VSM_BOOTSTRAP_TOKEN']}"}
