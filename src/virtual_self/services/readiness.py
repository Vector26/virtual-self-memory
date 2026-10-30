from __future__ import annotations

from pathlib import Path
from uuid import UUID

from virtual_self.ingestion.my_life_scanner import iter_my_life_pages
from virtual_self.policy.my_life_import import ImportDisposition
from virtual_self.persistence.db import fetch_one, transaction
from virtual_self.services.indexing import index_stats
from virtual_self.services.page_enrichment import enrichment_stats

READY_MARKER = Path(__file__).resolve().parents[3] / ".state" / "start_ready"


def count_full_pages(root: Path) -> int:
    return sum(1 for p in iter_my_life_pages(root) if p.decision.disposition == ImportDisposition.FULL)


def is_start_ready(subject_id: UUID, root: Path, *, min_coverage: float = 1.0) -> dict:
    enrich = enrichment_stats(subject_id, root=root)
    full = int(enrich.get("full_pages") or 0) or count_full_pages(root)
    idx = index_stats(subject_id)
    embedded = int(enrich.get("enriched_pages") or 0)
    coverage = (embedded / full) if full else 0.0
    with transaction() as conn:
        personality_ok = (
            fetch_one(
                conn,
                """
                SELECT 1 FROM personality_profiles
                WHERE subject_id = %s AND status = 'active'
                LIMIT 1
                """,
                (subject_id,),
            )
            is not None
        )
    marker_ok = READY_MARKER.is_file()
    coverage_ok = coverage >= min_coverage
    ready = full > 0 and coverage_ok and idx.get("fully_indexed") and personality_ok
    return {
        "ready": ready,
        "full_pages": full,
        "enriched_pages": embedded,
        "enrichment_coverage": round(coverage, 3),
        "fully_indexed": idx.get("fully_indexed"),
        "marker": str(READY_MARKER),
    }


def mark_start_ready() -> None:
    READY_MARKER.parent.mkdir(parents=True, exist_ok=True)
    READY_MARKER.write_text("ok\n", encoding="utf-8")
