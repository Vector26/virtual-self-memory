from __future__ import annotations

from pathlib import Path

import pytest

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.eval.notion_self_text import split_body_authorship, word_count
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.notion_authored_split import (
    detect_bulk_import_timestamps,
    run_notion_authored_split,
    split_page,
)
from virtual_self.ingestion.my_life_scanner import iter_my_life_pages

_LLM_BODY = """# demo

<details><summary>### Initial Prompt</summary>

I had a five year relationship that ended and I have been rebuilding habits since then.

</details>
<details><summary>### DS</summary>

Your situation reveals a profound existential tapestry.

</details>
"""

_QUOTE_BODY = """# notes

> This is a long blockquote excerpt that should be classified as quoted material
> because it spans multiple lines and exceeds the minimum word threshold easily.

My own reflection on the week and what I learned from therapy today.
"""


def test_split_body_llm_transcript():
    segments = split_body_authorship(_LLM_BODY)
    authors = [a for a, _, _ in segments]
    assert authors.count("self") == 1
    assert authors.count("llm") == 1
    self_text = next(c for a, c, _ in segments if a == "self")
    llm_text = next(c for a, c, _ in segments if a == "llm")
    assert "five year relationship" in self_text
    assert "profound existential" in llm_text
    assert "### DS" not in self_text


def test_split_body_blockquote_and_self():
    segments = split_body_authorship(_QUOTE_BODY)
    authors = {a for a, _, _ in segments}
    assert "quoted" in authors
    assert "self" in authors
    self_text = next(c for a, c, _ in segments if a == "self")
    assert "reflection on the week" in self_text


def test_run_splitter_on_mini_fixture(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")
    root = Path(__file__).resolve().parent / "fixtures" / "my_life_mini"

    first = run_notion_authored_split(auth, root, dry_run=True)
    assert first["pages_total"] >= 2
    assert first["pages_reconciled"] == first["pages_total"]
    assert first.get("reconcile_error") is None

    applied = run_notion_authored_split(auth, root, dry_run=False)
    assert applied["rows_inserted"] + applied["rows_skipped_duplicate"] >= 1

    again = run_notion_authored_split(auth, root, dry_run=False)
    assert again["rows_inserted"] == 0
    assert again["rows_skipped_duplicate"] >= applied["rows_inserted"] + applied["rows_skipped_duplicate"]


def test_three_personality_page_when_export_present():
    root = get_settings().my_life_root
    if not root.is_dir():
        pytest.skip("private corpus export not available")
    rel = "Journal [20f05923]/the 3 personality problem [20505c5b].md"
    path = root / rel
    if not path.is_file():
        pytest.skip("known transcript page missing")
    page = next(
        p
        for p in iter_my_life_pages(root)
        if "the 3 personality problem" in p.rel_path.lower()
    )
    segments = split_page(page)
    assert any(a == "self" for a, _, _ in segments)
    assert any(a == "llm" for a, _, _ in segments)
    self_blob = " ".join(c for a, c, _ in segments if a == "self")
    assert "five year" in self_blob.lower() or "5 year" in self_blob.lower()
    assert "profound existential" not in self_blob


def test_detect_bulk_import_timestamps():
    pages = list(
        iter_my_life_pages(
            Path(__file__).resolve().parent / "fixtures" / "my_life_mini",
        )
    )
    bulk = detect_bulk_import_timestamps(pages)
    assert isinstance(bulk, set)
