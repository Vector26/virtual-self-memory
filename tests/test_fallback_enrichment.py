from virtual_self.ingestion.my_life_scanner import ScannedPage
from virtual_self.policy.my_life_import import ImportDecision, ImportDisposition
from virtual_self.services.page_enrichment import fallback_enrichment_payload


def test_fallback_enrichment_grounds_on_thin_stub() -> None:
    page = ScannedPage(
        rel_path="Career [x]/MS Abroad [y]/Essay 1 [z].md",
        abs_path=None,
        notion_id=None,
        title="Essay 1",
        decision=ImportDecision(ImportDisposition.FULL, "test"),
        content_hash="abc",
        body="",
        meta={},
        properties={},
    )
    plain = "Personal statement draft notes for Oxford application."
    payload = fallback_enrichment_payload(page, plain)
    assert payload is not None
    assert len(payload["summary"]) >= 20
    assert payload["memory_kind"] in {
        "goal",
        "skill",
        "preference",
        "episode",
        "routine",
        "identity",
        "current_state",
    }
