"""Deterministic consistency metrics (G-2): prompt-to-line, line-to-line, Q&A."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

ConsistencyKind = Literal["prompt_to_line", "line_to_line", "qa"]

_AFFECT_VERB = re.compile(
    r"\b(?:i|you|we|'m|'re)\s+"
    r"(?:(?:do\s+)?not\s+|never\s+|n't\s+)?"
    r"(love|like|prefer|enjoy|hate|dislike|avoid|want|need|crave|skip|choose)\s+"
    r"((?:minimal tools|side projects|remote work|minimal|simple|remote|local|infra|sleep|[a-z][\w-]{2,}))",
    re.I,
)
_STANCE = re.compile(
    r"\b(?:i|you|we)\s+(?:am|are|was|were|'m|'re)\s+"
    r"(?:(?:still|not|no longer|never)\s+)?"
    r"(burned\s+out|single|remote|local|kind|harder)\b",
    re.I,
)
_YES_NO = re.compile(
    r"\b(?:yes|no)\s*[,.]?\s*(?:i|you|we)\s+(?:still|never|always)\b",
    re.I,
)

_POSITIVE = frozenset(
    {"love", "like", "prefer", "enjoy", "want", "need", "crave", "choose"}
)
_NEGATIVE = frozenset({"hate", "dislike", "avoid", "skip"})

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class PolarityClaim:
    key: str
    polarity: int
    cue: str

    def conflicts_with(self, other: PolarityClaim) -> bool:
        return self.key == other.key and self.polarity != other.polarity


@dataclass
class ConsistencyCheckResult:
    kind: ConsistencyKind
    passed: bool
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "passed": self.passed, "flags": list(self.flags)}


def _normalize_key(phrase: str) -> str:
    return re.sub(r"\s+", " ", phrase.strip().lower())


def _verb_polarity(verb: str, *, negated: bool) -> int:
    v = verb.lower()
    if v in _POSITIVE:
        base = 1
    elif v in _NEGATIVE:
        base = -1
    else:
        base = 1
    return -base if negated else base


def extract_polarity_claims(text: str) -> list[PolarityClaim]:
    claims: list[PolarityClaim] = []
    blob = (text or "").strip()
    if not blob:
        return claims

    _chain = re.compile(
        r"\b(?:and|but)\s+(?:(?:do\s+)?not\s+|never\s+|n't\s+)?"
        r"(love|like|prefer|enjoy|hate|dislike|avoid|want|need|crave|skip|choose)\s+"
        r"((?:minimal tools|side projects|remote work|minimal|simple|remote|local|infra|sleep|[a-z][\w-]{2,}))",
        re.I,
    )
    for m in _AFFECT_VERB.finditer(blob):
        span = m.group(0).lower()
        negated = "not " in span or "n't " in span or "never " in span
        verb = m.group(1).lower()
        obj = _normalize_key(m.group(2))
        if len(obj) < 3:
            continue
        claims.append(
            PolarityClaim(
                key=obj,
                polarity=_verb_polarity(verb, negated=negated),
                cue=verb,
            )
        )

    for m in _chain.finditer(blob):
        span = m.group(0).lower()
        negated = "not " in span or "n't " in span or "never " in span
        verb = m.group(1).lower()
        obj = _normalize_key(m.group(2))
        if len(obj) < 3:
            continue
        claims.append(
            PolarityClaim(
                key=obj,
                polarity=_verb_polarity(verb, negated=negated),
                cue=verb,
            )
        )

    for m in _STANCE.finditer(blob):
        span = m.group(0).lower()
        neg = any(x in span for x in ("not ", "no longer", "never "))
        key = _normalize_key(m.group(1))
        claims.append(
            PolarityClaim(
                key=key,
                polarity=-1 if neg else 1,
                cue="stance",
            )
        )

    lower = blob.lower()
    if "yes, i still" in lower or "yes you still" in lower:
        claims.append(PolarityClaim(key="affirmation:still", polarity=1, cue="yes_still"))
    if "no, i never" in lower or "no you never" in lower:
        claims.append(PolarityClaim(key="affirmation:never", polarity=-1, cue="no_never"))
    if _YES_NO.search(blob):
        if "yes" in lower.split()[0:3]:
            claims.append(PolarityClaim(key="affirmation:yes", polarity=1, cue="yes_no"))
        if lower.lstrip().startswith("no"):
            claims.append(PolarityClaim(key="affirmation:yes", polarity=-1, cue="yes_no"))

    return claims


def flatten_lineage_text(
    prompt: str,
    memory_bundle: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = [(prompt or "").strip()]
    bundle = memory_bundle or {}
    for c in bundle.get("behavioral_constraints") or []:
        parts.append(str(c))
    p = bundle.get("approved_personality") or {}
    for facet in ("voice", "values", "constitution"):
        if p.get(facet):
            parts.append(str(p[facet]))
    for group in bundle.get("memory_chapters") or []:
        header = group.get("header")
        if header:
            parts.append(str(header))
    for mem in bundle.get("relevant_memories") or []:
        content = mem.get("content") or mem.get("text")
        if content:
            parts.append(str(content))
    return "\n".join(p for p in parts if p)


def split_reply_lines(reply: str) -> list[str]:
    text = (reply or "").strip()
    if not text:
        return []
    lines: list[str] = []
    for chunk in _SENTENCE_SPLIT.split(text):
        line = chunk.strip()
        if line:
            lines.append(line)
    return lines


def _ambiguous_keys(claims: list[PolarityClaim]) -> set[str]:
    by_key: dict[str, set[int]] = {}
    for c in claims:
        by_key.setdefault(c.key, set()).add(c.polarity)
    return {k for k, pols in by_key.items() if len(pols) > 1}


def _contradiction_flags(
    reply_claims: list[PolarityClaim],
    context_claims: list[PolarityClaim],
    *,
    prefix: str,
) -> list[str]:
    ambiguous = _ambiguous_keys(context_claims)
    flags: list[str] = []
    for rc in reply_claims:
        if rc.key in ambiguous:
            continue
        for cc in context_claims:
            if rc.conflicts_with(cc):
                flags.append(f"{prefix}:{rc.key}:reply_{rc.cue}_vs_context_{cc.cue}")
    return flags


def check_prompt_to_line_consistency(
    reply: str,
    *,
    prompt: str,
    memory_bundle: dict[str, Any] | None = None,
) -> ConsistencyCheckResult:
    """Reply lines must not contradict prompt + memory bundle lineage."""
    context = flatten_lineage_text(prompt, memory_bundle)
    ctx_claims = extract_polarity_claims(context)
    reply_claims = extract_polarity_claims(reply)
    flags = _contradiction_flags(reply_claims, ctx_claims, prefix="prompt_to_line")
    lines = split_reply_lines(reply)
    for i, line in enumerate(lines):
        line_claims = extract_polarity_claims(line)
        line_flags = _contradiction_flags(line_claims, ctx_claims, prefix=f"line_{i}_ptl")
        flags.extend(line_flags)
    return ConsistencyCheckResult(
        kind="prompt_to_line",
        passed=not flags,
        flags=flags,
    )


def check_line_to_line_consistency(reply: str) -> ConsistencyCheckResult:
    """Adjacent reply lines must not contradict each other."""
    lines = split_reply_lines(reply)
    flags: list[str] = []
    for i in range(len(lines) - 1):
        left = extract_polarity_claims(lines[i])
        right = extract_polarity_claims(lines[i + 1])
        for a in left:
            for b in right:
                if a.conflicts_with(b):
                    flags.append(f"line_to_line:{i}-{i+1}:{a.key}")
    return ConsistencyCheckResult(kind="line_to_line", passed=not flags, flags=flags)


def check_qa_consistency(
    reply: str,
    history: list[tuple[str, str]] | tuple[tuple[str, str], ...],
) -> ConsistencyCheckResult:
    """Reply must not contradict prior assistant turns in the same conversation."""
    prior_assistant = [
        content.strip()
        for role, content in history
        if role == "assistant" and (content or "").strip()
    ]
    if not prior_assistant:
        return ConsistencyCheckResult(kind="qa", passed=True)

    context_claims: list[PolarityClaim] = []
    for turn in prior_assistant:
        context_claims.extend(extract_polarity_claims(turn))

    reply_claims = extract_polarity_claims(reply)
    flags = _contradiction_flags(reply_claims, context_claims, prefix="qa")
    return ConsistencyCheckResult(kind="qa", passed=not flags, flags=flags)


def check_turn_consistency(
    reply: str,
    *,
    prompt: str,
    memory_bundle: dict[str, Any] | None = None,
    history: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
) -> dict[str, ConsistencyCheckResult]:
    return {
        "prompt_to_line": check_prompt_to_line_consistency(
            reply, prompt=prompt, memory_bundle=memory_bundle
        ),
        "line_to_line": check_line_to_line_consistency(reply),
        "qa": check_qa_consistency(reply, history),
    }


def summarize_consistency(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate per-scenario check dicts (each with ptl/l2l/qa results)."""
    kinds: tuple[ConsistencyKind, ...] = ("prompt_to_line", "line_to_line", "qa")
    counts = {k: {"passed": 0, "failed": 0} for k in kinds}
    for row in rows:
        checks = row.get("consistency") or {}
        for kind in kinds:
            raw = checks.get(kind)
            if not raw:
                continue
            if raw.get("passed"):
                counts[kind]["passed"] += 1
            else:
                counts[kind]["failed"] += 1
    total = len(rows)
    all_pass = sum(
        1
        for row in rows
        if all(
            (row.get("consistency") or {}).get(k, {}).get("passed", True)
            for k in kinds
            if (row.get("consistency") or {}).get(k) is not None
        )
    )
    return {
        "scenario_count": total,
        "turns_all_checks_passed": all_pass,
        "prompt_to_line_pass_rate": (
            counts["prompt_to_line"]["passed"] / total if total else None
        ),
        "line_to_line_pass_rate": (
            counts["line_to_line"]["passed"] / total if total else None
        ),
        "qa_pass_rate": counts["qa"]["passed"] / total if total else None,
        "failures_by_kind": {
            k: counts[k]["failed"] for k in kinds
        },
    }
