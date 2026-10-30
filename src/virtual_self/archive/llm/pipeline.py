from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from virtual_self.archive.llm.prompts import (
    L1_SYSTEM,
    L2_SYSTEM,
    L3_SYSTEM,
    L4_SYSTEM,
    L5_SYSTEM,
    PROMPT_VERSION,
)
from virtual_self.archive.olap import fetch_events_by_ids, sql_stats
from virtual_self.config import get_settings
from virtual_self.enrichment.grounding import adjust_confidence, grounding_score
from virtual_self.observability.logging import get_logger
from virtual_self.persistence.db import transaction
from virtual_self.providers.agent_chat import chat_json
from virtual_self.archive import ledger

log = get_logger(__name__)


def verify_candidate(
    candidate: dict[str, Any],
    conn,
    archive_run_id: UUID | None = None,
) -> dict[str, Any]:
    """L4 verifier + grounding; returns candidate with verdict fields."""
    if candidate.get("abstain"):
        return {**candidate, "verdict": "unsupported", "verdict_reason": "abstain"}
    ids = candidate.get("evidence_event_ids") or []
    events = fetch_events_by_ids([str(x) for x in ids])
    corpus = "\n".join(
        f"{e.get('event_id')}: {e.get('title') or ''} {e.get('text') or ''}" for e in events
    )
    user = json.dumps({"candidate": candidate, "events": events}, ensure_ascii=False)[:12000]
    out = chat_json(L4_SYSTEM, user, temperature=0.0) if get_settings().archive_llm_enabled else None
    verdict = "supported"
    reason = "llm_disabled_default_supported"
    if out:
        verdict = str(out.get("verdict") or "unsupported")
        reason = str(out.get("reason") or "")
        if archive_run_id:
            with transaction() as c:
                ledger.record_llm_run(
                    c,
                    archive_run_id=archive_run_id,
                    layer="L4",
                    prompt_version=PROMPT_VERSION,
                    input_ref={"candidate": candidate.get("content", "")[:200]},
                    output_json=out,
                    verdict=verdict,
                )
    score = grounding_score(candidate.get("content", ""), corpus)
    conf = adjust_confidence(candidate.get("content", ""), float(candidate.get("confidence", 0.5)), score)
    if verdict == "unsupported":
        conf = 0.0
    elif verdict == "partially":
        conf = min(conf, 0.65)
    return {
        **candidate,
        "verdict": verdict,
        "verdict_reason": reason,
        "grounding_score": score,
        "confidence": conf,
    }


def run_l1_sample(file_sample: str, archive_run_id: UUID | None = None) -> dict[str, Any] | None:
    if not get_settings().archive_llm_enabled:
        return {"status": "unsupported"}
    out = chat_json(L1_SYSTEM, file_sample[:8000], temperature=0.0)
    if archive_run_id and out:
        with transaction() as c:
            ledger.record_llm_run(
                c,
                archive_run_id=archive_run_id,
                layer="L1",
                prompt_version=PROMPT_VERSION,
                input_ref={"bytes": len(file_sample)},
                output_json=out,
            )
    return out


def extract_candidates_for_product(
    product: str,
    digest: str,
    cites: list[list[str]],
    archive_run_id: UUID | None = None,
) -> list[dict[str, Any]]:
    stats = sql_stats(product)
    user = json.dumps(
        {"product": product, "sql_stats": stats, "digest": digest, "cites": cites},
        ensure_ascii=False,
    )[:14000]
    if not get_settings().archive_llm_enabled:
        return []
    out = chat_json(L3_SYSTEM, user, temperature=0.2)
    if archive_run_id and out:
        with transaction() as c:
            ledger.record_llm_run(
                c,
                archive_run_id=archive_run_id,
                layer="L3",
                prompt_version=PROMPT_VERSION,
                input_ref={"product": product},
                output_json=out,
            )
    if not out:
        return []
    cands = out.get("candidates") or []
    verified: list[dict[str, Any]] = []
    for cand in cands:
        if not isinstance(cand, dict):
            continue
        verified.append(verify_candidate(cand, None, archive_run_id))
    return verified


def window_digest(events: list[dict[str, Any]], archive_run_id: UUID | None = None) -> tuple[str, list[list[str]]]:
    if not get_settings().archive_llm_enabled or not events:
        return "", []
    sample = events[:80]
    user = json.dumps({"events": sample}, ensure_ascii=False)[:14000]
    out = chat_json(L2_SYSTEM, user, temperature=0.1)
    if archive_run_id and out:
        with transaction() as c:
            ledger.record_llm_run(
                c,
                archive_run_id=archive_run_id,
                layer="L2",
                prompt_version=PROMPT_VERSION,
                input_ref={"n_events": len(sample)},
                output_json=out,
            )
    if not out:
        return "", []
    return str(out.get("digest") or ""), out.get("cites") or []


def consolidate_actions(
    verified: list[dict[str, Any]],
    existing_snippets: list[str],
    archive_run_id: UUID | None = None,
) -> list[dict[str, Any]]:
    if not get_settings().archive_llm_enabled:
        return [{"candidate_index": i, "action": "promote"} for i in range(len(verified))]
    user = json.dumps(
        {"candidates": verified, "existing": existing_snippets[:20]},
        ensure_ascii=False,
    )[:12000]
    out = chat_json(L5_SYSTEM, user, temperature=0.0)
    if archive_run_id and out:
        with transaction() as c:
            ledger.record_llm_run(
                c,
                archive_run_id=archive_run_id,
                layer="L5",
                prompt_version=PROMPT_VERSION,
                input_ref={"n": len(verified)},
                output_json=out,
            )
    if not out:
        return [{"candidate_index": i, "action": "promote"} for i in range(len(verified))]
    return out.get("actions") or []
