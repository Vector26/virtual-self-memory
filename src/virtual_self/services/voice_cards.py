"""Evidence-linked voice card v2 (V-2) — stylometry rules + proposal flow."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import ConflictError, NotFoundError, ValidationError
from virtual_self.eval.register_stylometry import PRIMARY_REGISTERS, extract_text_features
from virtual_self.eval.slop import check_slop
from virtual_self.persistence.db import fetch_all, fetch_one, transaction
from virtual_self.services.chapter_proposal_notify import notify_personality_proposal
from virtual_self.eval.slop_seeds import all_anti_pattern_seeds
from virtual_self.services.style_profiles import STYLE_PROFILE_VERSION
from virtual_self.services.voice_exemplars import sync_anti_patterns_from_slop_baseline

VOICE_CARD_VERSION = 2
VOICE_CARD_DOC = Path(__file__).resolve().parents[3] / "docs" / "VOICE_CARD.md"
IPIP_PRIVATE_PATH = Path(__file__).resolve().parents[3] / ".state" / "persona" / "ipip_neo_private.json"

_MIN_RULES = 10
_MIN_PET_PHRASES = 3
_MAX_EVIDENCE_PER_CLAIM = 4

_BANNED_OPENER_RE = re.compile(
    r"(?im)^\s*(?:great question|here(?:'s| is)|certainly|absolutely|of course|as an ai)\b"
)
_PHRASE_IN_TEXT = re.compile(r"[a-z']+", re.I)


@dataclass
class VoiceCardClaim:
    text: str
    register: str | None = None
    evidence_authored_text_ids: list[str] = field(default_factory=list)


@dataclass
class VoiceCardV2:
    card_version: int
    generated_at: str
    rules: list[VoiceCardClaim]
    pet_phrases: list[VoiceCardClaim]
    anti_patterns: list[str]
    registers_with_data: list[str]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_version": self.card_version,
            "generated_at": self.generated_at,
            "rules": [asdict(r) for r in self.rules],
            "pet_phrases": [asdict(p) for p in self.pet_phrases],
            "anti_patterns": self.anti_patterns,
            "registers_with_data": self.registers_with_data,
            "markdown": self.markdown,
        }


def _active_profile_id(conn, subject_id: UUID) -> UUID:
    row = fetch_one(
        conn,
        """
        SELECT id FROM personality_profiles
        WHERE subject_id = %s AND status = 'active'
        ORDER BY activated_at DESC NULLS LAST
        LIMIT 1
        """,
        (subject_id,),
    )
    if row is None:
        raise NotFoundError("no active personality profile — bootstrap personality first")
    return row["id"]


def _load_style_features_by_register(conn, subject_id: UUID) -> dict[str, dict[str, Any]]:
    rows = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (register) register, features, n_words, n_texts
        FROM style_profiles
        WHERE subject_id = %s AND version = %s
        ORDER BY register, created_at DESC
        """,
        (subject_id, STYLE_PROFILE_VERSION),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        feat = row["features"]
        if isinstance(feat, str):
            feat = json.loads(feat)
        out[str(row["register"])] = feat
    return out


@dataclass(frozen=True)
class _AuthoredRow:
    id: UUID
    register: str
    content: str


def _load_train_authored_rows(subject_id: UUID, register: str | None = None) -> list[_AuthoredRow]:
    with transaction() as conn:
        if register:
            rows = fetch_all(
                conn,
                """
                SELECT id, register, content
                FROM authored_texts
                WHERE subject_id = %s AND author = 'self' AND split = 'train' AND register = %s
                ORDER BY authored_at NULLS LAST, id
                """,
                (subject_id, register),
            )
        else:
            rows = fetch_all(
                conn,
                """
                SELECT id, register, content
                FROM authored_texts
                WHERE subject_id = %s AND author = 'self' AND split = 'train'
                ORDER BY authored_at NULLS LAST, id
                """,
                (subject_id,),
            )
    return [_AuthoredRow(id=r["id"], register=str(r["register"]), content=str(r["content"])) for r in rows]


def _pick_evidence(
    rows: list[_AuthoredRow],
    predicate: Callable[[_AuthoredRow], bool],
    *,
    limit: int = _MAX_EVIDENCE_PER_CLAIM,
) -> list[str]:
    ids: list[str] = []
    for row in rows:
        if not row.content.strip():
            continue
        if predicate(row):
            ids.append(str(row.id))
        if len(ids) >= limit:
            break
    return ids


def _pick_evidence_or_any(
    rows: list[_AuthoredRow],
    predicate: Callable[[_AuthoredRow], bool],
    *,
    limit: int = _MAX_EVIDENCE_PER_CLAIM,
) -> list[str]:
    """Prefer predicate matches; always cite at least one train row when corpus exists."""
    picked = _pick_evidence(rows, predicate, limit=limit)
    if picked:
        return picked
    return _pick_evidence(rows, lambda r: bool(r.content.strip()), limit=limit)


def _words_in_band(row: _AuthoredRow, low: float, high: float) -> bool:
    wc = extract_text_features(row.content).get("words", 0)
    return low <= float(wc) <= high


def _phrase_present(row: _AuthoredRow, phrase: str) -> bool:
    return phrase.lower() in row.content.lower()


def sync_voice_anti_patterns(auth: AuthContext) -> list[str]:
    sync_anti_patterns_from_slop_baseline(auth)
    return [pattern for pattern, kind, _sev, _origin in all_anti_pattern_seeds() if kind == "phrase"]


def _trait_tone_hint() -> str | None:
    """Qualitative tone hint only — never expose numeric IPIP scores."""
    if not IPIP_PRIVATE_PATH.is_file():
        return None
    try:
        payload = json.loads(IPIP_PRIVATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not payload.get("facet_order_30"):
        return None
    return (
        "Under stress, writing tends toward self-critique and low extraversion "
        "(IPIP private file — no scores in prompts)."
    )


def build_voice_card_v2(auth: AuthContext) -> VoiceCardV2:
    with transaction() as conn:
        features_by_reg = _load_style_features_by_register(conn, auth.subject_id)

    all_rows = _load_train_authored_rows(auth.subject_id)
    if not all_rows:
        raise NotFoundError("no train-split authored_texts — ingest corpus first")

    registers_with_data = [
        reg
        for reg in PRIMARY_REGISTERS
        if (features_by_reg.get(reg) or {}).get("sufficient")
    ]

    rules: list[VoiceCardClaim] = []
    pet_phrases: list[VoiceCardClaim] = []

    # Global structure / slop rules (all registers)
    rules.append(
        VoiceCardClaim(
            text="Do not open with assistant filler (Certainly, Great question, As an AI).",
            register=None,
            evidence_authored_text_ids=_pick_evidence_or_any(
                all_rows, lambda r: not _BANNED_OPENER_RE.search(r.content)
            ),
        )
    )
    rules.append(
        VoiceCardClaim(
            text="Skip markdown headers and bullet lists unless the user explicitly asks for structure.",
            register=None,
            evidence_authored_text_ids=_pick_evidence_or_any(
                all_rows,
                lambda r: extract_text_features(r.content).get("markdown_headers_per_100_words", 0) == 0
                and extract_text_features(r.content).get("markdown_bullets_per_100_words", 0) == 0,
            ),
        )
    )
    rules.append(
        VoiceCardClaim(
            text="Avoid therapist boilerplate and validation clichés; stay direct.",
            register=None,
            evidence_authored_text_ids=_pick_evidence_or_any(
                all_rows,
                lambda r: check_slop(r.content[:2000], register=r.register or "journal").passed,
            ),
        )
    )
    rules.append(
        VoiceCardClaim(
            text="Say when memory is missing instead of inventing facts or motives.",
            register=None,
            evidence_authored_text_ids=_pick_evidence_or_any(
                all_rows,
                lambda r: "don't" in r.content.lower() or "dont" in r.content.lower() or "not sure" in r.content.lower(),
            )
            or _pick_evidence_or_any(all_rows, lambda r: len(r.content) > 40),
        )
    )

    trait_hint = _trait_tone_hint()
    if trait_hint:
        trait_evidence = _pick_evidence(
            all_rows,
            lambda r: r.register == "journal" and len(r.content.split()) >= 30,
        )
        if trait_evidence:
            rules.append(
                VoiceCardClaim(
                    text=trait_hint,
                    register=None,
                    evidence_authored_text_ids=trait_evidence,
                )
            )

    for register in PRIMARY_REGISTERS:
        feat = features_by_reg.get(register) or {}
        reg_rows = [r for r in all_rows if r.register == register]
        if not feat.get("sufficient") or not reg_rows:
            continue
        length = feat.get("length") or {}
        wpm = (length.get("words_per_message") or {}).get("mean") or (length.get("words_per_message") or {}).get("p50")
        if wpm:
            low = float(wpm) * 0.55
            high = float(wpm) * 1.45
            wpm_evidence = _pick_evidence(
                reg_rows,
                lambda r, lo=low, hi=high: _words_in_band(r, lo, hi),
            )
            if wpm_evidence:
                rules.append(
                    VoiceCardClaim(
                        text=f"In {register}, aim for ~{int(round(float(wpm)))} words per message (typical train median).",
                        register=register,
                        evidence_authored_text_ids=wpm_evidence,
                    )
                )
        punct = feat.get("punctuation_per_100_words") or {}
        q_rate = feat.get("question_rate")
        if q_rate is not None and float(q_rate) < 0.08:
            q_evidence = _pick_evidence(
                reg_rows,
                lambda r: extract_text_features(r.content).get("question_rate", 0) < 0.15,
            )
            if q_evidence:
                rules.append(
                    VoiceCardClaim(
                        text=f"In {register}, questions are rare — prefer statements unless clarifying.",
                        register=register,
                        evidence_authored_text_ids=q_evidence,
                    )
                )
        hedges = (feat.get("lexicon") or {}).get("hedges_per_100_words")
        if hedges is not None:
            hedge_evidence = _pick_evidence(reg_rows, lambda r: len(r.content) > 20)
            if hedge_evidence:
                rules.append(
                    VoiceCardClaim(
                        text=f"In {register}, keep hedging near train rate (~{hedges}/100w); don't sound like a disclaimer bot.",
                        register=register,
                        evidence_authored_text_ids=hedge_evidence,
                    )
                )
        lex = feat.get("lexicon") or {}
        for pet in (lex.get("pet_phrases") or [])[: _MIN_PET_PHRASES]:
            phrase = str(pet.get("phrase") or "")
            if not phrase:
                continue
            ev = _pick_evidence(reg_rows, lambda r, p=phrase: _phrase_present(r, p))
            if ev:
                pet_phrases.append(
                    VoiceCardClaim(
                        text=f'Pet phrase ({register}): "{phrase}"',
                        register=register,
                        evidence_authored_text_ids=ev,
                    )
                )

    fallbacks = [
        "Prefer plain sentences; measured orthography in output (clean typing — don't mimic typos).",
        "Lead with the answer, then context — no preamble.",
        "Match register: journal is reflective; technical is precise; chat is short.",
        "Use first person for self-knowledge; don't narrate as 'the user'.",
    ]
    fi = 0
    while len(rules) < _MIN_RULES and fi < len(fallbacks):
        rules.append(
            VoiceCardClaim(
                text=fallbacks[fi],
                register=None,
                evidence_authored_text_ids=_pick_evidence_or_any(all_rows, lambda r: len(r.content) > 25),
            )
        )
        fi += 1

    while len(pet_phrases) < _MIN_PET_PHRASES:
        for row in all_rows:
            words = _PHRASE_IN_TEXT.findall(row.content.lower())
            if len(words) < 4:
                continue
            bg = f"{words[0]} {words[1]}"
            if any(p.text.endswith(bg) for p in pet_phrases):
                continue
            pet_phrases.append(
                VoiceCardClaim(
                    text=f'Pet phrase ({row.register}): "{bg}"',
                    register=row.register,
                    evidence_authored_text_ids=[str(row.id)],
                )
            )
            if len(pet_phrases) >= _MIN_PET_PHRASES:
                break
        else:
            break

    anti_patterns = sync_voice_anti_patterns(auth)
    card = VoiceCardV2(
        card_version=VOICE_CARD_VERSION,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        rules=rules,
        pet_phrases=pet_phrases,
        anti_patterns=anti_patterns,
        registers_with_data=registers_with_data,
    )
    card.markdown = render_voice_card_markdown(card)
    validate_voice_card(card)
    return card


def validate_voice_card(card: VoiceCardV2) -> None:
    missing: list[str] = []
    for idx, rule in enumerate(card.rules):
        if not rule.evidence_authored_text_ids:
            missing.append(f"rule[{idx}]")
    for idx, pet in enumerate(card.pet_phrases):
        if not pet.evidence_authored_text_ids:
            missing.append(f"pet_phrase[{idx}]")
    if missing:
        raise ValidationError(
            "voice card claims must cite authored_text ids",
            details={"missing_evidence": missing},
        )


def render_voice_card_markdown(card: VoiceCardV2) -> str:
    lines = [
        f"# Voice card v{card.card_version} (proposed)",
        "",
        f"Generated: `{card.generated_at}` — evidence-linked; held-out excluded from stylometry.",
        "",
        "## Global rules",
    ]
    for rule in card.rules:
        if rule.register:
            continue
        ev = ", ".join(f"`{e}`" for e in rule.evidence_authored_text_ids[:3])
        lines.append(f"- {rule.text} _(evidence: {ev})_")

    for register in card.registers_with_data:
        lines.extend(["", f"## {register} register"])
        for rule in card.rules:
            if rule.register != register:
                continue
            ev = ", ".join(f"`{e}`" for e in rule.evidence_authored_text_ids[:3])
            lines.append(f"- {rule.text} _(evidence: {ev})_")

    lines.extend(["", "## Pet phrases"])
    for pet in card.pet_phrases:
        ev = ", ".join(f"`{e}`" for e in pet.evidence_authored_text_ids[:2])
        lines.append(f"- {pet.text} _(evidence: {ev})_")

    lines.extend(["", "## Anti-patterns (never imitate)"])
    for pat in card.anti_patterns[:12]:
        lines.append(f"- {pat}")

    return "\n".join(lines).rstrip() + "\n"


def serialize_proposed_content(card: VoiceCardV2) -> str:
    return json.dumps(card.to_dict(), ensure_ascii=False)


def parse_proposed_voice_content(raw: str) -> tuple[dict[str, Any] | None, str]:
    text = (raw or "").strip()
    if not text:
        return None, ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                md = str(payload.get("markdown") or "")
                return payload, md or text
        except json.JSONDecodeError:
            pass
    return None, text


def write_voice_card_doc(card: VoiceCardV2, path: Path | None = None) -> Path:
    target = path or VOICE_CARD_DOC
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(card.markdown, encoding="utf-8")
    return target


def _collect_proposal_evidence_ids(card: VoiceCardV2) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for claim in (*card.rules, *card.pet_phrases):
        for aid in claim.evidence_authored_text_ids:
            pairs.append((aid, "supports"))
    return pairs


def propose_voice_card_v2(
    auth: AuthContext,
    *,
    dry_run: bool = False,
    notify: bool = True,
    doc_path: Path | None = None,
) -> dict[str, Any]:
    card = build_voice_card_v2(auth)
    write_voice_card_doc(card, doc_path)
    if dry_run:
        return {
            "dry_run": True,
            "card_version": card.card_version,
            "rule_count": len(card.rules),
            "pet_phrase_count": len(card.pet_phrases),
            "registers_with_data": card.registers_with_data,
            "doc_path": str(doc_path or VOICE_CARD_DOC),
        }

    proposal_id = uuid7()
    proposed_content = serialize_proposed_content(card)
    reason = "Voice card v2 from train stylometry (V-1 profiles) — approve to activate facet."

    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        pending = fetch_one(
            conn,
            """
            SELECT id FROM personality_proposals
            WHERE profile_id = %s AND facet_type = 'voice' AND status = 'pending'
            LIMIT 1
            """,
            (profile_id,),
        )
        if pending:
            raise ConflictError(
                "pending voice proposal already exists",
                details={"proposal_id": str(pending["id"])},
            )

        conn.execute(
            """
            INSERT INTO personality_proposals (
              id, profile_id, facet_type, proposed_content, reason,
              confidence, proposed_by, origin_mode, status
            ) VALUES (
              %s, %s, 'voice', %s, %s,
              0.85, %s, 'Self', 'pending'
            )
            """,
            (proposal_id, profile_id, proposed_content, reason, auth.actor_id),
        )
        for authored_id, relationship in _collect_proposal_evidence_ids(card):
            conn.execute(
                """
                INSERT INTO personality_proposal_evidence (
                  id, proposal_id, authored_text_id, relationship
                ) VALUES (%s, %s, %s, %s)
                """,
                (uuid7(), proposal_id, UUID(authored_id), relationship),
            )

    notified = False
    if notify:
        notified = notify_personality_proposal(
            proposal_id=proposal_id,
            facet_type="voice",
            proposed_content=card.markdown[:500],
            origin_mode="Self",
            reason=reason,
        )

    return {
        "dry_run": False,
        "proposal_id": str(proposal_id),
        "status": "pending",
        "facet_type": "voice",
        "rule_count": len(card.rules),
        "pet_phrase_count": len(card.pet_phrases),
        "evidence_links": len(_collect_proposal_evidence_ids(card)),
        "notified": notified,
        "doc_path": str(doc_path or VOICE_CARD_DOC),
    }


def apply_approved_voice_proposal(auth: AuthContext, proposal_id: UUID) -> dict[str, Any]:
    """On approve: bump voice facet version and copy proposal evidence lineage."""
    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        row = fetch_one(
            conn,
            """
            SELECT id, proposed_content, status
            FROM personality_proposals
            WHERE id = %s AND profile_id = %s AND facet_type = 'voice'
            FOR UPDATE
            """,
            (proposal_id, profile_id),
        )
        if row is None:
            raise NotFoundError("voice proposal not found")
        if row["status"] != "approved":
            raise ConflictError("proposal must be approved before apply", details={"status": row["status"]})

        payload, markdown = parse_proposed_voice_content(row["proposed_content"])
        facet_content = markdown.strip()
        if not facet_content:
            raise ValidationError("voice proposal has empty content")

        facet_row = fetch_one(
            conn,
            "SELECT id, current_version_id FROM personality_facets WHERE profile_id = %s AND facet_type = 'voice'",
            (profile_id,),
        )
        if facet_row is None:
            raise NotFoundError("voice facet missing on profile")

        cur_ver = fetch_one(
            conn,
            "SELECT version FROM personality_facet_versions WHERE id = %s",
            (facet_row["current_version_id"],),
        )
        next_ver = (cur_ver["version"] if cur_ver else 0) + 1
        if facet_row["current_version_id"]:
            conn.execute(
                "UPDATE personality_facet_versions SET status = 'superseded' WHERE id = %s",
                (facet_row["current_version_id"],),
            )
        ver_id = uuid7()
        conn.execute(
            """
            INSERT INTO personality_facet_versions (
              id, facet_id, version, content, priority, confidence, status, approved_by, approved_at
            ) VALUES (%s, %s, %s, %s, 80, 0.9, 'active', %s, NOW())
            """,
            (ver_id, facet_row["id"], next_ver, facet_content, auth.actor_id),
        )
        conn.execute(
            "UPDATE personality_facets SET current_version_id = %s WHERE id = %s",
            (ver_id, facet_row["id"]),
        )

        evidence_rows = fetch_all(
            conn,
            """
            SELECT authored_text_id, relationship
            FROM personality_proposal_evidence
            WHERE proposal_id = %s AND authored_text_id IS NOT NULL
            """,
            (proposal_id,),
        )
        for ev in evidence_rows:
            conn.execute(
                """
                INSERT INTO personality_facet_evidence (
                  id, facet_version_id, authored_text_id, relationship
                ) VALUES (%s, %s, %s, %s)
                """,
                (uuid7(), ver_id, ev["authored_text_id"], ev["relationship"]),
            )

    return {
        "facet_type": "voice",
        "facet_version": next_ver,
        "proposal_id": str(proposal_id),
        "content_chars": len(facet_content),
        "card_version": (payload or {}).get("card_version"),
    }
