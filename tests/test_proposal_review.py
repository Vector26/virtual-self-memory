from __future__ import annotations

from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.domain.errors import NotFoundError
from virtual_self.persistence.db import fetch_one, transaction
from virtual_self.services.chapter_proposal_notify import format_chapter_proposal_message
from virtual_self.services.proposal_review import (
    approve_by_ref,
    edit_by_ref,
    reject_by_ref,
)


def _career_strand_id(conn) -> UUID:
    row = fetch_one(conn, "SELECT id FROM life_strands WHERE slug = 'career'")
    assert row is not None
    return row["id"]


def _cleanup_proposed(conn, subject_id: UUID) -> None:
    conn.execute(
        """
        DELETE FROM life_chapter_evidence
        WHERE chapter_id IN (
          SELECT id FROM life_chapters WHERE subject_id = %s AND status = 'proposed'
        )
        """,
        (subject_id,),
    )
    conn.execute(
        "DELETE FROM life_chapters WHERE subject_id = %s AND status = 'proposed'",
        (subject_id,),
    )


def _insert_proposed_chapter(conn, subject_id: UUID, title: str = "Smoke chapter") -> UUID:
    chapter_id = uuid7()
    strand_id = _career_strand_id(conn)
    conn.execute(
        """
        INSERT INTO life_chapters (
          id, subject_id, strand_id, title, summary, status,
          valid_from_precision, valid_to_precision, time_precision
        ) VALUES (%s, %s, %s, %s, %s, 'proposed', 'year', 'unknown', 'year')
        """,
        (chapter_id, subject_id, strand_id, title, "summary line"),
    )
    return chapter_id


def test_format_chapter_proposal_message_uses_self_commands():
    cid = uuid7()
    msg = format_chapter_proposal_message(
        chapter_id=cid,
        strand="career",
        title="Rebuild era",
        summary="After the move",
        valid_from=None,
        valid_to=None,
        evidence_count=3,
    )
    short = str(cid).split("-", 1)[0]
    assert f"chapter proposal {short}" in msg
    assert f"/approve {short}" in msg
    assert "/reject" in msg


def test_approve_reject_edit_life_chapter_by_prefix(ensure_db):
    auth_ctx = resolve_auth(f"Bearer {__import__('os').environ['VSM_BOOTSTRAP_TOKEN']}")
    with transaction() as conn:
        _cleanup_proposed(conn, auth_ctx.subject_id)
        cid = _insert_proposed_chapter(conn, auth_ctx.subject_id, title="Before edit")
    short = str(cid).split("-", 1)[0]

    approved = approve_by_ref(auth_ctx, short)
    assert approved["status"] == "active"
    assert approved["kind"] == "life_chapter"

    with transaction() as conn:
        cid2 = _insert_proposed_chapter(conn, auth_ctx.subject_id, title="To reject")
    short2 = str(cid2).split("-", 1)[0]
    rejected = reject_by_ref(auth_ctx, short2)
    assert rejected["status"] == "rejected"

    with transaction() as conn:
        cid3 = _insert_proposed_chapter(conn, auth_ctx.subject_id, title="Old title")
    short3 = str(cid3).split("-", 1)[0]
    edited = edit_by_ref(auth_ctx, short3, text="New title from configured notifier")
    assert edited["title"] == "New title from configured notifier"

    with pytest.raises(NotFoundError):
        reject_by_ref(auth_ctx, "00000000")


def test_proposal_api_routes(client, auth_headers, ensure_db):
    auth_ctx = resolve_auth(auth_headers["Authorization"])
    with transaction() as conn:
        _cleanup_proposed(conn, auth_ctx.subject_id)
        cid = _insert_proposed_chapter(conn, auth_ctx.subject_id)
    r = client.post(f"/v1/proposals/{cid}:approve", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "active"

    with transaction() as conn:
        cid2 = _insert_proposed_chapter(conn, auth_ctx.subject_id)
    r2 = client.post(f"/v1/proposals/{cid2}:reject", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "rejected"

    r404 = client.post("/v1/proposals/deadbeef:approve", headers=auth_headers)
    assert r404.status_code == 404
