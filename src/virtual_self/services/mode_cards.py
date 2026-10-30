"""Evidence-linked, user-defined mode cards with fictional defaults."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import ConflictError, NotFoundError, ValidationError
from virtual_self.persistence.db import fetch_all, fetch_one, transaction
from virtual_self.services.chapter_proposal_notify import notify_personality_proposal
from virtual_self.services.trait_values_profile import (
    _active_profile_id,
    _load_digest_evidence,
    _memory_version_for_memory,
)

MODE_CARD_VERSION = 1
MODE_CARD_DOC = Path(__file__).resolve().parents[3] / "docs" / "MODE_CARDS.md"

ModeName = Literal["Analyst", "Guide", "Mediator", "Explorer"]
CANONICAL_MODES: tuple[ModeName, ...] = ("Analyst", "Guide", "Mediator", "Explorer")

_MIN_EVIDENCE = 3
_DEFAULT_MODE_ERA = datetime(2024, 1, 1, tzinfo=timezone.utc)

_MODE_MATCHERS: dict[ModeName, re.Pattern[str]] = {
    "Analyst": re.compile(r"\banalyst\b", re.I),
    "Guide": re.compile(r"\bguide\b", re.I),
    "Mediator": re.compile(r"\bmediator\b", re.I),
    "Explorer": re.compile(r"\bexplorer\b", re.I),
}

_OTHER_VIEW_MARKERS = re.compile(
    r"\b(external analysis|according to an assistant)\b",
    re.I,
)

ModeStatus = Literal["active", "aspirational", "retired"]


@dataclass
class ModeEvidenceRef:
    memory_ids: list[str] = field(default_factory=list)
    authored_text_ids: list[str] = field(default_factory=list)


@dataclass
class ModeCard:
    name: ModeName
    card_version: int
    generated_at: str
    role_summary: str
    status: ModeStatus
    introduced_at: str | None
    chapter_valid_from: str | None
    chapter_valid_to: str | None
    triggers: list[str]
    voice_delta: dict[str, Any]
    values_delta: dict[str, Any]
    exemplars: list[dict[str, str]]
    evidence: ModeEvidenceRef
    manual_only: bool = True
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "card_version": self.card_version,
            "generated_at": self.generated_at,
            "role_summary": self.role_summary,
            "status": self.status,
            "introduced_at": self.introduced_at,
            "chapter_valid_from": self.chapter_valid_from,
            "chapter_valid_to": self.chapter_valid_to,
            "triggers": self.triggers,
            "voice_delta": self.voice_delta,
            "values_delta": self.values_delta,
            "exemplars": self.exemplars,
            "evidence": asdict(self.evidence),
            "manual_only": self.manual_only,
            "markdown": self.markdown,
        }


_MODE_DEFAULTS: dict[ModeName, dict[str, Any]] = {
    "Analyst": {
        "status": "active",
        "role_summary": "A concise mode for structured analysis and concrete next steps.",
        "voice_delta": {"register": "concise", "notes": "state assumptions and evidence"},
        "values_delta": {"emphasis": "clarity, accuracy, practical action"},
        "trigger_seeds": ("analyze", "compare", "evidence", "decision", "plan"),
        "era": _DEFAULT_MODE_ERA,
    },
    "Guide": {
        "status": "active",
        "role_summary": "A supportive mode for explanations, reflection, and learning.",
        "voice_delta": {"register": "supportive", "notes": "use patient, plain language"},
        "values_delta": {"emphasis": "understanding, context, sustainable progress"},
        "trigger_seeds": ("learn", "explain", "reflect", "context", "guide"),
        "era": _DEFAULT_MODE_ERA,
    },
    "Mediator": {
        "status": "aspirational",
        "role_summary": "A measured mode for reconciling competing constraints.",
        "voice_delta": {"register": "measured", "notes": "surface trade-offs without forcing consensus"},
        "values_delta": {"emphasis": "balance, fairness, explicit trade-offs"},
        "trigger_seeds": ("balance", "mediate", "trade-off", "constraints", "options"),
        "era": _DEFAULT_MODE_ERA,
    },
    "Explorer": {
        "status": "active",
        "role_summary": "A curious mode for brainstorming and low-risk experiments.",
        "voice_delta": {"register": "curious", "notes": "offer testable possibilities"},
        "values_delta": {"emphasis": "exploration, creativity, reversible experiments"},
        "trigger_seeds": ("explore", "brainstorm", "experiment", "prototype", "ideas"),
        "era": _DEFAULT_MODE_ERA,
    },
}


def facet_type_for_mode(name: ModeName) -> str:
    return f"mode_{name.lower()}"


def mode_register_markers(name: ModeName) -> tuple[str, ...]:
    """Voice/register tokens for deterministic mode adherence checks (TR-7 — qualitative only)."""
    voice = _MODE_DEFAULTS[name]["voice_delta"]
    register = str(voice.get("register") or "")
    notes = str(voice.get("notes") or "")
    emphasis = str(_MODE_DEFAULTS[name]["values_delta"].get("emphasis") or "")
    blob = f"{register}, {notes}, {emphasis}".lower()
    tokens: list[str] = []
    for part in re.split(r"[,;/]+|\s+—\s+|\s+-\s+", blob):
        t = part.strip()
        if len(t) >= 4 and t not in tokens:
            tokens.append(t)
    return tuple(tokens[:12])


def _text_mentions_mode(text: str, mode: ModeName) -> bool:
    if _OTHER_VIEW_MARKERS.search(text):
        return False
    return bool(_MODE_MATCHERS[mode].search(text))


def _collect_digest_hits(
    rows: list[dict[str, Any]], mode: ModeName
) -> tuple[list[str], list[str]]:
    memory_ids: list[str] = []
    snippets: list[str] = []
    for row in rows:
        text = f"{row.get('text') or ''}"
        layer = str(row.get("layer") or "")
        if layer == "modes" or _text_mentions_mode(text, mode):
            for mid in row.get("memory_ids") or []:
                if mid not in memory_ids:
                    memory_ids.append(mid)
            if text.strip():
                snippets.append(text.strip())
    return memory_ids, snippets


def _collect_authored_hits(conn, subject_id: UUID, mode: ModeName) -> tuple[list[str], list[str]]:
    rows = fetch_all(
        conn,
        """
        SELECT id, content, mode_hint
        FROM authored_texts
        WHERE subject_id = %s AND author = 'self' AND split = 'train'
          AND invalidated_at IS NULL
        ORDER BY COALESCE(authored_at, ingested_at)
        LIMIT 4000
        """,
        (subject_id,),
    )
    ids: list[str] = []
    snippets: list[str] = []
    hint = mode.lower()
    for row in rows:
        content = str(row.get("content") or "")
        mode_hint = str(row.get("mode_hint") or "").lower()
        if mode_hint and hint not in mode_hint:
            if not _text_mentions_mode(content, mode):
                continue
        elif not _text_mentions_mode(content, mode):
            continue
        aid = str(row["id"])
        if aid not in ids:
            ids.append(aid)
        snippets.append(re.sub(r"\s+", " ", content).strip()[:240])
    return ids, snippets


def _memory_chapter_window(conn, memory_ids: list[str]) -> tuple[str | None, str | None]:
    if not memory_ids:
        return None, None
    row = fetch_one(
        conn,
        """
        SELECT MIN(c.valid_from) AS vf, MAX(c.valid_to) AS vt
        FROM life_chapter_evidence e
        JOIN life_chapters c ON c.id = e.chapter_id
        WHERE e.memory_id = ANY(%s::uuid[])
        """,
        (memory_ids,),
    )
    if not row or not row.get("vf"):
        row = fetch_one(
            conn,
            """
            SELECT MIN(m.valid_from) AS vf, MAX(m.valid_to) AS vt
            FROM memories m
            WHERE m.id = ANY(%s::uuid[])
            """,
            (memory_ids,),
        )
    if not row or not row.get("vf"):
        return None, None
    vf = row["vf"]
    vt = row.get("vt")
    return (
        vf.isoformat() if hasattr(vf, "isoformat") else str(vf),
        vt.isoformat() if vt and hasattr(vt, "isoformat") else (str(vt) if vt else None),
    )


def _build_triggers(mode: ModeName, snippets: list[str]) -> list[str]:
    seeds = _MODE_DEFAULTS[mode]["trigger_seeds"]
    found: list[str] = []
    blob = " ".join(snippets).lower()
    for seed in seeds:
        if seed in blob and seed not in found:
            found.append(seed)
    for seed in seeds:
        if seed not in found:
            found.append(seed)
        if len(found) >= 5:
            break
    return found[:6]


def _build_exemplars(
    memory_ids: list[str], snippets: list[str]
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for idx, snippet in enumerate(snippets[:4]):
        if not snippet:
            continue
        ref: dict[str, str] = {"text": snippet[:220]}
        if idx < len(memory_ids):
            ref["memory_id"] = memory_ids[idx]
        out.append(ref)
    return out


def _summarize_role(mode: ModeName, snippets: list[str]) -> str:
    base = str(_MODE_DEFAULTS[mode]["role_summary"])
    for snippet in snippets:
        if len(snippet) >= 40 and _text_mentions_mode(snippet, mode):
            return re.sub(r"\s+", " ", snippet).strip()[:320]
    return base


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_mode_card_inner(
    auth: AuthContext,
    mode: ModeName,
    conn,
    *,
    digest_status: str,
    digest_rows: list[dict[str, Any]] | None,
) -> ModeCard:
    rows = digest_rows if digest_rows is not None else _load_digest_evidence(
        conn, auth.subject_id, digest_status=digest_status
    )
    mem_from_digest, snippets_digest = _collect_digest_hits(rows, mode)
    mem_ids = list(mem_from_digest)
    snippets = list(snippets_digest)
    authored_ids, snippets_auth = _collect_authored_hits(conn, auth.subject_id, mode)
    snippets.extend(snippets_auth)

    if len(mem_ids) < _MIN_EVIDENCE:
        extra = fetch_all(
            conn,
            """
            SELECT DISTINCT m.id::text AS id
            FROM memories m
            JOIN memory_versions mv ON mv.memory_id = m.id AND mv.status = 'active'
            WHERE m.subject_id = %s
              AND mv.content ~* %s
            ORDER BY m.id
            LIMIT 12
            """,
            (auth.subject_id, _MODE_MATCHERS[mode].pattern),
        )
        for row in extra:
            mid = row["id"]
            if mid not in mem_ids:
                mem_ids.append(mid)

    defaults = _MODE_DEFAULTS[mode]
    introduced = defaults["era"].strftime("%Y-%m")
    chapter_from, chapter_to = _memory_chapter_window(conn, mem_ids[:12])

    card = ModeCard(
        name=mode,
        card_version=MODE_CARD_VERSION,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        role_summary=_summarize_role(mode, snippets),
        status=defaults["status"],
        introduced_at=introduced,
        chapter_valid_from=chapter_from,
        chapter_valid_to=chapter_to,
        triggers=_build_triggers(mode, snippets),
        voice_delta=dict(defaults["voice_delta"]),
        values_delta=dict(defaults["values_delta"]),
        exemplars=_build_exemplars(mem_ids, snippets),
        evidence=ModeEvidenceRef(
            memory_ids=mem_ids[:12],
            authored_text_ids=authored_ids[:12],
        ),
    )
    card.markdown = render_mode_card_markdown(card)
    return card


def build_mode_card(
    auth: AuthContext,
    mode: ModeName,
    *,
    digest_status: str = "draft",
    digest_rows: list[dict[str, Any]] | None = None,
    conn=None,
) -> ModeCard:
    if conn is not None:
        return _build_mode_card_inner(
            auth, mode, conn, digest_status=digest_status, digest_rows=digest_rows
        )
    with transaction() as inner:
        return _build_mode_card_inner(
            auth, mode, inner, digest_status=digest_status, digest_rows=digest_rows
        )


def build_all_mode_cards(
    auth: AuthContext,
    *,
    digest_status: str = "draft",
) -> list[ModeCard]:
    with transaction() as conn:
        rows = _load_digest_evidence(conn, auth.subject_id, digest_status=digest_status)
        if not rows:
            raise NotFoundError(
                f"need chapter digests with evidence (status={digest_status!r}) for mode cards (TR-2)"
            )
        return [
            build_mode_card(auth, mode, digest_status=digest_status, digest_rows=rows, conn=conn)
            for mode in CANONICAL_MODES
        ]


def validate_mode_card(card: ModeCard) -> None:
    if card.name not in CANONICAL_MODES:
        raise ValidationError(f"unknown mode {card.name!r}")
    if card.name == "Mediator" and card.status != "aspirational":
        raise ValidationError("Mediator must stay aspirational in the bundled fictional defaults")
    if not card.manual_only:
        raise ValidationError("mode cards are manual-only (no auto router)")
    evidence_count = len(set(card.evidence.memory_ids)) + len(set(card.evidence.authored_text_ids))
    if evidence_count < _MIN_EVIDENCE:
        raise ValidationError(
            f"mode {card.name} needs ≥{_MIN_EVIDENCE} evidence ids",
            details={"evidence_count": evidence_count},
        )
    if not card.role_summary.strip():
        raise ValidationError(f"mode {card.name} missing role_summary")


def render_mode_card_markdown(card: ModeCard) -> str:
    ev_m = ", ".join(f"`{m}`" for m in card.evidence.memory_ids[:4])
    ev_a = ", ".join(f"`{a}`" for a in card.evidence.authored_text_ids[:3])
    lines = [
        f"## {card.name} (proposed mode card)",
        "",
        f"Status: **{card.status}** · introduced ~{card.introduced_at or '?'} · manual `/mode` only.",
        "",
        card.role_summary,
        "",
        "### Triggers (documentation — not auto-routing)",
    ]
    for trig in card.triggers:
        lines.append(f"- {trig}")
    lines.extend(
        [
            "",
            "### Voice delta",
            f"- {json.dumps(card.voice_delta, ensure_ascii=False)}",
            "",
            "### Values delta",
            f"- {json.dumps(card.values_delta, ensure_ascii=False)}",
            "",
            "### Exemplars",
        ]
    )
    for ex in card.exemplars[:4]:
        lines.append(f"- {ex.get('text', '')[:200]}")
    lines.extend(["", f"Evidence memories: {ev_m or '—'}", f"Evidence authored: {ev_a or '—'}", ""])
    return "\n".join(lines)


def render_all_mode_cards_markdown(cards: list[ModeCard]) -> str:
    header = [
        "# Mode cards (proposed, TR-4)",
        "",
        "Four manual modes — evidence-linked, not active until approved via T-5.",
        "",
        f"Generated: `{cards[0].generated_at if cards else ''}`",
        "",
    ]
    body: list[str] = []
    for card in cards:
        body.append(card.markdown.rstrip())
        body.append("")
    return "\n".join(header + body).rstrip() + "\n"


def write_mode_cards_doc(cards: list[ModeCard], path: Path | None = None) -> Path:
    target = path or MODE_CARD_DOC
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_all_mode_cards_markdown(cards), encoding="utf-8")
    return target


def serialize_proposed_content(card: ModeCard) -> str:
    return json.dumps(card.to_dict(), ensure_ascii=False)


def _proposal_evidence_pairs(card: ModeCard) -> list[tuple[str, str, UUID | None, UUID | None]]:
    pairs: list[tuple[str, str, UUID | None, UUID | None]] = []
    for mid in card.evidence.memory_ids:
        pairs.append(("memory", mid, UUID(mid), None))
    for aid in card.evidence.authored_text_ids:
        pairs.append(("authored", aid, None, UUID(aid)))
    return pairs


def _upsert_draft_persona_mode(conn, auth: AuthContext, card: ModeCard) -> UUID:
    row = fetch_one(
        conn,
        "SELECT id, current_version_id FROM persona_modes WHERE subject_id = %s AND name = %s",
        (auth.subject_id, card.name),
    )
    mode_id = row["id"] if row else uuid7()
    if not row:
        intro = None
        if card.introduced_at:
            try:
                y, m = card.introduced_at.split("-", 1)
                intro = datetime(int(y), int(m), 1, tzinfo=timezone.utc)
            except (ValueError, TypeError):
                intro = _MODE_DEFAULTS[card.name]["era"]
        conn.execute(
            """
            INSERT INTO persona_modes (
              id, subject_id, name, role_summary, triggers, voice_delta, values_delta,
              status, introduced_at, chapter_valid_from, chapter_valid_to
            ) VALUES (
              %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
              %s, %s, %s, %s
            )
            """,
            (
                mode_id,
                auth.subject_id,
                card.name,
                card.role_summary,
                json.dumps(card.triggers),
                json.dumps(card.voice_delta),
                json.dumps(card.values_delta),
                card.status,
                intro,
                _parse_ts(card.chapter_valid_from),
                _parse_ts(card.chapter_valid_to),
            ),
        )
    ver_id = uuid7()
    conn.execute(
        """
        INSERT INTO persona_mode_versions (
          id, mode_id, version, role_summary, triggers, voice_delta, values_delta, status
        ) VALUES (
          %s, %s,
          COALESCE((SELECT MAX(version) FROM persona_mode_versions WHERE mode_id = %s), 0) + 1,
          %s, %s::jsonb, %s::jsonb, %s::jsonb, 'draft'
        )
        """,
        (
            ver_id,
            mode_id,
            mode_id,
            card.role_summary,
            json.dumps(card.triggers),
            json.dumps(card.voice_delta),
            json.dumps(card.values_delta),
        ),
    )
    conn.execute(
        "UPDATE persona_modes SET current_version_id = %s WHERE id = %s",
        (ver_id, mode_id),
    )
    for _kind, _ref, mem_id, auth_id in _proposal_evidence_pairs(card):
        conn.execute(
            """
            INSERT INTO persona_mode_evidence (
              id, mode_version_id, memory_version_id, authored_text_id, relationship
            ) VALUES (%s, %s, %s, %s, 'supports')
            """,
            (
                uuid7(),
                ver_id,
                _memory_version_for_memory(conn, mem_id) if mem_id else None,
                auth_id,
            ),
        )
    return mode_id


def propose_mode_cards(
    auth: AuthContext,
    *,
    dry_run: bool = False,
    notify: bool = True,
    digest_status: str = "draft",
    doc_path: Path | None = None,
) -> dict[str, Any]:
    cards = build_all_mode_cards(auth, digest_status=digest_status)
    for card in cards:
        validate_mode_card(card)
    write_mode_cards_doc(cards, doc_path)

    if dry_run:
        return {
            "dry_run": True,
            "card_version": MODE_CARD_VERSION,
            "modes": [c.name for c in cards],
            "doc_path": str(doc_path or MODE_CARD_DOC),
        }

    reason = "TR-4 mode card from chapter digests + self texts — manual modes only; approve via T-5."
    proposals: dict[str, str] = {}
    mode_ids: dict[str, str] = {}

    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        for card in cards:
            facet = facet_type_for_mode(card.name)
            pending = fetch_one(
                conn,
                """
                SELECT id FROM personality_proposals
                WHERE profile_id = %s AND facet_type = %s AND status = 'pending'
                LIMIT 1
                """,
                (profile_id, facet),
            )
            if pending:
                raise ConflictError(
                    f"pending {facet} proposal already exists",
                    details={"proposal_id": str(pending["id"]), "mode": card.name},
                )

        for card in cards:
            mode_ids[card.name] = str(_upsert_draft_persona_mode(conn, auth, card))
            facet = facet_type_for_mode(card.name)
            proposal_id = uuid7()
            conn.execute(
                """
                INSERT INTO personality_proposals (
                  id, profile_id, facet_type, proposed_content, reason,
                  confidence, proposed_by, origin_mode, status
                ) VALUES (
                  %s, %s, %s, %s, %s,
                  0.8, %s, %s, 'pending'
                )
                """,
                (
                    proposal_id,
                    profile_id,
                    facet,
                    serialize_proposed_content(card),
                    reason,
                    auth.actor_id,
                    card.name,
                ),
            )
            for _kind, _ref, mem_id, auth_id in _proposal_evidence_pairs(card):
                mem_ver = _memory_version_for_memory(conn, mem_id) if mem_id else None
                conn.execute(
                    """
                    INSERT INTO personality_proposal_evidence (
                      id, proposal_id, memory_version_id, authored_text_id, relationship
                    ) VALUES (%s, %s, %s, %s, 'supports')
                    """,
                    (uuid7(), proposal_id, mem_ver, auth_id),
                )
            proposals[card.name] = str(proposal_id)

    notified: dict[str, bool] = {}
    if notify:
        for card in cards:
            pid = UUID(proposals[card.name])
            notified[card.name] = notify_personality_proposal(
                proposal_id=pid,
                facet_type=facet_type_for_mode(card.name),
                proposed_content=card.markdown[:500],
                origin_mode=card.name,
                reason=reason,
            )

    return {
        "dry_run": False,
        "status": "pending",
        "proposals": proposals,
        "persona_mode_ids": mode_ids,
        "modes": [c.name for c in cards],
        "evidence_links": sum(
            len(set(c.evidence.memory_ids)) + len(set(c.evidence.authored_text_ids)) for c in cards
        ),
        "notified": notified,
        "doc_path": str(doc_path or MODE_CARD_DOC),
    }


def apply_approved_mode_proposal(auth: AuthContext, proposal_id: UUID, *, mode: ModeName) -> dict[str, Any]:
    """On approve: promote draft persona_mode_version to active (still manual-only at runtime)."""
    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        facet = facet_type_for_mode(mode)
        row = fetch_one(
            conn,
            """
            SELECT id, proposed_content, status
            FROM personality_proposals
            WHERE id = %s AND profile_id = %s AND facet_type = %s
            FOR UPDATE
            """,
            (proposal_id, profile_id, facet),
        )
        if row is None:
            raise NotFoundError(f"{mode} mode proposal not found")
        if row["status"] != "approved":
            raise ConflictError("proposal must be approved before apply", details={"status": row["status"]})

        mode_row = fetch_one(
            conn,
            "SELECT id, current_version_id FROM persona_modes WHERE subject_id = %s AND name = %s",
            (auth.subject_id, mode),
        )
        if not mode_row or not mode_row.get("current_version_id"):
            raise NotFoundError(f"persona_modes row missing for {mode}")

        cur_ver_id = mode_row["current_version_id"]
        conn.execute(
            """
            UPDATE persona_mode_versions
            SET status = 'active', approved_by = %s, approved_at = NOW()
            WHERE id = %s
            """,
            (auth.actor_id, cur_ver_id),
        )
        conn.execute(
            """
            UPDATE persona_mode_versions
            SET status = 'superseded'
            WHERE mode_id = %s AND id <> %s AND status = 'active'
            """,
            (mode_row["id"], cur_ver_id),
        )

    return {
        "mode": mode,
        "facet_type": facet,
        "proposal_id": str(proposal_id),
        "persona_mode_id": str(mode_row["id"]),
        "mode_version_id": str(cur_ver_id),
    }
