from __future__ import annotations

import json
import re
from typing import Any

import duckdb

from virtual_self.archive.paths import datasets_dir, duckdb_path
from virtual_self.auth.credentials import AuthContext

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
NAME_LIKE = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")


def scrub_pii(text: str) -> str:
    t = EMAIL_RE.sub("[EMAIL]", text)
    t = NAME_LIKE.sub("[NAME]", t)
    return t


def export_datasets(
    verified_memories: list[dict[str, Any]] | None = None,
    *,
    auth: AuthContext | None = None,
) -> dict[str, Any]:
    style_path = datasets_dir() / "style.jsonl"
    facts_path = datasets_dir() / "facts_sft.jsonl"
    manifest_path = datasets_dir() / "manifest.json"

    style_rows: list[dict[str, str]] = []
    con = duckdb.connect(str(duckdb_path()))
    try:
        rows = con.execute(
            """
            SELECT text, title, product, event_type FROM events
            WHERE actor = 'self' AND (
              event_type IN ('note', 'email_sent') OR product IN ('keep', 'youtube')
            ) AND text IS NOT NULL
            LIMIT 50000
            """
        ).fetchall()
        for text, title, product, etype in rows:
            body = scrub_pii(str(text or title or ""))
            if len(body) < 8:
                continue
            style_rows.append(
                {"text": body, "meta": {"product": product, "event_type": etype}}
            )
    except duckdb.Error:
        pass
    finally:
        con.close()

    with style_path.open("w", encoding="utf-8") as f:
        for row in style_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    fact_rows: list[dict[str, Any]] = []
    merged_facts: list[dict[str, Any]] = list(verified_memories or [])
    if auth is not None:
        from virtual_self.archive.promote import archive_memories_for_dataset

        merged_facts.extend(archive_memories_for_dataset(auth))
    for mem in merged_facts:
        content = mem.get("content")
        if not content:
            continue
        fact_rows.append(
            {
                "messages": [
                    {"role": "user", "content": "What do you know about me related to this topic?"},
                    {"role": "assistant", "content": scrub_pii(str(content))},
                ],
                "evidence_event_ids": mem.get("evidence_event_ids") or [],
            }
        )
    with facts_path.open("w", encoding="utf-8") as f:
        for row in fact_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    manifest = {
        "style_lines": len(style_rows),
        "facts_lines": len(fact_rows),
        "style_path": str(style_path),
        "facts_path": str(facts_path),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
