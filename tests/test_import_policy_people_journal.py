from __future__ import annotations

from virtual_self.policy.my_life_import import ImportDisposition, classify_page

_STUB = "_Stub only — content intentionally not exported"
_SUBSTANTIVE = "x" * 80


def test_people_substantive_body_full_with_relationship_kind():
    d = classify_page(
        "People [p]/Alice [a].md",
        export_mode=None,
        body=f"# Alice\n\n{_SUBSTANTIVE}",
        properties={},
    )
    assert d.disposition == ImportDisposition.FULL
    assert d.memory_kind == "relationship"
    assert d.reason == "people_substantive"


def test_people_thin_body_metadata_only():
    d = classify_page(
        "People [p]/Bob [b].md",
        export_mode=None,
        body="short note",
        properties={},
    )
    assert d.disposition == ImportDisposition.METADATA_ONLY
    assert d.reason == "people_thin"


def test_journal_substantive_body_full_with_episode_kind():
    d = classify_page(
        "Journal [j]/Entry [e].md",
        export_mode=None,
        body=_SUBSTANTIVE,
        properties={},
    )
    assert d.disposition == ImportDisposition.FULL
    assert d.memory_kind == "episode"
    assert d.reason == "journal_substantive"


def test_journal_thin_body_metadata_only():
    d = classify_page(
        "Journal [j]/Quick [q].md",
        export_mode=None,
        body="one line",
        properties={},
    )
    assert d.disposition == ImportDisposition.METADATA_ONLY
    assert d.reason == "journal_thin"


def test_finance_still_skipped():
    d = classify_page(
        "Finance Tracker [f]/Secret [x].md",
        export_mode=None,
        body=_SUBSTANTIVE,
        properties={},
    )
    assert d.disposition == ImportDisposition.SKIP
    assert d.reason == "finance_excluded"


def test_heal_repair_still_skipped():
    d = classify_page(
        "Quests [q]/Main-Quests [m]/Heal_Repair [h]/Note [n].md",
        export_mode=None,
        body=_SUBSTANTIVE,
        properties={},
    )
    assert d.disposition == ImportDisposition.SKIP
    assert d.reason == "sensitive_area_excluded"


def test_triggers_still_skipped():
    d = classify_page(
        "Mental Health [mh]/Triggers [t]/Item [i].md",
        export_mode=None,
        body=_SUBSTANTIVE,
        properties={},
    )
    assert d.disposition == ImportDisposition.SKIP
    assert d.reason == "sensitive_area_excluded"


def test_stub_people_stays_metadata_only_even_if_long():
    d = classify_page(
        "People [p]/Alice [a].md",
        export_mode=None,
        body=f"{_STUB}\n{_SUBSTANTIVE}",
        properties={},
    )
    assert d.disposition == ImportDisposition.METADATA_ONLY
    assert d.reason == "title_only_stub"


def test_dev_pratap_notion_page_classifies_full():
    from pathlib import Path

    root = Path(
        "/data/corpus"
    )
    page = root / "People [a51decc3]/People [50016a98]/Jordan Example [22e0a991]/index.md"
    if not page.is_file():
        import pytest

        pytest.skip("Notion export not present on this machine")
    body = page.read_text(encoding="utf-8")
    rel = str(page.relative_to(root))
    d = classify_page(rel, export_mode=None, body=body, properties={})
    assert d.disposition == ImportDisposition.FULL
    assert d.memory_kind == "relationship"
    assert d.reason == "people_substantive"
