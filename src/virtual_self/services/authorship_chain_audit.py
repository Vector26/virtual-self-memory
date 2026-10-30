"""Held-out split consistency + authorship corpus audit (persona P1 exit, A-8)."""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.eval.heldout_snapshot import (
    expected_split_for_row,
    heldout_dir,
    stable_split_bucket_from_key,
    validate_heldout_dir,
)
from virtual_self.persistence.db import connection, transaction

AUDIT_SAMPLE_SIZE = 50
DEFAULT_REPORT_PATH = Path(__file__).resolve().parents[3] / ".state" / "authorship_chain_audit.json"

KNOWN_ISSUES: list[dict[str, str]] = [
    {
        "ticket": "VSELF-36",
        "id": "gmail_sent_label_token",
        "summary": (
            "Gmail sent detection requires an exact X-Gmail-Labels token `sent` after "
            "comma-split; compound labels (e.g. `[Imap]/Sent` variants, spaced labels) "
            "can false-negative and skip user sent mail — see A-5 / takeout ingest."
        ),
    },
    {
        "ticket": "VSELF-36",
        "id": "gmail_reply_strip_edge_cases",
        "summary": (
            "strip_email_body may leave inline 'On … wrote:' tails on unusual Unicode "
            "spacing; validate_no_reply_leak guards ingest samples but odd mbox shapes "
            "remain worth manual spot-check."
        ),
    },
]

TRAINING_VIEW_CHECKS: list[dict[str, str]] = [
    {
        "name": "style_profiles.load_train_texts_by_register",
        "module": "virtual_self.services.style_profiles",
        "requires_split_train": True,
    },
    {
        "name": "llm_likeness_pass._load_training_corpora self branch",
        "module": "virtual_self.services.llm_likeness_pass",
        "requires_split_train": True,
    },
]


@dataclass
class AuthorshipChainAuditReport:
    generated_at: str
    subject_id: str
    heldout_manifest: dict[str, Any]
    counts: dict[str, Any]
    split_summary: dict[str, Any]
    heldout_consistency: dict[str, Any]
    training_view_leakage: dict[str, Any]
    random_audit_sample: list[dict[str, Any]]
    known_issues: list[dict[str, str]]
    split_reconcile: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "subject_id": self.subject_id,
            "heldout_manifest": self.heldout_manifest,
            "counts": self.counts,
            "split_summary": self.split_summary,
            "heldout_consistency": self.heldout_consistency,
            "training_view_leakage": self.training_view_leakage,
            "random_audit_sample": self.random_audit_sample,
            "known_issues": self.known_issues,
            "split_reconcile": self.split_reconcile,
        }


def _nested_count(rows: list[dict[str, Any]], *keys: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for row in rows:
        node = root
        for key in keys[:-1]:
            val = str(row.get(key) or "unknown")
            node = node.setdefault(val, {})
        leaf = keys[-1]
        val = str(row.get(leaf) or "unknown")
        node[val] = node.get(val, 0) + 1
    return root


def _load_manifest(heldout_root: Path) -> dict[str, Any]:
    manifest_path = heldout_root / "manifest.json"
    if not manifest_path.is_file():
        return {"error": f"missing {manifest_path}"}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _manifest_source_keys(manifest: dict[str, Any], my_life_root: Path | None) -> dict[str, set[str]]:
    notion_abs: set[str] = set()
    chat_turns: set[str] = set()
    for entry in manifest.get("entries") or []:
        kind = entry.get("source_kind")
        path = str(entry.get("source_path") or "")
        if kind == "notion":
            notion_abs.add(path)
        elif kind == "chat_user_turn":
            m = re.match(r"chat_turns:(.+)$", path)
            if m:
                chat_turns.add(m.group(1))
    rel_paths: set[str] = set()
    if my_life_root:
        for abs_path in notion_abs:
            try:
                rel_paths.add(str(Path(abs_path).resolve().relative_to(my_life_root.resolve())))
            except ValueError:
                rel_paths.add(abs_path)
    return {"notion_abs": notion_abs, "notion_rel": rel_paths, "chat_turn_ids": chat_turns}


def _heldout_file_hashes(heldout_root: Path, manifest: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    for entry in manifest.get("entries") or []:
        fname = entry.get("filename")
        if not fname:
            continue
        body = (heldout_root / str(fname)).read_text(encoding="utf-8")
        norm = re.sub(r"\s+", " ", body.strip().lower())
        hashes.add(hashlib.sha256(norm.encode()).hexdigest())
    return hashes


def _row_matches_manifest(
    row: dict[str, Any],
    keys: dict[str, set[str]],
    my_life_root: Path | None,
) -> bool:
    kind = row.get("source_kind")
    ref = row.get("source_ref") or {}
    if isinstance(ref, str):
        ref = json.loads(ref)
    if kind == "notion":
        rel = str(ref.get("rel_path") or "")
        if rel in keys["notion_rel"]:
            return True
        if my_life_root and rel:
            abs_path = str((my_life_root / rel).resolve())
            return abs_path in keys["notion_abs"]
    if kind == "chat_user_turn":
        turn_id = str(ref.get("turn_id") or "")
        return turn_id in keys["chat_turn_ids"]
    return False


def _training_view_leakage(subject_id: UUID) -> dict[str, Any]:
    """Rows that would leak into train-only views (heldout self rows)."""
    with connection() as conn:
        style_leaks = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM authored_texts
            WHERE subject_id = %s
              AND author = 'self'
              AND split = 'heldout'
              AND excluded_reason IS NULL
            """,
            (subject_id,),
        ).fetchone()["n"]
        llm_corpus_leaks = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM authored_texts
            WHERE subject_id = %s
              AND author = 'self'
              AND split = 'heldout'
              AND excluded_reason IS DISTINCT FROM 'llm_likeness_flagged'
              AND word_count >= 25
            """,
            (subject_id,),
        ).fetchone()["n"]
    return {
        "heldout_self_rows_total": int(style_leaks),
        "note": (
            "Train views must filter split='train'; counts above are heldout self rows "
            "that must stay excluded from style_profiles and LLM-likeness self corpus."
        ),
        "llm_likeness_would_include_if_unfiltered": int(llm_corpus_leaks),
        "static_checks": TRAINING_VIEW_CHECKS,
    }


def _random_audit_sample(rows: list[dict[str, Any]], *, seed: int = 42) -> list[dict[str, Any]]:
    pool = [r for r in rows if r.get("author") == "self" and not r.get("excluded_reason")]
    rng = random.Random(seed)
    sample = rng.sample(pool, min(AUDIT_SAMPLE_SIZE, len(pool)))
    out: list[dict[str, Any]] = []
    for row in sample:
        ref = row.get("source_ref") or {}
        if isinstance(ref, str):
            ref = json.loads(ref)
        out.append(
            {
                "id": str(row["id"]),
                "source_kind": row["source_kind"],
                "register": row["register"],
                "sensitivity": int(row["sensitivity"]),
                "split": row["split"],
                "word_count": int(row["word_count"]),
                "author": row["author"],
                "source_ref": ref,
                "content_preview": (row.get("content") or "")[:240],
                "reviewer_mark": None,
                "reviewer_note": "Mark true_self / false_self in Plane comment (≥95% true_self required).",
            }
        )
    return out


def reconcile_authored_splits(
    auth: AuthContext,
    *,
    my_life_root: Path | None = None,
    heldout_root: Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    root = my_life_root
    if root is None:
        settings = get_settings()
        if settings.my_life_root:
            root = Path(settings.my_life_root)

    manifest = _load_manifest(heldout_root or heldout_dir())
    keys = _manifest_source_keys(manifest, root)

    updates: list[tuple[str, Any, UUID]] = []
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, source_kind, source_ref, text_hash, split, author
            FROM authored_texts
            WHERE subject_id = %s
            """,
            (auth.subject_id,),
        ).fetchall()

    for row in rows:
        ref = row["source_ref"]
        if isinstance(ref, str):
            ref = json.loads(ref)
        expected = expected_split_for_row(
            source_kind=row["source_kind"],
            source_ref=ref,
            text_hash=row["text_hash"],
            my_life_root=root,
        )
        if row["author"] == "self" and _row_matches_manifest(dict(row, source_ref=ref), keys, root):
            expected = "heldout"
        if row["split"] != expected:
            updates.append((expected, row["id"], auth.subject_id))

    if not dry_run and updates:
        with transaction() as conn:
            for split_val, row_id, subject_id in updates:
                conn.execute(
                    """
                    UPDATE authored_texts SET split = %s
                    WHERE id = %s AND subject_id = %s
                    """,
                    (split_val, row_id, subject_id),
                )

    return {
        "dry_run": dry_run,
        "rows_checked": len(rows),
        "rows_to_update": len(updates),
        "preview": [{"id": str(i), "split": s} for s, i, _ in updates[:10]],
    }


def run_authorship_chain_audit(
    auth: AuthContext,
    *,
    heldout_root: Path | None = None,
    my_life_root: Path | None = None,
    report_path: Path | None = None,
    reconcile_splits: bool = False,
    reconcile_dry_run: bool = True,
    audit_seed: int = 42,
) -> dict[str, Any]:
    settings = get_settings()
    held_root = heldout_root or heldout_dir()
    life_root = my_life_root or (Path(settings.my_life_root) if settings.my_life_root else None)

    manifest = _load_manifest(held_root)
    manifest_errors = validate_heldout_dir(held_root)
    keys = _manifest_source_keys(manifest, life_root)
    heldout_norm_hashes = _heldout_file_hashes(held_root, manifest) if not manifest.get("error") else set()

    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, source_kind, source_ref, author, register, sensitivity, split,
                   text_hash, content, word_count, excluded_reason
            FROM authored_texts
            WHERE subject_id = %s
            ORDER BY created_at
            """,
            (auth.subject_id,),
        ).fetchall()

    row_dicts = [dict(r) for r in rows]
    for r in row_dicts:
        if isinstance(r.get("source_ref"), str):
            r["source_ref"] = json.loads(r["source_ref"])

    counts = {
        "by_source_kind": dict(Counter(str(r["source_kind"]) for r in row_dicts)),
        "by_author": dict(Counter(str(r["author"]) for r in row_dicts)),
        "by_split": dict(Counter(str(r["split"]) for r in row_dicts)),
        "self_by_source_kind_register_sensitivity": _nested_count(
            [r for r in row_dicts if r["author"] == "self"],
            "source_kind",
            "register",
            "sensitivity",
        ),
        "self_by_source_kind_split": _nested_count(
            [r for r in row_dicts if r["author"] == "self"],
            "source_kind",
            "split",
        ),
    }

    split_mismatches: list[dict[str, Any]] = []
    manifest_train_leaks: list[dict[str, Any]] = []
    content_train_leaks: list[dict[str, Any]] = []

    for row in row_dicts:
        if row["author"] != "self":
            continue
        expected = expected_split_for_row(
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            text_hash=row["text_hash"],
            my_life_root=life_root,
        )
        if _row_matches_manifest(row, keys, life_root):
            expected = "heldout"
        if row["split"] != expected:
            split_mismatches.append(
                {
                    "id": str(row["id"]),
                    "source_kind": row["source_kind"],
                    "stored_split": row["split"],
                    "expected_split": expected,
                }
            )
        if _row_matches_manifest(row, keys, life_root) and row["split"] != "heldout":
            manifest_train_leaks.append(
                {
                    "id": str(row["id"]),
                    "source_kind": row["source_kind"],
                    "split": row["split"],
                }
            )
        if row["split"] == "train" and heldout_norm_hashes:
            norm = re.sub(r"\s+", " ", (row.get("content") or "").strip().lower())
            h = hashlib.sha256(norm.encode()).hexdigest()
            if h in heldout_norm_hashes:
                content_train_leaks.append(
                    {
                        "id": str(row["id"]),
                        "source_kind": row["source_kind"],
                        "text_hash": row["text_hash"],
                    }
                )

    self_rows = [r for r in row_dicts if r["author"] == "self"]
    split_summary = {
        "self_total": len(self_rows),
        "self_train": sum(1 for r in self_rows if r["split"] == "train"),
        "self_heldout": sum(1 for r in self_rows if r["split"] == "heldout"),
        "heldout_bucket_pct": round(
            100.0 * sum(1 for r in self_rows if r["split"] == "heldout") / max(len(self_rows), 1),
            2,
        ),
        "split_mismatch_count": len(split_mismatches),
    }

    heldout_consistency = {
        "manifest_entry_count": manifest.get("entry_count"),
        "manifest_validation_errors": manifest_errors,
        "manifest_notion_sources": len(keys["notion_abs"]),
        "manifest_chat_sources": len(keys["chat_turn_ids"]),
        "manifest_source_train_split_leaks": manifest_train_leaks[:25],
        "manifest_source_train_split_leak_count": len(manifest_train_leaks),
        "eval_content_train_split_leaks": content_train_leaks[:25],
        "eval_content_train_split_leak_count": len(content_train_leaks),
        "split_mismatch_preview": split_mismatches[:25],
    }

    reconcile_out: dict[str, Any] = {}
    if reconcile_splits:
        reconcile_out = reconcile_authored_splits(
            auth,
            my_life_root=life_root,
            heldout_root=held_root,
            dry_run=reconcile_dry_run,
        )

    report = AuthorshipChainAuditReport(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        subject_id=str(auth.subject_id),
        heldout_manifest={
            "path": str(held_root),
            "entry_count": manifest.get("entry_count"),
            "validation_errors": manifest_errors,
        },
        counts=counts,
        split_summary=split_summary,
        heldout_consistency=heldout_consistency,
        training_view_leakage=_training_view_leakage(auth.subject_id),
        random_audit_sample=_random_audit_sample(row_dicts, seed=audit_seed),
        known_issues=list(KNOWN_ISSUES),
        split_reconcile=reconcile_out,
    )

    out_path = report_path or DEFAULT_REPORT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    result = report.to_dict()
    result["report_path"] = str(out_path)
    result["ok"] = (
        not manifest_errors
        and len(manifest_train_leaks) == 0
        and len(content_train_leaks) == 0
        and len(split_mismatches) == 0
    )
    return result


def stable_split_bucket_from_text_hash(text_hash: str) -> str:
    """Expose text_hash-based split for tests."""
    return stable_split_bucket_from_key(text_hash)
