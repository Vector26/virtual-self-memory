from __future__ import annotations

import json
from pathlib import Path

from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.eval.heldout_snapshot import (
    split_key_chat_turn,
    split_key_notion_source,
    stable_split_bucket_from_key,
)
from virtual_self.persistence.db import connection, transaction
from virtual_self.services.authorship_chain_audit import (
    reconcile_authored_splits,
    run_authorship_chain_audit,
)


def test_notion_split_key_uses_notion_prefix():
    path = "/tmp/My Life [x]/Journal [j]/Note [n].md"
    assert split_key_notion_source(path) == f"notion:{path}"
    assert split_key_notion_source(path) != path
    assert stable_split_bucket_from_key(split_key_notion_source(path)) in ("train", "heldout")


def test_chat_split_key_matches_heldout_candidate_ref():
    turn_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    key = split_key_chat_turn(turn_id)
    assert key == f"chat_user_turn:chat_turns:{turn_id}"


def test_audit_flags_manifest_train_leak(ensure_db, tmp_path):
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    heldout_root = tmp_path / "heldout"
    heldout_root.mkdir()
    notion_path = "/corpus/Journal [j]/Audit page [abc].md"
    (heldout_root / "heldout_001.txt").write_text(
        "This is a long enough held out evaluation paragraph for testing split leaks.",
        encoding="utf-8",
    )
    manifest = {
        "version": 1,
        "entry_count": 1,
        "entries": [
            {
                "id": "heldout_001",
                "filename": "heldout_001.txt",
                "source_path": notion_path,
                "source_kind": "notion",
                "register": "journal",
                "word_count": 12,
            }
        ],
    }
    (heldout_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    content = "This is a long enough held out evaluation paragraph for testing split leaks."
    with transaction() as conn:
        conn.execute("DELETE FROM authored_texts WHERE subject_id = %s", (auth.subject_id,))
        conn.execute(
            """
            INSERT INTO authored_texts (
              id, subject_id, source_kind, source_ref, author, register,
              content, word_count, text_hash, sensitivity, split
            ) VALUES (%s, %s, 'notion', %s::jsonb, 'self', 'journal',
              %s, %s, %s, 1, 'train')
            """,
            (
                uuid7(),
                auth.subject_id,
                json.dumps({"rel_path": "Journal [j]/Audit page [abc].md"}),
                content,
                len(content.split()),
                uuid7().hex,
            ),
        )

    report = run_authorship_chain_audit(
        auth,
        heldout_root=heldout_root,
        my_life_root=Path("/corpus"),
        report_path=tmp_path / "audit.json",
    )
    assert report["heldout_consistency"]["manifest_source_train_split_leak_count"] >= 1
    assert report["heldout_consistency"]["eval_content_train_split_leak_count"] >= 1

    recon = reconcile_authored_splits(
        auth,
        my_life_root=Path("/corpus"),
        heldout_root=heldout_root,
        dry_run=False,
    )
    assert recon["rows_to_update"] >= 1

    with connection() as conn:
        split = conn.execute(
            "SELECT split FROM authored_texts WHERE subject_id = %s",
            (auth.subject_id,),
        ).fetchone()["split"]
    assert split == "heldout"
