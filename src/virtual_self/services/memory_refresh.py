from __future__ import annotations

from pathlib import Path
from typing import Any

from virtual_self.auth.credentials import AuthContext
from virtual_self.ingestion.my_life_scanner import iter_my_life_pages
from virtual_self.policy.my_life_import import ImportDisposition
from virtual_self.persistence.db import fetch_one, transaction
from virtual_self.services.my_life_import import _maybe_promote_memory, _payload_for_page


def refresh_memories_from_enrichments(auth: AuthContext, root: Path) -> dict[str, Any]:
    """Re-promote memories for pages that already have cursor_agent enrichments."""
    updated = 0
    pending = 0
    created = 0
    for page in iter_my_life_pages(root):
        if page.decision.disposition != ImportDisposition.FULL:
            continue
        from virtual_self.services.page_enrichment import get_stored_enrichment

        if not get_stored_enrichment(auth.subject_id, page.rel_path, page.content_hash):
            pending += 1
            continue
        plain, _ = _payload_for_page(page)
        with transaction() as conn:
            row = fetch_one(
                conn,
                """
                SELECT er.id AS evidence_id
                FROM sources s
                JOIN evidence_records er ON er.source_id = s.id
                WHERE s.subject_id = %s AND s.source_path = %s
                ORDER BY er.created_at DESC
                LIMIT 1
                """,
                (auth.subject_id, page.rel_path),
            )
            if row is None:
                pending += 1
                continue
            mem = _maybe_promote_memory(conn, auth, page, row["evidence_id"], plain)
            if mem == 2:
                updated += 1
            elif mem == 1:
                created += 1
            elif mem == -1:
                pending += 1
    return {
        "memories_refreshed": updated,
        "memories_created": created,
        "still_pending_enrichment": pending,
    }
