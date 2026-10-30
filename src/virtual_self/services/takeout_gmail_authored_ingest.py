"""Google Takeout loose mbox → authored_texts (gmail_sent, persona P1 rule 4)."""

from __future__ import annotations

import hashlib
import json
import mailbox
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.eval.heldout_snapshot import stable_split_bucket_from_key
from virtual_self.eval.notion_self_text import word_count
from virtual_self.persistence.db import connection, transaction
from virtual_self.services.email_body_clean import (
    gmail_labels_include_sent,
    is_auto_generated_mail,
    strip_email_body,
)

DEFAULT_MBOX_NAME = "All mail Including Spam and Trash-002.mbox"
_REGISTER = "work_formal"
_MIN_WORDS = 3


@dataclass
class TakeoutVoiceProductStats:
    """Per granted Takeout product (voice scope)."""

    keep: str = "absent"
    meta: str = "absent"
    mail_messages_total: int = 0
    mail_sent_seen: int = 0
    mail_skipped_not_sent: int = 0
    mail_skipped_auto: int = 0
    mail_skipped_empty: int = 0
    mail_skipped_short: int = 0
    rows_inserted: int = 0
    rows_skipped_duplicate: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "products": {
                "keep": {"status": self.keep, "rows": 0},
                "meta": {"status": self.meta, "rows": 0},
                "mail": {
                    "status": "parsed",
                    "messages_total": self.mail_messages_total,
                    "sent_seen": self.mail_sent_seen,
                    "skipped_not_sent": self.mail_skipped_not_sent,
                    "skipped_auto": self.mail_skipped_auto,
                    "skipped_empty": self.mail_skipped_empty,
                    "skipped_short": self.mail_skipped_short,
                    "rows_inserted": self.rows_inserted,
                    "rows_skipped_duplicate": self.rows_skipped_duplicate,
                },
            }
        }


@dataclass
class _PendingRow:
    fields: tuple[Any, ...]
    preview: str = ""


def resolve_takeout_mbox_path(
    *,
    mbox_path: Path | None = None,
    local_inbox: Path | None = None,
) -> Path:
    """Prefer explicit path, then local inbox copy, else rclone copy from Takeout root."""
    settings = get_settings()
    if mbox_path is not None:
        p = mbox_path.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        return p

    inbox = local_inbox or (settings.archive_data_root / "inbox" / "google")
    candidate = inbox / DEFAULT_MBOX_NAME
    if candidate.is_file():
        return candidate

    inbox.mkdir(parents=True, exist_ok=True)
    remote = f"{settings.archive_rclone_remote}:{settings.archive_takeout_path.rstrip('/')}/{DEFAULT_MBOX_NAME}"
    cfg = str(settings.archive_rclone_config)
    subprocess.run(
        ["rclone", "copyto", remote, str(candidate), "--config", cfg],
        check=True,
    )
    if not candidate.is_file():
        raise FileNotFoundError(f"rclone copy failed for {remote}")
    return candidate


def _normalize_email(addr: str) -> str:
    _, email = parseaddr(addr)
    return email.strip().lower()


def _self_email_set(configured: str) -> set[str]:
    out = {e.strip().lower() for e in configured.split(",") if e.strip()}
    return out


def _message_plain_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    return payload.decode("utf-8", errors="replace")


def _header_map(msg: Message) -> dict[str, str]:
    return {k.lower(): str(v) for k, v in msg.items()}


def _from_is_self(from_header: str, self_emails: set[str]) -> bool:
    if not self_emails:
        return True
    return _normalize_email(from_header) in self_emails


def iter_mbox_messages(source: Path | BinaryIO) -> Iterator[tuple[int, Message]]:
    if isinstance(source, Path):
        box = mailbox.mbox(str(source))
        try:
            for i, msg in enumerate(box):
                yield i, msg
        finally:
            box.close()
        return

    raise TypeError("stream parsing uses iter_mbox_bytes")


def _text_hash(message_index: int, content: str) -> str:
    payload = f"gmail_sent:{message_index}\n{content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _coerce_ts(raw: datetime | None) -> datetime | None:
    if raw is None:
        return None
    if raw.tzinfo is None:
        return raw.replace(tzinfo=timezone.utc)
    return raw


def parse_sent_authored_candidates(
    mbox: Path,
    *,
    self_emails: set[str],
    mbox_name: str = DEFAULT_MBOX_NAME,
) -> tuple[list[_PendingRow], TakeoutVoiceProductStats]:
    stats = TakeoutVoiceProductStats()
    pending: list[_PendingRow] = []

    for i, msg in iter_mbox_messages(mbox):
        stats.mail_messages_total += 1
        headers = _header_map(msg)
        labels = headers.get("x-gmail-labels")
        if not gmail_labels_include_sent(labels):
            stats.mail_skipped_not_sent += 1
            continue

        frm = msg.get("From", "")
        if is_auto_generated_mail(
            from_header=str(frm),
            subject=str(msg.get("Subject", "")),
            headers=headers,
        ):
            stats.mail_skipped_auto += 1
            continue

        if not _from_is_self(str(frm), self_emails):
            stats.mail_skipped_not_sent += 1
            continue

        stats.mail_sent_seen += 1
        body = strip_email_body(_message_plain_body(msg))
        if not body.strip():
            stats.mail_skipped_empty += 1
            continue
        wc = word_count(body)
        if wc < _MIN_WORDS:
            stats.mail_skipped_short += 1
            continue

        try:
            ts = parsedate_to_datetime(msg.get("Date", ""))
        except (TypeError, ValueError):
            ts = None

        source_ref = {
            "mbox": mbox_name,
            "message_index": i,
            "subject": str(msg.get("Subject", ""))[:500],
            "from": str(frm)[:500],
        }
        th = _text_hash(i, body)
        pending.append(
            _PendingRow(
                fields=(
                    uuid7(),
                    "gmail_sent",
                    json.dumps(source_ref),
                    "self",
                    0.95,
                    _REGISTER,
                    body,
                    wc,
                    th,
                    1,
                    _coerce_ts(ts),
                    "instant",
                    "email_header",
                    stable_split_bucket_from_key(th),
                ),
                preview=body[:240],
            )
        )

    return pending, stats


def run_takeout_gmail_authored_ingest(
    auth: AuthContext,
    *,
    mbox_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    self_emails = _self_email_set(settings.self_emails or "")
    path = resolve_takeout_mbox_path(mbox_path=mbox_path)
    pending, stats = parse_sent_authored_candidates(path, self_emails=self_emails)

    inserted = 0
    dupes = 0
    if not dry_run and pending:
        with transaction() as conn:
            for row in pending:
                cur = conn.execute(
                    """
                    INSERT INTO authored_texts (
                      id, subject_id, source_kind, source_ref, author, author_confidence,
                      register, content, word_count, text_hash, sensitivity,
                      authored_at, time_precision, time_source, split
                    ) VALUES (
                      %s, %s, %s, %s::jsonb, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s
                    )
                    ON CONFLICT (subject_id, text_hash) DO NOTHING
                    """,
                    (row.fields[0], auth.subject_id, *row.fields[1:]),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    dupes += 1

    stats.rows_inserted = inserted
    stats.rows_skipped_duplicate = dupes

    out = stats.to_dict()
    out["mbox_path"] = str(path)
    out["dry_run"] = dry_run
    out["self_emails_configured"] = sorted(self_emails)
    out["pending_rows"] = len(pending)
    if pending:
        out["sample_previews"] = [p.preview for p in pending[:5]]

    if not dry_run:
        with connection() as conn:
            n = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM authored_texts
                WHERE subject_id = %s AND source_kind = 'gmail_sent'
                """,
                (auth.subject_id,),
            ).fetchone()["n"]
        out["authored_gmail_rows"] = int(n)
    return out


def sample_gmail_authored_rows(subject_id: UUID, *, limit: int = 5) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT content, source_ref, register, authored_at
            FROM authored_texts
            WHERE subject_id = %s AND source_kind = 'gmail_sent' AND author = 'self'
            ORDER BY authored_at NULLS LAST
            LIMIT %s
            """,
            (subject_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def validate_no_reply_leak(content: str) -> bool:
    """True if content looks free of quoted-reply markers."""
    if re.search(r"^>\s*\S", content, re.M):
        return False
    if re.search(r"^On .+ wrote:\s*$", content, re.M | re.I):
        return False
    if "-----Original Message-----" in content:
        return False
    return True
