from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.config import get_settings
from virtual_self.eval.notion_self_text import (
    extract_self_text_from_markdown,
    is_near_duplicate,
    word_count,
)
from virtual_self.ingestion.frontmatter import split_frontmatter

HELDOUT_MIN_ENTRIES = 25
HELDOUT_MAX_ENTRIES = 40
HELDOUT_MIN_WORDS = 50
HELDOUT_HASH_BUCKET = 15  # ~15% stable held-out per persona plan


def stable_split_bucket_from_key(key: str) -> str:
    """Stable train/heldout from a deterministic key (source ref or text_hash)."""
    digest = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)
    return "heldout" if (digest % 100) < HELDOUT_HASH_BUCKET else "train"


def split_key_notion_source(abs_path: str | Path) -> str:
    return f"notion:{abs_path}"


def split_key_chat_turn(turn_id: str | UUID) -> str:
    return f"chat_user_turn:chat_turns:{turn_id}"


def expected_split_for_row(
    *,
    source_kind: str,
    source_ref: dict[str, Any] | None,
    text_hash: str,
    my_life_root: Path | None = None,
) -> str:
    """Canonical split for authored_texts (aligns with eval/heldout source keys)."""
    ref = source_ref or {}
    if source_kind == "notion":
        rel = ref.get("rel_path")
        if rel and my_life_root:
            abs_path = (my_life_root / str(rel)).resolve()
            return stable_split_bucket_from_key(split_key_notion_source(abs_path))
        if rel:
            return stable_split_bucket_from_key(f"notion:{rel}")
    if source_kind == "chat_user_turn":
        turn_id = ref.get("turn_id")
        if turn_id:
            return stable_split_bucket_from_key(split_key_chat_turn(str(turn_id)))
    if source_kind == "gmail_sent":
        return stable_split_bucket_from_key(text_hash)
    return stable_split_bucket_from_key(text_hash)

_EXCLUDE_DIR_PARTS = (
    "custom templates",
    "article readings",
    "finance tracker",
    "people [",
    "media [",
    "cinematography [",
    "apparels and looks",
)
_SKIP_NAME_FRAGMENTS = ("index.md", "rows.csv", "schema.json")
_GPT_IN_TITLE = re.compile(r"\b(by gpt|gpt generated|chatgpt)\b", re.I)


@dataclass(frozen=True)
class Candidate:
    source_path: str
    source_kind: str
    authored_at: str | None
    register: str
    text: str

    @property
    def source_ref(self) -> str:
        return f"{self.source_kind}:{self.source_path}"

    @property
    def words(self) -> int:
        return word_count(self.text)

    def stable_hash(self) -> int:
        return int(hashlib.sha256(self.source_ref.encode()).hexdigest()[:8], 16)

    def expected_split(self) -> str:
        return stable_split_bucket_from_key(self.source_ref)


def heldout_dir(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[3]
    return root / "eval" / "heldout"


def _in_my_life_section(rel_path: str, section: str) -> bool:
    low = rel_path.lower()
    section = section.lower()
    return re.search(rf"(^|/){re.escape(section)}\s*\[", low) is not None


def infer_register(rel_path: str, source_kind: str) -> str:
    if source_kind == "chat_user_turn":
        return "casual_chat"
    if _in_my_life_section(rel_path, "skills") or _in_my_life_section(rel_path, "workspace"):
        return "technical"
    if _in_my_life_section(rel_path, "quests"):
        return "technical"
    if _in_my_life_section(rel_path, "journal") or _in_my_life_section(rel_path, "mental health"):
        return "journal"
    if _in_my_life_section(rel_path, "wins") or _in_my_life_section(rel_path, "fun stuff"):
        return "journal"
    if _in_my_life_section(rel_path, "misc."):
        return "journal"
    return "journal"


def _parse_authored_at(meta: dict[str, Any], path: Path) -> str | None:
    for key in ("last_edited_time", "created_time"):
        val = meta.get(key)
        if val:
            return str(val).strip('"')
    props = meta.get("properties") or {}
    if isinstance(props, dict):
        created = props.get("Created")
        if created:
            return str(created).strip('"')
    return None


def _should_skip_path(path: Path, my_life_root: Path) -> bool:
    name = path.name.lower()
    if any(s in name for s in _SKIP_NAME_FRAGMENTS):
        return True
    if _GPT_IN_TITLE.search(path.stem):
        return True
    rel = str(path.relative_to(my_life_root)).lower()
    return any(part in rel for part in _EXCLUDE_DIR_PARTS)


def collect_notion_candidates(my_life_root: Path) -> list[Candidate]:
    out: list[Candidate] = []
    for path in sorted(my_life_root.rglob("*.md")):
        if _should_skip_path(path, my_life_root):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, _ = split_frontmatter(raw)
        text = extract_self_text_from_markdown(raw)
        if not text or word_count(text) < HELDOUT_MIN_WORDS:
            continue
        rel = str(path.relative_to(my_life_root))
        out.append(
            Candidate(
                source_path=str(path),
                source_kind="notion",
                authored_at=_parse_authored_at(meta, path),
                register=infer_register(rel, "notion"),
                text=text,
            )
        )
    return out


def _fetch_chat_turns_via_docker() -> list[dict[str, Any]]:
    cmd = [
        "docker",
        "exec",
        "virtual_self_postgres",
        "psql",
        "-U",
        "virtual_self",
        "-d",
        "virtual_self",
        "-At",
        "-F",
        "\t",
        "-c",
        "SELECT id::text, created_at::text, content FROM chat_turns WHERE role='user' ORDER BY created_at",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        rows.append({"id": parts[0], "created_at": parts[1], "content": parts[2]})
    return rows


def _fetch_chat_turns_via_db() -> list[dict[str, Any]]:
    try:
        from psycopg_pool import PoolTimeout

        from virtual_self.persistence.db import connection, fetch_all, init_pool

        init_pool()
        with connection() as conn:
            return fetch_all(
                conn,
                """
                SELECT id::text AS id, created_at::text AS created_at, content
                FROM chat_turns
                WHERE role = 'user'
                ORDER BY created_at
                """,
            )
    except (PoolTimeout, OSError):
        return []


def collect_chat_candidates() -> list[Candidate]:
    rows = _fetch_chat_turns_via_docker()
    if not rows:
        rows = _fetch_chat_turns_via_db()
    out: list[Candidate] = []
    for row in rows:
        content = (row.get("content") or "").strip()
        if word_count(content) < HELDOUT_MIN_WORDS:
            continue
        if "### DS" in content:
            continue
        out.append(
            Candidate(
                source_path=f"chat_turns:{row['id']}",
                source_kind="chat_user_turn",
                authored_at=row.get("created_at"),
                register="casual_chat",
                text=content,
            )
        )
    return out


def _heldout_bucket(candidate: Candidate) -> bool:
    return (candidate.stable_hash() % 100) < HELDOUT_HASH_BUCKET


def select_heldout_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Pick 25–40 entries with stable hash split and register diversity."""
    unique: list[Candidate] = []
    seen_texts: list[str] = []
    for cand in sorted(candidates, key=lambda c: c.stable_hash()):
        if is_near_duplicate(cand.text, seen_texts):
            continue
        seen_texts.append(cand.text)
        unique.append(cand)

    primary = [c for c in unique if _heldout_bucket(c)]
    if len(primary) < HELDOUT_MIN_ENTRIES:
        rest = [c for c in unique if c not in primary]
        primary.extend(rest[: HELDOUT_MIN_ENTRIES - len(primary)])

    selected = primary[:HELDOUT_MAX_ENTRIES]

    registers = {c.register for c in selected}
    needed = {"journal", "casual_chat", "technical"} - registers
    if needed:
        pool = [c for c in unique if c not in selected]
        for reg in sorted(needed):
            for cand in pool:
                if cand.register == reg:
                    selected.append(cand)
                    break
        selected = selected[:HELDOUT_MAX_ENTRIES]

    if len(selected) < HELDOUT_MIN_ENTRIES:
        for cand in unique:
            if cand in selected:
                continue
            selected.append(cand)
            if len(selected) >= HELDOUT_MIN_ENTRIES:
                break

    return selected[:HELDOUT_MAX_ENTRIES]


def build_manifest_entries(selected: list[Candidate]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for idx, cand in enumerate(selected, start=1):
        entries.append(
            {
                "id": f"heldout_{idx:03d}",
                "filename": f"heldout_{idx:03d}.txt",
                "source_path": cand.source_path,
                "source_kind": cand.source_kind,
                "authored_at": cand.authored_at,
                "register": cand.register,
                "word_count": cand.words,
            }
        )
    return entries


def build_heldout_snapshot(
    *,
    my_life_root: Path | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    root = my_life_root or settings.my_life_root
    target = out_dir or heldout_dir()
    target.mkdir(parents=True, exist_ok=True)

    for old in target.glob("heldout_*.txt"):
        old.unlink()

    candidates = collect_notion_candidates(root) + collect_chat_candidates()
    selected = select_heldout_candidates(candidates)
    if len(selected) < HELDOUT_MIN_ENTRIES:
        raise ValueError(
            f"only {len(selected)} held-out candidates (>={HELDOUT_MIN_WORDS} words); need {HELDOUT_MIN_ENTRIES}"
        )

    entries = build_manifest_entries(selected)
    for entry, cand in zip(entries, selected, strict=True):
        (target / entry["filename"]).write_text(cand.text + "\n", encoding="utf-8")

    manifest = {
        "version": 1,
        "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "min_words": HELDOUT_MIN_WORDS,
        "entry_count": len(entries),
        "registers": sorted({e["register"] for e in entries}),
        "entries": entries,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def validate_heldout_dir(path: Path | None = None) -> list[str]:
    """Return a list of validation errors (empty if valid)."""
    errors: list[str] = []
    root = path or heldout_dir()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return ["missing manifest.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"manifest.json invalid: {exc}"]

    entries = manifest.get("entries") or []
    if len(entries) < HELDOUT_MIN_ENTRIES:
        errors.append(f"manifest has {len(entries)} entries; need >={HELDOUT_MIN_ENTRIES}")
    registers = {e.get("register") for e in entries}
    if len(registers) < 3:
        errors.append(f"need >=3 registers; got {sorted(registers)}")

    for entry in entries:
        fname = entry.get("filename")
        if not fname:
            errors.append("entry missing filename")
            continue
        fpath = root / fname
        if not fpath.is_file():
            errors.append(f"missing file {fname}")
            continue
        text = fpath.read_text(encoding="utf-8")
        if "### DS" in text:
            errors.append(f"{fname} contains LLM answer marker ### DS")
        wc = word_count(text)
        if wc < HELDOUT_MIN_WORDS:
            errors.append(f"{fname} has {wc} words; need >={HELDOUT_MIN_WORDS}")
        if entry.get("word_count") != wc:
            errors.append(f"{fname} manifest word_count mismatch ({entry.get('word_count')} vs {wc})")

    return errors
