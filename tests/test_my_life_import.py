from __future__ import annotations

from pathlib import Path

from virtual_self.policy.my_life_import import ImportDisposition, classify_page
from virtual_self.services.my_life_import import plan_import, run_import

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "my_life_mini"


def test_classify_finance_skipped():
    d = classify_page(
        "Finance Tracker [f]/Secret [x].md",
        export_mode="title_only",
        body="_Stub only — content intentionally not exported",
        properties={},
    )
    assert d.disposition == ImportDisposition.SKIP


def test_classify_quest_full():
    d = classify_page(
        "Quests [q]/Goal [x].md",
        export_mode=None,
        body="real content here",
        properties={"Status": "Done"},
    )
    assert d.disposition == ImportDisposition.FULL
    assert d.memory_kind == "goal"


def test_plan_import_fixture():
    out = plan_import(FIXTURE_ROOT)
    assert out["stats"]["scanned"] >= 2
    assert out["stats"]["skipped"] >= 1
    assert out["stats"]["full"] >= 1


def test_apply_import_fixture(client, auth_headers, ensure_db):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.persistence.db import execute, transaction
    from virtual_self.services.page_enrichment import load_enrichment_jsonl

    auth = resolve_auth(auth_headers["Authorization"])
    fixture_root = str(FIXTURE_ROOT)
    load_enrichment_jsonl(
        Path(__file__).resolve().parent / "fixtures" / "my_life_mini_enrichments.jsonl",
        auth.subject_id,
    )

    plan = client.post(
        "/v1/imports/my-life/plan",
        json={"root": str(FIXTURE_ROOT)},
        headers=auth_headers,
    )
    assert plan.status_code == 200

    applied = client.post(
        "/v1/imports/my-life",
        json={"dry_run": False, "root": str(FIXTURE_ROOT), "promote_memories": True},
        headers=auth_headers,
    )
    assert applied.status_code == 200
    stats = applied.json()["stats"]
    assert stats["errors"] == []
    assert stats["full"] >= 1
    assert stats["sources_created"] + stats["sources_updated"] + stats["sources_unchanged"] >= 1

    search = client.post(
        "/v1/memories/search",
        json={"query": "home lab", "limit": 10},
        headers=auth_headers,
    )
    assert search.status_code == 200
    assert len(search.json()["items"]) >= 1

    again = run_import(
        __import__("virtual_self.auth.credentials", fromlist=["resolve_auth"]).resolve_auth(
            auth_headers["Authorization"]
        ),
        FIXTURE_ROOT,
        dry_run=False,
        promote_memories=True,
    )
    assert again["stats"]["sources_unchanged"] >= 1

    with transaction() as conn:
        execute(
            conn,
            "DELETE FROM page_enrichments WHERE subject_id = %s AND rel_path LIKE %s",
            (auth.subject_id, "Quests [q]/%"),
        )


def test_real_my_life_plan_dry_run_only():
    real = Path(
        "/data/corpus"
    )
    if not real.is_dir():
        return
    out = plan_import(real)
    assert out["stats"]["scanned"] > 800
    assert out["stats"]["skipped"] > 0
    assert out["stats"]["full"] > 0
    assert out["stats"]["metadata_only"] > 0
