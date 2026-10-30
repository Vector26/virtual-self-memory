from __future__ import annotations

import json
import zipfile
from pathlib import Path

from virtual_self.archive.takeout_inventory import (
    inventory_local_zip,
    render_markdown,
    summarize_zip_entries,
    voice_verdict,
)


def _fixture_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Takeout/Chrome/History.json", json.dumps({"x": 1}))
        zf.writestr("Takeout/Mail/All mail Including Spam and Trash.mbox", "From: me\n\nhello")
        zf.writestr("Takeout/Keep/note.json", json.dumps({"title": "note"}))
        zf.writestr("Takeout/My Activity/Search/MyActivity.json", json.dumps([]))
        zf.writestr("messages/inbox/thread/message_1.json", json.dumps({"content": "hi"}))


def test_summarize_zip_entries_voice_flags():
    entries = [
        ("Takeout/Mail/a.mbox", 100),
        ("Takeout/Keep/x.json", 50),
        ("Takeout/My Activity/Search/MyActivity.json", 10),
        ("messages/inbox/t/message_1.json", 20),
    ]
    inv = summarize_zip_entries(entries, zip_name="z.zip", zip_bytes=999, source="local")
    assert inv.has_mail_mbox
    assert inv.has_keep
    assert inv.has_my_activity
    assert inv.has_meta
    names = {p.name for p in inv.products}
    assert "Mail" in names
    assert "Keep" in names
    assert "My Activity" in names
    assert "Meta (export root)" in names


def test_inventory_local_zip(tmp_path: Path):
    zpath = tmp_path / "takeout-mini.zip"
    _fixture_zip(zpath)
    inv = inventory_local_zip(zpath)
    assert inv.zip_bytes == zpath.stat().st_size
    verdict = voice_verdict([inv])
    assert all(verdict.values())


def test_render_markdown_contains_verdict_section(tmp_path: Path):
    zpath = tmp_path / "takeout-mini.zip"
    _fixture_zip(zpath)
    inv = inventory_local_zip(zpath)
    md = render_markdown([inv])
    assert "## Voice-source verdict" in md
    assert "takeout-mini.zip" in md
    assert "Mail (.mbox" in md
