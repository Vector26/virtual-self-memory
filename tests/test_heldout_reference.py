from __future__ import annotations

import json
from pathlib import Path

from virtual_self.eval.heldout_snapshot import (
    HELDOUT_MIN_ENTRIES,
    HELDOUT_MIN_WORDS,
    heldout_dir,
    validate_heldout_dir,
)
from virtual_self.eval.notion_self_text import extract_self_text_from_markdown, word_count

_SAMPLE_LLM_PAGE = """---
title: "demo"
---

# demo

<details><summary>### Initial Prompt</summary>

so basically i want you to answer this :
I had a five year relationship that ended and I have been rebuilding habits since then.
I sometimes feel empty and I am trying therapy gym and reading philosophers.
can you point out what is/are the issues?

</details>
<details><summary>### DS</summary>

Your situation reveals a profound existential tapestry.

</details>
"""


def test_extract_initial_prompt_only():
    text = extract_self_text_from_markdown(_SAMPLE_LLM_PAGE)
    assert text is not None
    assert "### DS" not in text
    assert "profound existential" not in text
    assert "five year relationship" in text


def test_heldout_snapshot_on_disk():
    root = heldout_dir()
    assert (root / "manifest.json").is_file(), "run: virtual-self snapshot-heldout"
    errors = validate_heldout_dir(root)
    assert errors == []

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["entry_count"] >= HELDOUT_MIN_ENTRIES
    assert len(manifest["registers"]) >= 3
    for entry in manifest["entries"]:
        body = (root / entry["filename"]).read_text(encoding="utf-8")
        assert word_count(body) >= HELDOUT_MIN_WORDS
        assert entry["word_count"] == word_count(body)
