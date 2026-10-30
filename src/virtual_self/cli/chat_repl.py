from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.domain.errors import NotFoundError, ValidationError
from virtual_self.providers.agent_chat import _openai_available, preflight_llm
from virtual_self.providers.ollama_runtime import ensure_chat_model
from virtual_self.cli.remember import parse_remember_request
from virtual_self.domain.types import Trust
from virtual_self.services.chat_memory_promote import enqueue_chat_memory_extract
from virtual_self.services.chat_modes import (
    ModeCommand,
    SessionModeState,
    mode_command_ack,
    parse_mode_command,
)
from virtual_self.services.chat_feedback import (
    feedback_command_ack,
    parse_feedback_command,
    submit_chat_turn_feedback,
)
from virtual_self.services.chat_sessions import append_turn, open_cli_session
from virtual_self.services.mode_reflection import (
    maybe_enqueue_persona_reflect_after_turn,
    set_session_active_mode,
)
from virtual_self.services.memories import create_memory
from virtual_self.services.chat_reply import generate_chat_reply_async

UserMemoryAction = Literal["none", "inline", "extract_user"]

_PEOPLE_IN_MY_LIFE = re.compile(
    r"(?is)^(?P<name>[\w][\w\s.'-]{0,60}?)\s+is\s+one\s+of\s+(?:the\s+)?people\s+in\s+my\s+life\.?\s*$"
)
_SHOULD_KNOW = re.compile(r"(?is)\b(?:you\s+)?should\s+(?:have\s+)?know(?:n)?\b")
_REMEMBER_THAT_INLINE = re.compile(r"(?is)\bremember\s+that\s+(?P<body>.+)$")


@dataclass(frozen=True)
class UserMemoryPlan:
    action: UserMemoryAction
    content: str | None = None
    kind: str = "fact"


def user_memory_plan_for_turn(text: str) -> UserMemoryPlan:
    """Classify user lines for chat-scope memory (explicit /remember path is handled separately)."""
    raw = text.strip()
    if not raw or parse_remember_request(raw):
        return UserMemoryPlan("none")

    people = _PEOPLE_IN_MY_LIFE.match(raw)
    if people:
        name = people.group("name").strip()
        return UserMemoryPlan(
            "inline",
            content=f"{name} is one of the people in the user's life.",
            kind="relationship",
        )

    if _SHOULD_KNOW.search(raw):
        return UserMemoryPlan("extract_user")

    remember = _REMEMBER_THAT_INLINE.search(raw)
    if remember:
        body = remember.group("body").strip().rstrip(".")
        if body:
            return UserMemoryPlan("inline", content=body)
        return UserMemoryPlan("extract_user")

    return UserMemoryPlan("none")


def _ensure_llm_for_chat() -> None:
    settings = get_settings()
    backend = settings.llm_backend
    if backend == "auto":
        backend = "openai" if _openai_available(settings.openai_base_url) else "ollama"
    if backend == "openai":
        preflight_llm()
        return
    ensure_chat_model(thaw=True)


def _apply_user_memory_plan(
    auth: AuthContext,
    session_id: UUID,
    user_turn_id: UUID,
    plan: UserMemoryPlan,
) -> tuple[bool, str]:
    """Returns (skip_user_turn_extract, saved_note_prefix)."""
    if plan.action in ("none", "extract_user"):
        return False, ""
    if plan.action == "inline" and plan.content:
        create_memory(
            auth,
            kind=plan.kind,
            content=plan.content,
            scope="chat",
            trust=Trust.USER_ASSERTED.value,
        )
        saved_note = f"[Memory saved: {plan.content}]\n"
        print(f"(saved to memory: {plan.content})", flush=True)
        return True, saved_note
    return False, ""


def _handle_mode_command(
    cmd: ModeCommand,
    mode_state: SessionModeState,
) -> tuple[SessionModeState, str | None, bool]:
    """Returns (new_state, optional_reply_without_llm, should_generate)."""
    prev = mode_state.active_mode
    if cmd.kind == "council":
        if not cmd.user_query.strip():
            return mode_state, "Usage: /council — <question>", False
        return mode_state, None, True
    if cmd.kind == "show":
        return mode_state, mode_command_ack(cmd, previous=prev), False
    if cmd.kind == "off":
        new_state = mode_state.apply(cmd)
        if cmd.user_query.strip():
            return new_state, None, True
        return new_state, mode_command_ack(cmd, previous=prev), False
    if cmd.kind == "set" and cmd.mode:
        new_state = mode_state.apply(cmd)
        if cmd.user_query.strip():
            return new_state, None, True
        return new_state, mode_command_ack(cmd, previous=prev), False
    return mode_state, None, True


def run_chat(auth: AuthContext) -> None:
    print("Virtual Self chat — empty line or Ctrl-D to exit.", flush=True)
    print(
        "Manual modes: /mode analyst|guide|mediator|explorer, /mode off, /council — …",
        flush=True,
    )
    print("Voice feedback on last reply: /good, /off, /off — <your rewrite>", flush=True)
    print("Loading model and personality…", flush=True)
    _ensure_llm_for_chat()
    history: list[dict[str, str]] = []
    session_id = open_cli_session(auth)
    mode_state = SessionModeState()
    last_assistant_turn_id: UUID | None = None
    print("Ready.\n", flush=True)
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            break

        feedback_cmd = parse_feedback_command(user)
        if feedback_cmd is not None:
            append_turn(auth, session_id, role="user", content=user)
            if last_assistant_turn_id is None:
                msg = "No assistant reply to rate yet — chat first, then /good or /off."
                print(f"self> {msg}\n", flush=True)
                append_turn(auth, session_id, role="assistant", content=msg)
                continue
            try:
                submit_chat_turn_feedback(
                    auth,
                    turn_id=last_assistant_turn_id,
                    rating=feedback_cmd.rating,
                    rewrite=feedback_cmd.rewrite,
                )
            except (ValidationError, NotFoundError) as exc:
                print(f"(feedback error: {exc.message})\n", flush=True)
                continue
            ack = feedback_command_ack(feedback_cmd)
            print(f"self> {ack}\n", flush=True)
            append_turn(auth, session_id, role="assistant", content=ack)
            continue

        council_turn = False
        llm_query = user
        mode_cmd: ModeCommand | None = None
        try:
            mode_cmd = parse_mode_command(user)
        except ValidationError as exc:
            print(f"(mode error: {exc.message})\n", flush=True)
            continue

        if mode_cmd is not None:
            mode_state, instant, should_generate = _handle_mode_command(mode_cmd, mode_state)
            set_session_active_mode(auth, session_id, mode_state.active_mode)
            if instant:
                print(f"self> {instant}\n", flush=True)
                append_turn(auth, session_id, role="user", content=user)
                append_turn(auth, session_id, role="assistant", content=instant)
                continue
            if not should_generate:
                append_turn(auth, session_id, role="user", content=user)
                continue
            council_turn = mode_cmd.kind == "council"
            if mode_cmd.user_query.strip():
                llm_query = mode_cmd.user_query.strip()

        user_turn = append_turn(auth, session_id, role="user", content=user)
        set_session_active_mode(auth, session_id, mode_state.active_mode)
        maybe_enqueue_persona_reflect_after_turn(auth)
        saved_note = ""
        skip_user_turn_extract = False
        to_remember = parse_remember_request(user)
        if to_remember:
            create_memory(
                auth,
                kind="fact",
                content=to_remember,
                scope="chat",
                trust=Trust.USER_ASSERTED.value,
            )
            saved_note = f"[Memory saved: {to_remember}]\n"
            print(f"(saved to memory: {to_remember})", flush=True)
            skip_user_turn_extract = True
        elif mode_cmd is None:
            skip_user_turn_extract, saved_note = _apply_user_memory_plan(
                auth,
                session_id,
                user_turn["turn_id"],
                user_memory_plan_for_turn(user),
            )
        else:
            skip_user_turn_extract = True

        if not skip_user_turn_extract:
            enqueue_chat_memory_extract(auth, session_id, user_turn["turn_id"])

        print("…retrieving memories (and web if needed) — often 10–30s…", flush=True)
        try:
            outcome = asyncio.run(
                generate_chat_reply_async(
                    auth,
                    llm_query,
                    memory_limit=12,
                    history=history,
                    saved_note=saved_note,
                    session_mode=mode_state.active_mode,
                    council=council_turn,
                )
            )
        except NotFoundError as exc:
            print(f"self> ({exc.message})\n", flush=True)
            history.append({"role": "user", "content": llm_query})
            history.append({"role": "assistant", "content": exc.message})
            append_turn(auth, session_id, role="assistant", content=exc.message)
            continue

        history.append({"role": "user", "content": outcome.model_user_content})
        reply = outcome.reply
        if reply is None:
            print("self> (model unavailable — thaw chat model and retry)\n")
            history.pop()
            continue
        if mode_cmd and mode_cmd.kind == "set" and mode_cmd.mode and not mode_cmd.user_query.strip():
            reply = f"{mode_command_ack(mode_cmd, previous=None)}\n\n{reply}"
        history.append({"role": "assistant", "content": reply})
        print(f"self> {reply}\n")
        asst = append_turn(auth, session_id, role="assistant", content=reply)
        last_assistant_turn_id = asst["turn_id"]
