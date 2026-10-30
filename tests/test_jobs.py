from __future__ import annotations

import uuid

from virtual_self.persistence.db import close_pool, transaction
from virtual_self.services.jobs import claim_next, complete_job, enqueue, release_stale_leases


def test_job_claim_and_complete(ensure_db):
    close_pool()
    job_type = f"test.job.{uuid.uuid4()}"
    dedupe = f"test-{uuid.uuid4()}"
    with transaction() as conn:
        jid = enqueue(conn, job_type, {"memory_id": "test"}, dedupe_key=dedupe)
    job = None
    for _ in range(20):
        job = claim_next("test-worker", job_type=job_type)
        if job is None:
            break
        if job["id"] == jid:
            break
        complete_job(job["id"], True)
    assert job is not None
    assert job["id"] == jid
    complete_job(job["id"], True)
    release_stale_leases()
