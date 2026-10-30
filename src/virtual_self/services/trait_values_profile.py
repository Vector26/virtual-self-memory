"""Trait + values profile from chapter digests + IPIP ground truth (TR-3 / plan §P4 L2–L3)."""

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
from virtual_self.providers.agent_chat import chat_json_for_task
from virtual_self.services.chapter_proposal_notify import notify_personality_proposal

PROFILE_VERSION = 1
TRAITS_VALUES_DOC = Path(__file__).resolve().parents[3] / "docs" / "TRAITS_VALUES_PROFILE.md"
IPIP_PRIVATE_PATH = Path(__file__).resolve().parents[3] / ".state" / "persona" / "ipip_neo_private.json"

BIG_FIVE = (
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
)

IPIP_DOMAIN_FACET_SLICES: dict[str, tuple[int, int]] = {
    "neuroticism": (0, 6),
    "extraversion": (6, 12),
    "openness": (12, 18),
    "agreeableness": (18, 24),
    "conscientiousness": (24, 30),
}

LAYER_TRAIT_WEIGHTS: dict[str, tuple[tuple[str, float], ...]] = {
    "values": (("agreeableness", 0.5), ("conscientiousness", 0.5)),
    "decision_style": (("conscientiousness", 0.55), ("openness", 0.45)),
    "voice": (("extraversion", 0.45), ("neuroticism", 0.55)),
    "modes": (("extraversion", 0.5), ("openness", 0.5)),
}

FacetProposalKind = Literal["traits", "values", "interaction_preferences"]

_TRAIT_SYNTH_SYSTEM = """You refine a draft Big Five + values profile using ONLY the supplied digest excerpts.
Rules:
- Every trait claim must cite memory_ids from the bundle.
- Do not invent clinical labels or numeric psychometric scores.
- Preserve aspiration vs actual stance labels.
- Output JSON:
{
  "traits": {
    "<openness|conscientiousness|extraversion|agreeableness|neuroticism>": {
      "mean": 0.0-1.0,
      "range": [low, high],
      "summary": "one sentence",
      "memory_ids": ["uuid", ...]
    }
  },
  "values": [{"label":"...", "weight":0.0-1.0, "memory_ids":["uuid"], "stance":"actual|aspiration"}],
  "conflict_rules": ["when ... prefer ..."],
  "interaction_policy": ["bullet", ...]
}"""


@dataclass
class TraitClaim:
    trait: str
    summary: str
    mean: float
    range_low: float
    range_high: float
    memory_ids: list[str] = field(default_factory=list)
    stance: str = "actual"


@dataclass
class ValueClaim:
    label: str
    weight: float
    memory_ids: list[str] = field(default_factory=list)
    stance: str = "actual"


@dataclass
class TraitValuesProfile:
    profile_version: int
    generated_at: str
    digest_count: int
    traits: list[TraitClaim]
    values: list[ValueClaim]
    conflict_rules: list[str]
    interaction_policy: list[str]
    ipip_calibration_note: str
    journaling_bias_notes: list[str]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_version": self.profile_version,
            "generated_at": self.generated_at,
            "digest_count": self.digest_count,
            "traits": [asdict(t) for t in self.traits],
            "values": [asdict(v) for v in self.values],
            "conflict_rules": self.conflict_rules,
            "interaction_policy": self.interaction_policy,
            "ipip_calibration_note": self.ipip_calibration_note,
            "journaling_bias_notes": self.journaling_bias_notes,
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


def _ipip_domain_bands(path: Path | None = None) -> dict[str, str]:
    """Qualitative bands only — numeric facets never leave this function."""
    target = path or IPIP_PRIVATE_PATH
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    facets = payload.get("facet_order_30")
    if not isinstance(facets, list) or len(facets) < 30:
        return {}
    bands: dict[str, str] = {}
    for domain, (lo, hi) in IPIP_DOMAIN_FACET_SLICES.items():
        chunk = [float(x) for x in facets[lo:hi]]
        avg = sum(chunk) / len(chunk)
        if avg >= 3.35:
            bands[domain] = "elevated"
        elif avg <= 2.45:
            bands[domain] = "low"
        else:
            bands[domain] = "moderate"
    return bands


def _ipip_calibration_note(bands: dict[str, str]) -> str:
    if not bands:
        return "IPIP private file absent — traits estimated from chapter digests only."
    parts = [f"{domain} band: {band}" for domain, band in sorted(bands.items())]
    return (
        "IPIP ground truth used for qualitative calibration only (no scores exported). "
        + "; ".join(parts)
    )


def qualitative_band_for_mean(mean: float) -> str:
    """Map internal 0–1 trait mean to a qualitative band (TR-7 reports — never raw means)."""
    if mean >= 0.58:
        return "elevated"
    if mean <= 0.42:
        return "low"
    return "moderate"


def load_ipip_qualitative_bands(path: Path | None = None) -> dict[str, str]:
    """IPIP ground-truth bands only; numeric facets never leave the loader."""
    return _ipip_domain_bands(path)


_BAND_ORDER = ("low", "moderate", "elevated")


def bands_within_tolerance(reference: str, candidate: str) -> bool:
    """True when candidate matches reference or differs by at most one band (TR-7 gate)."""
    if reference == candidate:
        return True
    try:
        ri = _BAND_ORDER.index(reference)
        ci = _BAND_ORDER.index(candidate)
    except ValueError:
        return False
    return abs(ri - ci) <= 1


def _nudge_mean(base: float, band: str | None) -> float:
    mean = max(0.05, min(0.95, base))
    if band == "elevated":
        mean = max(mean, 0.58)
    elif band == "low":
        mean = min(mean, 0.42)
    return round(mean, 3)


def _stance_weight(stance: str | None) -> float:
    if stance == "aspiration":
        return 0.45
    if stance == "mode_difference":
        return 0.35
    return 1.0


def _load_digest_evidence(conn, subject_id: UUID, *, digest_status: str = "draft") -> list[dict[str, Any]]:
    digests = fetch_all(
        conn,
        """
        SELECT id, chapter_id, journaling_bias_note
        FROM life_chapter_guided_digests
        WHERE subject_id = %s AND status = %s
        ORDER BY created_at
        """,
        (subject_id, digest_status),
    )
    if not digests:
        return []
    out: list[dict[str, Any]] = []
    for dg in digests:
        items = fetch_all(
            conn,
            """
            SELECT i.id, i.step, i.layer, i.text, i.stance, i.question_key,
                   array_agg(e.memory_id::text) AS memory_ids
            FROM life_chapter_guided_digest_items i
            LEFT JOIN life_chapter_guided_digest_evidence e ON e.item_id = i.id
            WHERE i.digest_id = %s
            GROUP BY i.id, i.step, i.layer, i.text, i.stance, i.question_key, i.sort_order
            ORDER BY i.sort_order
            """,
            (dg["id"],),
        )
        for it in items:
            mids = [m for m in (it.get("memory_ids") or []) if m]
            if not mids:
                continue
            out.append(
                {
                    "digest_id": str(dg["id"]),
                    "journaling_bias_note": (dg.get("journaling_bias_note") or "").strip(),
                    "step": it["step"],
                    "layer": it["layer"],
                    "text": str(it["text"] or "").strip(),
                    "stance": it.get("stance") or "actual",
                    "question_key": it.get("question_key"),
                    "memory_ids": mids,
                }
            )
    return out


def _memory_version_for_memory(conn, memory_id: UUID) -> UUID | None:
    row = fetch_one(
        conn,
        "SELECT current_version_id FROM memories WHERE id = %s",
        (memory_id,),
    )
    if row and row.get("current_version_id"):
        return row["current_version_id"]
    row = fetch_one(
        conn,
        """
        SELECT id FROM memory_versions
        WHERE memory_id = %s AND status = 'active'
        ORDER BY version DESC
        LIMIT 1
        """,
        (memory_id,),
    )
    return row["id"] if row else None


def _collect_trait_buckets(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {
        t: {"scores": [], "memory_ids": set(), "summaries": []} for t in BIG_FIVE
    }
    for row in rows:
        if row["step"] != "profile_sentence":
            continue
        layer = str(row.get("layer") or "")
        weights = LAYER_TRAIT_WEIGHTS.get(layer)
        if not weights:
            continue
        text = row["text"]
        stance_w = _stance_weight(row.get("stance"))
        for trait, w in weights:
            buckets[trait]["scores"].append(0.52 * stance_w * w + 0.08)
            buckets[trait]["memory_ids"].update(row["memory_ids"])
            if text and len(buckets[trait]["summaries"]) < 3:
                buckets[trait]["summaries"].append(text)
    return buckets


def _build_conflict_rules(rows: list[dict[str, Any]]) -> list[str]:
    rules: list[str] = []
    for row in rows:
        if row.get("stance") != "mode_difference":
            continue
        text = re.sub(r"\s+", " ", row["text"]).strip()
        if len(text) < 20:
            continue
        rules.append(f"When modes diverge: {text[:220]}")
    seen: set[str] = set()
    uniq: list[str] = []
    for r in rules:
        key = r.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq[:8]


def _build_values(rows: list[dict[str, Any]]) -> list[ValueClaim]:
    values: list[ValueClaim] = []
    for row in rows:
        if row["step"] != "profile_sentence" or row.get("layer") != "values":
            continue
        label = re.sub(r"\s+", " ", row["text"]).strip()
        if not label:
            continue
        stance = str(row.get("stance") or "actual")
        weight = round(0.55 * _stance_weight(stance), 2)
        values.append(
            ValueClaim(
                label=label,
                weight=weight,
                memory_ids=list(row["memory_ids"]),
                stance=stance,
            )
        )
    return values


def _build_interaction_policy(rows: list[dict[str, Any]]) -> list[str]:
    bullets: list[str] = []
    for row in rows:
        if row["step"] != "profile_sentence":
            continue
        if row.get("layer") not in ("decision_style", "voice"):
            continue
        text = re.sub(r"\s+", " ", row["text"]).strip()
        if not text:
            continue
        bullets.append(text[:240])
    if not bullets:
        bullets.append("Prefer evidence-backed answers; cite uncertainty when memories are thin.")
    bullets.append("Push back when a request conflicts with stated non-negotiables (values layer).")
    bullets.append("Keep replies direct; no performative empathy or filler openers.")
    seen: set[str] = set()
    out: list[str] = []
    for b in bullets:
        k = b.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(b)
    return out[:10]


def _render_markdown(profile: TraitValuesProfile) -> str:
    lines = [
        "# Traits & values profile (draft for approval)",
        "",
        f"_Generated {profile.generated_at} from {profile.digest_count} chapter digests._",
        "",
        "## Big Five (mean + range)",
    ]
    for t in profile.traits:
        ev = ", ".join(f"`{m}`" for m in t.memory_ids[:3])
        lines.append(
            f"- **{t.trait}**: mean {t.mean:.2f}, range [{t.range_low:.2f}, {t.range_high:.2f}] — "
            f"{t.summary} _(evidence: {ev})_"
        )
    lines.extend(["", "## Weighted values"])
    for v in profile.values:
        ev = ", ".join(f"`{m}`" for m in v.memory_ids[:2])
        lines.append(f"- {v.label} (weight {v.weight:.2f}, {v.stance}) _(evidence: {ev})_")
    lines.extend(["", "## Conflict rules"])
    for rule in profile.conflict_rules:
        lines.append(f"- {rule}")
    lines.extend(["", "## Interaction policy"])
    for rule in profile.interaction_policy:
        lines.append(f"- {rule}")
    if profile.journaling_bias_notes:
        lines.extend(["", "## Journaling bias notes"])
        for note in profile.journaling_bias_notes:
            lines.append(f"- {note}")
    lines.extend(["", "## Calibration", profile.ipip_calibration_note])
    return "\n".join(lines).rstrip() + "\n"


def _maybe_llm_refine(profile: TraitValuesProfile, rows: list[dict[str, Any]]) -> TraitValuesProfile | None:
    bundle = json.dumps(rows[:80], ensure_ascii=False, default=str)
    raw = chat_json_for_task(_TRAIT_SYNTH_SYSTEM, bundle[:12000], temperature=0.2)
    if not raw or not isinstance(raw, dict):
        return None
    allowed = {mid for row in rows for mid in row["memory_ids"]}
    traits_raw = raw.get("traits") or {}
    traits: list[TraitClaim] = []
    for name in BIG_FIVE:
        block = traits_raw.get(name)
        if not isinstance(block, dict):
            continue
        mids = [m for m in (block.get("memory_ids") or []) if str(m) in allowed]
        if not mids:
            continue
        mean = float(block.get("mean", 0.5))
        rng = block.get("range") or [mean - 0.1, mean + 0.1]
        traits.append(
            TraitClaim(
                trait=name,
                summary=str(block.get("summary") or "").strip() or name,
                mean=max(0.0, min(1.0, mean)),
                range_low=max(0.0, min(1.0, float(rng[0]))),
                range_high=max(0.0, min(1.0, float(rng[1]))),
                memory_ids=mids,
            )
        )
    if len(traits) < 5:
        return None
    values: list[ValueClaim] = []
    for row in raw.get("values") or []:
        if not isinstance(row, dict):
            continue
        mids = [m for m in (row.get("memory_ids") or []) if str(m) in allowed]
        label = str(row.get("label") or "").strip()
        if not label or not mids:
            continue
        values.append(
            ValueClaim(
                label=label,
                weight=float(row.get("weight") or 0.5),
                memory_ids=mids,
                stance=str(row.get("stance") or "actual"),
            )
        )
    conflict = [str(x).strip() for x in (raw.get("conflict_rules") or []) if str(x).strip()]
    interaction = [str(x).strip() for x in (raw.get("interaction_policy") or []) if str(x).strip()]
    updated = TraitValuesProfile(
        profile_version=profile.profile_version,
        generated_at=profile.generated_at,
        digest_count=profile.digest_count,
        traits=traits,
        values=values or profile.values,
        conflict_rules=conflict or profile.conflict_rules,
        interaction_policy=interaction or profile.interaction_policy,
        ipip_calibration_note=profile.ipip_calibration_note,
        journaling_bias_notes=profile.journaling_bias_notes,
    )
    updated.markdown = _render_markdown(updated)
    return updated


def build_trait_values_profile(
    auth: AuthContext,
    *,
    digest_status: str = "draft",
    use_llm: bool = False,
    ipip_path: Path | None = None,
    min_digests: int = 1,
) -> TraitValuesProfile:
    bands = _ipip_domain_bands(ipip_path)
    with transaction() as conn:
        digest_rows = fetch_all(
            conn,
            """
            SELECT id FROM life_chapter_guided_digests
            WHERE subject_id = %s AND status = %s
            """,
            (auth.subject_id, digest_status),
        )
        digest_count = len(digest_rows)
        if digest_count < min_digests:
            raise NotFoundError(
                f"need at least {min_digests} chapter digests with status={digest_status!r} (TR-2)"
            )
        rows = _load_digest_evidence(conn, auth.subject_id, digest_status=digest_status)
    if not rows:
        raise NotFoundError("chapter digests have no evidence-linked items")

    buckets = _collect_trait_buckets(rows)
    traits: list[TraitClaim] = []
    for name in BIG_FIVE:
        b = buckets[name]
        if b["scores"]:
            base = min(0.9, 0.35 + sum(b["scores"]))
        else:
            base = 0.5
        mean = _nudge_mean(base, bands.get(name))
        spread = 0.12 if b["scores"] else 0.18
        summaries = b["summaries"]
        summary = summaries[0] if summaries else f"Insufficient chapter evidence for {name}; prior kept neutral."
        traits.append(
            TraitClaim(
                trait=name,
                summary=summary,
                mean=mean,
                range_low=round(max(0.0, mean - spread), 3),
                range_high=round(min(1.0, mean + spread), 3),
                memory_ids=sorted(b["memory_ids"])[:6],
                stance="actual",
            )
        )

    values = _build_values(rows)
    if not values:
        raise ValidationError("no values-layer profile sentences in digests")

    bias_notes = sorted(
        {r["journaling_bias_note"] for r in rows if r.get("journaling_bias_note")}
    )
    profile = TraitValuesProfile(
        profile_version=PROFILE_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        digest_count=digest_count,
        traits=traits,
        values=values,
        conflict_rules=_build_conflict_rules(rows),
        interaction_policy=_build_interaction_policy(rows),
        ipip_calibration_note=_ipip_calibration_note(bands),
        journaling_bias_notes=bias_notes,
    )
    profile.markdown = _render_markdown(profile)
    if use_llm:
        refined = _maybe_llm_refine(profile, rows)
        if refined is not None:
            profile = refined
    return profile


def validate_trait_values_profile(profile: TraitValuesProfile) -> None:
    if len(profile.traits) != 5:
        raise ValidationError("traits profile must include all Big Five dimensions")
    for t in profile.traits:
        if not t.memory_ids:
            raise ValidationError(f"trait {t.trait} missing evidence memory ids")
        if t.range_low > t.mean or t.mean > t.range_high:
            raise ValidationError(f"trait {t.trait} has invalid range")
    if not profile.values:
        raise ValidationError("values list empty")
    for v in profile.values:
        if not v.memory_ids:
            raise ValidationError("value claim missing evidence")


def write_traits_values_doc(profile: TraitValuesProfile, path: Path | None = None) -> Path:
    target = path or TRAITS_VALUES_DOC
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(profile.markdown, encoding="utf-8")
    return target


def _serialize_traits_proposal(profile: TraitValuesProfile) -> str:
    payload = profile.to_dict()
    return json.dumps(payload, ensure_ascii=False)


def _serialize_values_proposal(profile: TraitValuesProfile) -> str:
    lines = ["# Values (weighted)", ""]
    for v in profile.values:
        lines.append(f"- {v.label} (weight {v.weight:.2f}, stance={v.stance})")
    if profile.conflict_rules:
        lines.extend(["", "## Conflict rules"])
        for rule in profile.conflict_rules:
            lines.append(f"- {rule}")
    return "\n".join(lines).strip() + "\n"


def _serialize_interaction_proposal(profile: TraitValuesProfile) -> str:
    return "\n".join(f"- {b}" for b in profile.interaction_policy).strip() + "\n"


def _collect_evidence_pairs(profile: TraitValuesProfile) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for t in profile.traits:
        for mid in t.memory_ids:
            if mid in seen:
                continue
            seen.add(mid)
            pairs.append((mid, "supports"))
    for v in profile.values:
        for mid in v.memory_ids:
            if mid in seen:
                continue
            seen.add(mid)
            pairs.append((mid, "supports"))
    return pairs


def _insert_proposal(
    conn,
    *,
    profile_id: UUID,
    facet_type: FacetProposalKind,
    proposed_content: str,
    reason: str,
    actor_id: UUID,
    evidence_memory_ids: list[str],
) -> UUID:
    pending = fetch_one(
        conn,
        """
        SELECT id FROM personality_proposals
        WHERE profile_id = %s AND facet_type = %s AND status = 'pending'
        LIMIT 1
        """,
        (profile_id, facet_type),
    )
    if pending:
        raise ConflictError(
            f"pending {facet_type} proposal already exists",
            details={"proposal_id": str(pending["id"])},
        )
    proposal_id = uuid7()
    conn.execute(
        """
        INSERT INTO personality_proposals (
          id, profile_id, facet_type, proposed_content, reason,
          confidence, proposed_by, origin_mode, status
        ) VALUES (
          %s, %s, %s, %s, %s,
          0.82, %s, 'Self', 'pending'
        )
        """,
        (proposal_id, profile_id, facet_type, proposed_content, reason, actor_id),
    )
    for memory_id in evidence_memory_ids:
        ver_id = _memory_version_for_memory(conn, UUID(memory_id))
        if ver_id is None:
            continue
        conn.execute(
            """
            INSERT INTO personality_proposal_evidence (
              id, proposal_id, memory_version_id, relationship
            ) VALUES (%s, %s, %s, %s)
            """,
            (uuid7(), proposal_id, ver_id, "supports"),
        )
    return proposal_id


def propose_trait_values_profile(
    auth: AuthContext,
    *,
    dry_run: bool = False,
    notify: bool = True,
    use_llm: bool = False,
    digest_status: str = "draft",
    doc_path: Path | None = None,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    profile = build_trait_values_profile(
        auth,
        digest_status=digest_status,
        use_llm=use_llm,
    )
    validate_trait_values_profile(profile)
    write_traits_values_doc(profile, doc_path)

    if audit_path is not None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")

    if dry_run:
        return {
            "dry_run": True,
            "profile_version": profile.profile_version,
            "digest_count": profile.digest_count,
            "trait_count": len(profile.traits),
            "value_count": len(profile.values),
            "doc_path": str(doc_path or TRAITS_VALUES_DOC),
        }

    reason = "TR-3 trait/values profile from chapter digests + IPIP qualitative calibration."
    evidence_pairs = _collect_evidence_pairs(profile)
    memory_ids_for_evidence = [mid for mid, _rel in evidence_pairs]

    proposals: dict[str, str] = {}
    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        traits_id = _insert_proposal(
            conn,
            profile_id=profile_id,
            facet_type="traits",
            proposed_content=_serialize_traits_proposal(profile),
            reason=reason,
            actor_id=auth.actor_id,
            evidence_memory_ids=memory_ids_for_evidence,
        )
        values_id = _insert_proposal(
            conn,
            profile_id=profile_id,
            facet_type="values",
            proposed_content=_serialize_values_proposal(profile),
            reason=reason,
            actor_id=auth.actor_id,
            evidence_memory_ids=memory_ids_for_evidence,
        )
        interaction_id = _insert_proposal(
            conn,
            profile_id=profile_id,
            facet_type="interaction_preferences",
            proposed_content=_serialize_interaction_proposal(profile),
            reason=reason,
            actor_id=auth.actor_id,
            evidence_memory_ids=memory_ids_for_evidence,
        )
        proposals = {
            "traits": str(traits_id),
            "values": str(values_id),
            "interaction_preferences": str(interaction_id),
        }

    notified: dict[str, bool] = {}
    if notify:
        for facet_type, pid in proposals.items():
            snippet = profile.markdown[:400] if facet_type == "traits" else _serialize_values_proposal(profile)[:400]
            notified[facet_type] = notify_personality_proposal(
                proposal_id=UUID(pid),
                facet_type=facet_type,
                proposed_content=snippet,
                origin_mode="Self",
                reason=reason,
            )

    return {
        "dry_run": False,
        "status": "pending",
        "proposals": proposals,
        "digest_count": profile.digest_count,
        "evidence_links": len(evidence_pairs),
        "notified": notified,
        "doc_path": str(doc_path or TRAITS_VALUES_DOC),
    }


def _facet_content_from_proposal(facet_type: str, proposed_content: str) -> str:
    text = (proposed_content or "").strip()
    if facet_type == "traits" and text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                md = str(payload.get("markdown") or "").strip()
                if md:
                    return md
                return json.dumps(
                    {k: payload[k] for k in ("traits", "values", "conflict_rules") if k in payload},
                    ensure_ascii=False,
                    indent=2,
                )
        except json.JSONDecodeError:
            pass
    return text


def _ensure_facet(conn, profile_id: UUID, facet_type: str, mutability: str = "approval_required") -> UUID:
    row = fetch_one(
        conn,
        "SELECT id FROM personality_facets WHERE profile_id = %s AND facet_type = %s",
        (profile_id, facet_type),
    )
    if row:
        return row["id"]
    facet_id = uuid7()
    conn.execute(
        """
        INSERT INTO personality_facets (id, profile_id, facet_type, mutability)
        VALUES (%s, %s, %s, %s)
        """,
        (facet_id, profile_id, facet_type, mutability),
    )
    return facet_id


def apply_approved_facet_proposal(
    auth: AuthContext,
    proposal_id: UUID,
    *,
    expected_facet_type: str,
    priority: int,
) -> dict[str, Any]:
    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        row = fetch_one(
            conn,
            """
            SELECT id, facet_type, proposed_content, status
            FROM personality_proposals
            WHERE id = %s AND profile_id = %s AND facet_type = %s
            FOR UPDATE
            """,
            (proposal_id, profile_id, expected_facet_type),
        )
        if row is None:
            raise NotFoundError(f"{expected_facet_type} proposal not found")
        if row["status"] != "approved":
            raise ConflictError("proposal must be approved before apply", details={"status": row["status"]})

        facet_content = _facet_content_from_proposal(expected_facet_type, row["proposed_content"])
        if not facet_content.strip():
            raise ValidationError(f"{expected_facet_type} proposal has empty content")

        facet_id = _ensure_facet(conn, profile_id, expected_facet_type)
        facet_row = fetch_one(
            conn,
            "SELECT id, current_version_id FROM personality_facets WHERE id = %s",
            (facet_id,),
        )
        cur_ver = None
        if facet_row and facet_row.get("current_version_id"):
            cur_ver = fetch_one(
                conn,
                "SELECT version FROM personality_facet_versions WHERE id = %s",
                (facet_row["current_version_id"],),
            )
        next_ver = (cur_ver["version"] if cur_ver else 0) + 1
        if facet_row and facet_row.get("current_version_id"):
            conn.execute(
                "UPDATE personality_facet_versions SET status = 'superseded' WHERE id = %s",
                (facet_row["current_version_id"],),
            )
        ver_id = uuid7()
        conn.execute(
            """
            INSERT INTO personality_facet_versions (
              id, facet_id, version, content, priority, confidence, status, approved_by, approved_at
            ) VALUES (%s, %s, %s, %s, %s, 0.88, 'active', %s, NOW())
            """,
            (ver_id, facet_id, next_ver, facet_content, priority, auth.actor_id),
        )
        conn.execute(
            "UPDATE personality_facets SET current_version_id = %s WHERE id = %s",
            (ver_id, facet_id),
        )

        evidence_rows = fetch_all(
            conn,
            """
            SELECT memory_version_id, relationship
            FROM personality_proposal_evidence
            WHERE proposal_id = %s AND memory_version_id IS NOT NULL
            """,
            (proposal_id,),
        )
        for ev in evidence_rows:
            conn.execute(
                """
                INSERT INTO personality_facet_evidence (
                  id, facet_version_id, memory_version_id, relationship
                ) VALUES (%s, %s, %s, %s)
                """,
                (uuid7(), ver_id, ev["memory_version_id"], ev["relationship"]),
            )

    return {
        "facet_type": expected_facet_type,
        "facet_version": next_ver,
        "proposal_id": str(proposal_id),
        "content_chars": len(facet_content),
    }
