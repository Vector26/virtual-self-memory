from __future__ import annotations

from pathlib import Path

import pytest

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.takeout_gmail_authored_ingest import (
    parse_sent_authored_candidates,
    run_takeout_gmail_authored_ingest,
    validate_no_reply_leak,
)

_SENT_MBOX = """From sender@xxx Mon Jan  1 12:00:00 2024
X-Gmail-Labels: Sent
From: me@example.com
To: friend@example.com
Subject: Project update
Date: Mon, 1 Jan 2024 12:00:00 +0000
Content-Type: text/plain; charset=utf-8

I'll ship the API changes tomorrow morning.

On Mon, Dec 30, 2023 friend wrote:
> when is the API ready?

--
me@example.com

From sender@xxx Mon Jan  2 12:00:00 2024
X-Gmail-Labels: Inbox
From: promo@shop.example
To: me@example.com
Subject: Sale
Date: Tue, 2 Jan 2024 12:00:00 +0000
Content-Type: text/plain; charset=utf-8

Buy now.

From sender@xxx Mon Jan  3 12:00:00 2024
X-Gmail-Labels: Sent
From: no-reply@example.com
Subject: Automatic reply
Date: Wed, 3 Jan 2024 12:00:00 +0000
Content-Type: text/plain; charset=utf-8

Your account was accessed.

"""


@pytest.fixture
def sent_fixture_mbox(tmp_path: Path) -> Path:
    p = tmp_path / "sample.mbox"
    p.write_text(_SENT_MBOX, encoding="utf-8")
    return p


def test_parse_sent_only_and_strip_replies(sent_fixture_mbox: Path):
    pending, stats = parse_sent_authored_candidates(
        sent_fixture_mbox,
        self_emails={"me@example.com"},
    )
    assert stats.mail_messages_total == 3
    assert stats.mail_sent_seen == 1
    assert stats.mail_skipped_not_sent == 1
    assert stats.mail_skipped_auto == 1  # Sent-labeled noreply / automatic reply
    assert len(pending) == 1
    body = pending[0].fields[6]
    assert "API changes tomorrow" in body
    assert validate_no_reply_leak(body)


def test_ingest_idempotent(sent_fixture_mbox: Path, ensure_db, monkeypatch: pytest.MonkeyPatch):
    apply_migrations()
    monkeypatch.setenv("VSM_SELF_EMAILS", "me@example.com")
    get_settings.cache_clear()
    with connection() as conn:
        conn.execute("DELETE FROM authored_texts WHERE source_kind = 'gmail_sent'")
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")

    first = run_takeout_gmail_authored_ingest(auth, mbox_path=sent_fixture_mbox, dry_run=False)
    assert first["products"]["keep"]["status"] == "absent"
    assert first["products"]["meta"]["status"] == "absent"
    assert first["products"]["mail"]["rows_inserted"] == 1

    again = run_takeout_gmail_authored_ingest(auth, mbox_path=sent_fixture_mbox, dry_run=False)
    assert again["products"]["mail"]["rows_inserted"] == 0
    assert again["products"]["mail"]["rows_skipped_duplicate"] >= 1
