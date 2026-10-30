from __future__ import annotations

import json

import pytest

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.policy.authored_tags import classify_authored_text
from virtual_self.persistence.db import connection, transaction
from virtual_self.services.authored_register_sensitivity_pass import (
    run_authored_register_sensitivity_pass,
)
from uuid6 import uuid7


def test_classify_chat_and_gmail_registers():
    chat = classify_authored_text(
        source_kind="chat_user_turn",
        source_ref={"turn_id": "t1"},
        content="hey what is up",
        author="self",
    )
    assert chat.register == "casual_chat"
    assert chat.sensitivity == 1

    mail = classify_authored_text(
        source_kind="gmail_sent",
        source_ref={"mbox": "x.mbox"},
        content="Please find the report attached.",
        author="self",
    )
    assert mail.register == "work_formal"


def test_mental_health_journal_sensitivity_at_least_three():
    tag = classify_authored_text(
        source_kind="notion",
        source_ref={"rel_path": "Mental Health [mh]/Session [s].md"},
        content="Therapy today was hard but useful.",
        author="self",
    )
    assert tag.register == "journal"
    assert tag.sensitivity >= 3


def test_finance_content_excluded():
    tag = classify_authored_text(
        source_kind="notion",
        source_ref={"rel_path": "Journal [j]/Note [n].md"},
        content="Paid invoice via stripe for the subscription.",
        author="self",
    )
    assert tag.excluded_reason == "finance_excluded"
    assert tag.sensitivity >= 3


def test_otp_flags_high_sensitivity():
    tag = classify_authored_text(
        source_kind="gmail_sent",
        source_ref={},
        content="Your verification code is 482916 do not share.",
        author="self",
    )
    assert tag.sensitivity >= 3


def test_people_path_social_register():
    tag = classify_authored_text(
        source_kind="notion",
        source_ref={"rel_path": "People [p]/Alice [a].md"},
        content="We talked about boundaries and what I want from friendship.",
        author="self",
    )
    assert tag.register == "social"


def test_pass_updates_rows(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with transaction() as conn:
        conn.execute("DELETE FROM authored_texts WHERE subject_id = %s", (auth.subject_id,))
        conn.execute(
            """
            INSERT INTO authored_texts (
              id, subject_id, source_kind, source_ref, author, register,
              content, word_count, text_hash, sensitivity
            ) VALUES (%s, %s, 'notion', %s::jsonb, 'self', 'journal',
              %s, %s, %s, 1)
            """,
            (
                uuid7(),
                auth.subject_id,
                json.dumps({"rel_path": "People [p]/Bob [b].md"}),
                "Long enough prose about a friend and how we relate to each other today.",
                12,
                uuid7().hex,
            ),
        )

    dry = run_authored_register_sensitivity_pass(auth, dry_run=True)
    assert dry["register_changes"] >= 1
    assert dry["self_by_register_rows"].get("social") == 1

    applied = run_authored_register_sensitivity_pass(auth, dry_run=False)
    assert applied["rows_updated"] >= 1

    with connection() as conn:
        row = conn.execute(
            "SELECT register, sensitivity FROM authored_texts WHERE subject_id = %s",
            (auth.subject_id,),
        ).fetchone()
    assert row["register"] == "social"
