from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.config import get_settings
from virtual_self.enrichment.constants import PROMPT_VERSION
from virtual_self.ingestion.my_life_scanner import ScannedPage, iter_my_life_pages
from virtual_self.observability.logging import get_logger
from virtual_self.observability.notify import NotifyCategory, NotifySeverity, notify_user
from virtual_self.policy.my_life_import import ImportDisposition
from virtual_self.providers.agent_chat import preflight_llm
from virtual_self.providers.ollama_runtime import ensure_chat_model
from virtual_self.services.my_life_import import _payload_for_page
from virtual_self.services.page_enrichment import (
    enrich_page_with_agent,
    enrichment_stats,
    fallback_enrichment_payload,
    get_stored_enrichment,
    upsert_enrichment,
)
from virtual_self.services.readiness import count_full_pages

log = get_logger(__name__)


def _uses_remote_openai(settings) -> bool:
    return settings.llm_backend == "openai" or (
        settings.llm_backend == "auto"
        and "openrouter.ai" in settings.openai_base_url
        and settings.openai_api_key is not None
    )


def _collect_pages_to_enrich(
    subject_id: UUID,
    root: Path,
    *,
    only_missing: bool,
    only_prefix: str | None,
) -> tuple[list[tuple[ScannedPage, str]], int, int]:
    todo: list[tuple[ScannedPage, str]] = []
    skipped = 0
    processed = 0
    for page in iter_my_life_pages(root):
        if only_prefix is not None and not page.rel_path.startswith(only_prefix):
            continue
        if page.decision.disposition != ImportDisposition.FULL:
            continue
        processed += 1
        if only_missing:
            stored = get_stored_enrichment(subject_id, page.rel_path, page.content_hash)
            if stored and stored.get("prompt_version") == PROMPT_VERSION:
                skipped += 1
                continue
        plain, _ = _payload_for_page(page)
        todo.append((page, plain))
    return todo, skipped, processed


def _enrich_one_page(subject_id: UUID, page: ScannedPage, plain: str, model: str) -> bool:
    for _attempt in range(2):
        payload = enrich_page_with_agent(page, plain)
        if payload is not None:
            upsert_enrichment(subject_id, page, payload, model=model)
            return True
    payload = fallback_enrichment_payload(page, plain)
    if payload is not None:
        upsert_enrichment(subject_id, page, payload, model="extractive_fallback")
        return True
    return False


def enrich_corpus(
    subject_id: UUID,
    root: Path,
    *,
    only_missing: bool = True,
    only_prefix: str | None = None,
    thaw_chat_model: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    use_openai = _uses_remote_openai(settings)
    if not use_openai:
        ensure_chat_model(settings.chat_model, thaw=thaw_chat_model)
    backend = preflight_llm()
    llm_label = settings.openai_model if use_openai else settings.chat_model
    workers = settings.enrich_parallel_workers if use_openai else 1
    log.info(
        "enrich_llm_preflight_ok",
        backend=backend,
        model=llm_label,
        parallel_workers=workers,
    )

    todo, skipped, processed = _collect_pages_to_enrich(
        subject_id, root, only_missing=only_missing, only_prefix=only_prefix
    )
    enriched = 0
    failed = 0
    base_stats = enrichment_stats(subject_id, root=root)
    base_enriched = int(base_stats.get("enriched_pages") or 0)
    total_full = count_full_pages(root)
    milestones_sent: set[int] = set()
    counter_lock = threading.Lock()
    record_model = settings.openai_model if use_openai else settings.chat_model

    def _on_success(page: ScannedPage) -> None:
        nonlocal enriched
        with counter_lock:
            enriched += 1
            n = enriched
            sk = skipped
            fl = failed
        if n == 1 or n % 25 == 0:
            log.info(
                "enrich_progress",
                enriched=n,
                skipped=sk,
                failed=fl,
                rel_path=page.rel_path,
            )
        done = base_enriched + n
        if total_full >= 10 and 0 < done <= total_full:
            with counter_lock:
                for mark in (25, 50, 75, 100):
                    if done * 100 // total_full >= mark and mark not in milestones_sent:
                        milestones_sent.add(mark)
                        notify_user(
                            f"Virtual Self enrichment is about {mark}% complete "
                            f"({done} of {total_full} pages enriched so far).",
                            category=NotifyCategory.ENRICHMENT_MILESTONE,
                            severity=NotifySeverity.INFO,
                        )

    if workers <= 1:
        for page, plain in todo:
            if _enrich_one_page(subject_id, page, plain, record_model):
                _on_success(page)
            else:
                failed += 1
                if failed <= 3 or failed % 25 == 0:
                    log.warning("enrich_failed", rel_path=page.rel_path, failed=failed)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_enrich_one_page, subject_id, page, plain, record_model): page
                for page, plain in todo
            }
            for fut in as_completed(futures):
                page = futures[fut]
                try:
                    ok = fut.result()
                except Exception as exc:
                    ok = False
                    log.warning("enrich_worker_error", rel_path=page.rel_path, error=str(exc)[:200])
                if ok:
                    _on_success(page)
                else:
                    with counter_lock:
                        failed += 1
                        fl = failed
                    if fl <= 3 or fl % 25 == 0:
                        log.warning("enrich_failed", rel_path=page.rel_path, failed=fl)

    stats = enrichment_stats(subject_id, root=root)
    if enriched == 0 and failed > 0 and stats.get("enriched_pages", 0) < 10:
        log.error(
            "enrich_corpus_no_progress",
            processed=processed,
            failed=failed,
            model=record_model,
        )
    return {
        "enriched": enriched,
        "skipped_existing": skipped,
        "failed": failed,
        "full_pages_seen": processed,
        "parallel_workers": workers,
        **stats,
    }
