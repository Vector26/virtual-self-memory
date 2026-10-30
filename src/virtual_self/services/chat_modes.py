"""Manual /mode and /council for chat (TR-5) — explicit only, approved mode cards."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import NotFoundError, ValidationError
from virtual_self.persistence.db import fetch_all, fetch_one, transaction
from virtual_self.services.mode_cards import CANONICAL_MODES, ModeName

ModeCommandKind = Literal["set", "off", "show", "council"]

_MODE_ALIASES: dict[str, ModeName] = {
    "analyst": "Analyst",
    "guide": "Guide",
    "mediator": "Mediator",
    "explorer": "Explorer",
    "the explorer": "Explorer",
}

_MODE_CMD = re.compile(
    r"^/mode(?:\s+(?P<arg>[^\s—\-]+))?(?:\s*[—\-]\s*)?(?P<rest>.*)$",
    re.I | re.DOTALL,
)
_COUNCIL_CMD = re.compile(r"^/council(?:\s*[—\-]\s*)?(?P<rest>.*)$", re.I | re.DOTALL)


@dataclass(frozen=True)
class ModeCommand:
    kind: ModeCommandKind
    mode: ModeName | None = None
    user_query: str = ""


@dataclass(frozen=True)
class SessionModeState:
    active_mode: ModeName | None = None

    def apply(self, cmd: ModeCommand) -> SessionModeState:
        if cmd.kind == "off":
            return SessionModeState(None)
        if cmd.kind == "set" and cmd.mode:
            return SessionModeState(cmd.mode)
        return self


def resolve_mode_name(token: str) -> ModeName | None:
    key = token.strip().lower()
    if not key:
        return None
    if key in _MODE_ALIASES:
        return _MODE_ALIASES[key]
    for canonical in CANONICAL_MODES:
        if canonical.lower() == key:
            return canonical
    return None


def parse_mode_command(user_text: str) -> ModeCommand | None:
    """Parse explicit /mode or /council only (no natural-language activation)."""
    raw = user_text.strip()
    if not raw.startswith("/"):
        return None

    council = _COUNCIL_CMD.match(raw)
    if council:
        rest = (council.group("rest") or "").strip()
        return ModeCommand("council", user_query=rest)

    mode_match = _MODE_CMD.match(raw)
    if not mode_match:
        return None

    arg = (mode_match.group("arg") or "").strip().lower()
    rest = (mode_match.group("rest") or "").strip()

    if not arg:
        return ModeCommand("show")
    if arg == "off":
        return ModeCommand("off", user_query=rest)
    resolved = resolve_mode_name(arg)
    if resolved is None:
        raise ValidationError(
            f"unknown mode {arg!r} — use analyst, guide, mediator, or explorer"
        )
    return ModeCommand("set", mode=resolved, user_query=rest)


def mode_command_ack(cmd: ModeCommand, *, previous: ModeName | None) -> str:
    if cmd.kind == "show":
        if previous:
            return f"Active mode: {previous} (integrated present self when off)."
        return "No mode active — integrated present self (voice card only)."
    if cmd.kind == "off":
        return "Mode off — back to integrated present self."
    if cmd.kind == "set" and cmd.mode:
        return f"Mode set to {cmd.mode} until /mode off or session end."
    if cmd.kind == "council":
        return "Council format — each approved mode speaks, then present self sums up."
    return ""


def _row_to_composer_block(row: dict[str, Any]) -> str:
    name = row["name"]
    triggers = row.get("triggers") or []
    if isinstance(triggers, str):
        triggers = json.loads(triggers)
    voice = row.get("voice_delta") or {}
    if isinstance(voice, str):
        voice = json.loads(voice)
    values = row.get("values_delta") or {}
    if isinstance(values, str):
        values = json.loads(values)
    lifecycle = row.get("mode_lifecycle") or row.get("status") or "active"
    lines = [
        f"## Active mode: {name}",
        "",
        f"Lifecycle: {lifecycle} (aspirational modes stay labeled; do not pretend they are fully achieved).",
        "",
        row.get("role_summary") or "",
        "",
        "### Voice delta (apply for this turn)",
        json.dumps(voice, ensure_ascii=False),
        "",
        "### Values delta",
        json.dumps(values, ensure_ascii=False),
        "",
        "### Triggers (documentation only — never auto-switch on these)",
    ]
    for t in triggers[:8]:
        lines.append(f"- {t}")
    lines.extend(
        [
            "",
            "Stay in this mode's voice until the user sends /mode off or the session ends.",
            "Do not mention that you are a mode unless it helps clarity.",
        ]
    )
    return "\n".join(lines)


def load_approved_mode_block(auth: AuthContext, mode: ModeName) -> str:
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT m.name, m.status AS mode_lifecycle, m.role_summary,
                   v.triggers, v.voice_delta, v.values_delta, v.status AS version_status
            FROM persona_modes m
            JOIN persona_mode_versions v ON v.id = m.current_version_id
            WHERE m.subject_id = %s AND m.name = %s AND v.status = 'active'
            """,
            (auth.subject_id, mode),
        )
    if row is None:
        raise NotFoundError(
            f"mode {mode} has no approved card yet — approve the TR-4 proposal (T-5) first"
        )
    return _row_to_composer_block(row)


def list_approved_mode_names(auth: AuthContext) -> list[ModeName]:
    with transaction() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT m.name
            FROM persona_modes m
            JOIN persona_mode_versions v ON v.id = m.current_version_id
            WHERE m.subject_id = %s AND v.status = 'active'
            ORDER BY m.name
            """,
            (auth.subject_id,),
        )
    names: list[ModeName] = []
    for row in rows:
        n = row["name"]
        if n in CANONICAL_MODES:
            names.append(n)  # type: ignore[arg-type]
    return names


def council_composer_block(auth: AuthContext) -> str:
    approved = list_approved_mode_names(auth)
    if not approved:
        raise NotFoundError(
            "no approved mode cards — approve TR-4 proposals before /council"
        )
    blocks = [load_approved_mode_block(auth, m) for m in approved]
    mode_list = ", ".join(approved)
    header = (
        "# Council turn (manual /council)\n\n"
        f"Speak briefly as each of: {mode_list}. Use a labeled line per mode "
        "(e.g. **[Analyst]**, **[Guide]**, …). "
        "Then **[Present self]** — integrated summary in the default voice (no mode delta).\n\n"
        "Approved mode cards for this council:\n"
    )
    return header + "\n\n---\n\n".join(blocks)


def composer_mode_section(
    auth: AuthContext,
    *,
    session_mode: ModeName | None,
    council: bool,
) -> str:
    if council:
        return council_composer_block(auth)
    if session_mode:
        return load_approved_mode_block(auth, session_mode)
    return ""
