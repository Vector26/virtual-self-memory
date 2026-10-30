from __future__ import annotations

import os
from unittest.mock import patch

from virtual_self.observability.notify import (
    NotifyCategory,
    NotifySeverity,
    notify_user,
)


def test_notify_user_adds_period():
    with patch.dict(os.environ, {"VSM_NOTIFY_ALLOW_IN_TESTS": "true", "VSM_NOTIFY_ENABLED": "true"}):
        with patch("urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.status = 200
            with patch("os.path.isfile", return_value=False):
                with patch("subprocess.run", return_value=type("R", (), {"returncode": 0})()):
                    ok = notify_user("Build finished", force=True)
    assert ok
    payload = op.call_args[0][0].data.decode()
    assert "Build finished." in payload
    assert "Virtual Self" in payload


def test_notify_suppressed_by_category(monkeypatch):
    monkeypatch.setenv("VSM_NOTIFY_ARCHIVE_ZIP", "false")
    monkeypatch.setenv("VSM_NOTIFY_ALLOW_IN_TESTS", "true")
    monkeypatch.setenv("VSM_NOTIFY_ENABLED", "true")
    with patch("urllib.request.urlopen") as op:
        ok = notify_user(
            "Archive zip x: files 1, events 0, excluded 0.",
            category=NotifyCategory.ARCHIVE_ZIP,
            force=True,
        )
    assert ok is False
    op.assert_not_called()


def test_notify_dedupe_info(monkeypatch):
    monkeypatch.setenv("VSM_NOTIFY_DEDUPE_MINUTES", "60")
    monkeypatch.setenv("VSM_ENRICH_NOTIFY_MILESTONES", "true")
    monkeypatch.setenv("VSM_NOTIFY_ALLOW_IN_TESTS", "true")
    monkeypatch.setenv("VSM_NOTIFY_ENABLED", "true")
    with patch("urllib.request.urlopen") as op:
        op.return_value.__enter__.return_value.status = 200
        with patch("os.path.isfile", return_value=False):
            msg = "Virtual Self enrichment is about 25% complete (10 of 100 pages enriched so far)."
            assert notify_user(
                msg,
                category=NotifyCategory.ENRICHMENT_MILESTONE,
                severity=NotifySeverity.INFO,
                force=True,
            )
            assert notify_user(
                msg,
                category=NotifyCategory.ENRICHMENT_MILESTONE,
                severity=NotifySeverity.INFO,
                force=True,
            ) is False
    assert op.call_count == 1


def test_notify_blocked_in_pytest_by_default(monkeypatch):
    monkeypatch.delenv("VSM_NOTIFY_ALLOW_IN_TESTS", raising=False)
    monkeypatch.setenv("VSM_NOTIFY_ENABLED", "true")
    with patch("urllib.request.urlopen") as op:
        ok = notify_user("Should not send", force=False)
    assert ok is False
    op.assert_not_called()
