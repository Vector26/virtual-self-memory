from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from virtual_self.enrichment.constants import CURSOR_AGENT_PRODUCER, PROMPT_VERSION
from virtual_self.enrichment.prompts import PERSONALITY_SYNTHESIS_SYSTEM
from virtual_self.persistence.db import execute, fetch_all, fetch_one, transaction
from virtual_self.providers.agent_chat import chat_json
from uuid6 import uuid7

def synthesize_facets(subject_id: UUID, profile_name: str = "Default") -> dict[str, str] | None:
    with transaction() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT pe.summary, pe.memory_kind, pe.labels
            FROM page_enrichments pe
            WHERE pe.subject_id = %s AND pe.producer = %s
            ORDER BY pe.created_at DESC
            LIMIT 200
            """,
            (subject_id, CURSOR_AGENT_PRODUCER),
        )
    if not rows:
        return None
    blob = json.dumps(
        [{"summary": r["summary"], "kind": r["memory_kind"], "labels": r["labels"]} for r in rows],
        default=str,
    )[:80000]
    user = (
        f"Profile display name: {profile_name}\n"
        f"Enrichment count: {len(rows)}\n\n"
        f"Memory enrichments (newest first):\n{blob}"
    )
    raw = chat_json(PERSONALITY_SYNTHESIS_SYSTEM, user, temperature=0.3)
    if not raw:
        return None
    keys = (
        "constitution",
        "values",
        "voice",
        "interaction_preferences",
        "current_priorities",
        "decision_style",
    )
    out: dict[str, str] = {}
    for k in keys:
        text = str(raw.get(k) or "").strip()
        if len(text) < 40:
            return None
        out[k] = text[:8000]
    return out


def record_synthesis_run(profile_id: UUID, facets: dict[str, str], model: str | None) -> None:
    with transaction() as conn:
        execute(
            conn,
            """
            INSERT INTO personality_synthesis_runs (
              id, profile_id, producer, model, prompt_version, facet_snapshot
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                uuid7(),
                profile_id,
                CURSOR_AGENT_PRODUCER,
                model,
                PROMPT_VERSION,
                json.dumps(facets),
            ),
        )
