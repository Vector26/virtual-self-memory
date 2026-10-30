from __future__ import annotations

from pathlib import Path
from typing import Any

from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.observability.logging import get_logger
from virtual_self.observability.notify import NotifyCategory, notify_user

log = get_logger(__name__)
from virtual_self.providers.ollama_runtime import ensure_chat_model
from virtual_self.services.enrichment_batch import enrich_corpus
from virtual_self.services.indexing import index_stats, reindex_subject
from virtual_self.services.memory_refresh import refresh_memories_from_enrichments
from virtual_self.services.my_life_import import run_import
from virtual_self.services.personality import bootstrap_personality, resynthesize_personality
from virtual_self.services.readiness import is_start_ready, mark_start_ready


def run_bake(auth: AuthContext, root: Path) -> dict[str, Any]:
    settings = get_settings()
    try:
        ensure_chat_model(settings.chat_model, thaw=True)
    except Exception as exc:
        notify_user(
            "Virtual Self could not load the chat model for enrichment. "
            "Plug in the Expansion drive and run ollama-cold thaw qwen3.6:35b-a3b, then run virtual-self start again.",
            category=NotifyCategory.BAKE,
        )
        raise exc

    enrich = enrich_corpus(auth.subject_id, root, only_missing=True, thaw_chat_model=False)
    import_out = run_import(
        auth, root, dry_run=False, promote_memories=True, enrich_on_import=False
    )
    refresh = refresh_memories_from_enrichments(auth, root)
    try:
        resynthesize_personality(auth)
    except Exception:
        bootstrap_personality(auth)
    index_run = reindex_subject(auth.subject_id, only_missing=False)
    status = index_stats(auth.subject_id)
    ready = is_start_ready(auth.subject_id, root)

    full = int(ready.get("full_pages") or 0)
    done = int(ready.get("enriched_pages") or 0)
    pct = round(float(ready.get("enrichment_coverage") or 0) * 100, 1)

    if ready["ready"]:
        mark_start_ready()
        msg = (
            f"Virtual Self build completed successfully. {done} of {full} pages enriched ({pct}%). "
            f"The memory is ready to chat. On the server, run virtual-self start."
        )
    else:
        msg = (
            f"Virtual Self build finished but is not fully ready yet. "
            f"{done} of {full} pages enriched ({pct}%). "
            f"Another bake pass may be needed before virtual-self start will open chat."
        )
    if not notify_user(msg, category=NotifyCategory.BAKE):
        log.error("bake_notify_failed", readiness=ready)
        raise RuntimeError("Completion notification could not be delivered — check .state/notify_audit.jsonl")

    return {
        "enrichment": enrich,
        "import": import_out,
        "memory_refresh": refresh,
        "index": index_run,
        "index_status": status,
        "readiness": ready,
    }
