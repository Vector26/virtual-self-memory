from __future__ import annotations

import socket
import time

from virtual_self.config import get_settings
from virtual_self.observability.logging import configure_logging, get_logger
from virtual_self.observability.metrics import JOB_RUNS
from virtual_self.persistence.db import init_pool, transaction
from virtual_self.services.jobs import claim_next, complete_job, release_stale_leases

log = get_logger(__name__)
OWNER = f"{socket.gethostname()}-worker"


def handle_job(job: dict) -> None:
    from uuid import UUID

    from virtual_self.persistence.db import fetch_one, transaction
    from virtual_self.services.indexing import index_memory_version

    jtype = job["job_type"]
    if jtype == "memory.index":
        ref = job.get("payload_ref") or {}
        vid = ref.get("memory_version_id")
        if not vid:
            raise ValueError("memory.index: missing memory_version_id in payload_ref")
        with transaction() as conn:
            row = fetch_one(conn, "SELECT content FROM memory_versions WHERE id = %s", (UUID(vid),))
            if row:
                index_memory_version(conn, UUID(vid), row["content"])
        return
    if jtype in ("projection.invalidate", "memory.purge_projections"):
        log.info("job_noop_projection", job_id=str(job["id"]), job_type=jtype)
        return
    if jtype.startswith("archive."):
        from uuid import UUID

        from virtual_self.auth.credentials import resolve_auth
        from virtual_self.config import get_settings
        from virtual_self.archive.run import process_archive_file, run_pipeline
        from virtual_self.archive.sync import sync_drive_and_inbox
        from virtual_self.persistence.db import fetch_one, transaction

        ref = job.get("payload_ref") or {}
        settings = get_settings()
        if settings.bootstrap_token is None:
            raise RuntimeError("VSM_BOOTSTRAP_TOKEN required for archive jobs")
        auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
        if jtype == "archive.sync":
            sync_drive_and_inbox(auth.subject_id)
            return
        if jtype == "archive.run":
            run_pipeline(auth, sync_first=ref.get("sync_first", True))
            return
        if jtype in ("archive.unpack", "archive.parse", "archive.analyze", "archive.promote"):
            fid = ref.get("archive_file_id")
            if not fid:
                return
            with transaction() as conn:
                row = fetch_one(conn, "SELECT * FROM archive_files WHERE id = %s", (UUID(fid),))
            if not row:
                return
            state = str(row.get("state") or "")
            if state in ("done", "failed"):
                return
            if jtype == "archive.unpack" and state != "downloaded":
                return
            if jtype == "archive.parse" and state not in ("unpacked",):
                return
            if jtype == "archive.analyze" and state in ("analyzed", "promoted"):
                return
            if jtype == "archive.promote" and state in ("promoted",):
                return
            if jtype == "archive.unpack" or jtype == "archive.parse":
                process_archive_file(
                    auth,
                    row,
                    analyze=jtype != "archive.parse",
                    promote=jtype != "archive.parse",
                )
            elif jtype == "archive.analyze":
                process_archive_file(auth, row, analyze=True, promote=False)
            else:
                process_archive_file(auth, row, analyze=False, promote=True)
            return
        log.warning("archive_job_unknown", job_type=jtype)
        return
    if jtype == "chat.memory.extract":
        from virtual_self.auth.credentials import resolve_auth
        from virtual_self.config import get_settings
        from virtual_self.services.chat_memory_promote import run_chat_memory_extract

        ref = job.get("payload_ref") or {}
        settings = get_settings()
        if settings.bootstrap_token is None:
            raise RuntimeError("VSM_BOOTSTRAP_TOKEN required for chat.memory.extract")
        auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
        sid = ref.get("session_id")
        tid = ref.get("turn_id")
        if not sid or not tid:
            raise ValueError("chat.memory.extract: missing session_id or turn_id in payload_ref")
        run_chat_memory_extract(auth, UUID(sid), UUID(tid))
        return
    if jtype == "persona.reflect":
        from virtual_self.auth.credentials import resolve_auth
        from virtual_self.config import get_settings
        from virtual_self.services.mode_reflection import run_persona_reflect_job

        ref = job.get("payload_ref") or {}
        settings = get_settings()
        if settings.bootstrap_token is None:
            raise RuntimeError("VSM_BOOTSTRAP_TOKEN required for persona.reflect")
        auth = resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")
        run_persona_reflect_job(auth, ref)
        return
    log.warning("job_unknown_type", job_type=jtype)


def run_forever() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_pool()
    log.info("worker_started", owner=OWNER)
    _last_reflect_tick = 0.0
    while True:
        release_stale_leases()
        job = claim_next(OWNER)
        if job is None:
            if settings.persona_reflect_enabled and settings.bootstrap_token is not None:
                now = time.monotonic()
                if now - _last_reflect_tick >= max(60.0, settings.worker_poll_seconds * 10):
                    _last_reflect_tick = now
                    try:
                        from virtual_self.auth.credentials import resolve_auth
                        from virtual_self.services.mode_reflection import worker_tick_persona_reflect

                        auth = resolve_auth(
                            f"Bearer {settings.bootstrap_token.get_secret_value()}"
                        )
                        worker_tick_persona_reflect(auth)
                    except Exception as exc:
                        log.warning("persona_reflect_tick_failed", error=str(exc))
            time.sleep(settings.worker_poll_seconds)
            continue
        try:
            handle_job(job)
            complete_job(job["id"], True)
            JOB_RUNS.labels(job["job_type"], "success").inc()
        except Exception as exc:
            log.exception("job_failed", job_id=str(job["id"]), error=str(exc))
            complete_job(job["id"], False, str(exc))
            JOB_RUNS.labels(job["job_type"], "failure").inc()


if __name__ == "__main__":
    run_forever()
