from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from virtual_self.auth.credentials import AuthContext
from virtual_self.providers.agent_chat import chat_text
from virtual_self.services.mode_cards import ModeName
from virtual_self.services.chat_web import maybe_gather_web
from virtual_self.services.chat_composer import (
    CHAT_HISTORY_K,
    CHAT_TEMPERATURE,
    COMPOSER_VERSION,
    _format_exemplar_block,
    compose_turn_user,
    expectations_system_prompt,
    late_composer_reminder,
    load_composer_anti_patterns,
    prepare_chat_history,
    render_memory_notes,
    retrieve_voice_exemplars,
)
from virtual_self.services.personality import compile_context
from virtual_self.web.client import KnowledgeGatewayClient, WebKnowledgeTurn


@dataclass(frozen=True)
class ChatTurnOutcome:
    reply: str | None
    model_user_content: str
    composer_version: str = COMPOSER_VERSION


def new_web_knowledge_turn(turn_id: str, user_message: str) -> tuple[WebKnowledgeTurn, KnowledgeGatewayClient]:
    """Per-turn web scope (W-5): pasted URLs + search result_ids; composer wiring in W-8."""
    client = KnowledgeGatewayClient()
    turn = WebKnowledgeTurn(turn_id=turn_id)
    client.register_user_message(turn, user_message)
    return turn, client


# Back-compat for tests and callers that imported private helpers.
_system_prompt = expectations_system_prompt
_memory_block = render_memory_notes
_compose_turn_user = compose_turn_user


async def generate_chat_reply_async(
    auth: AuthContext,
    query: str,
    *,
    memory_limit: int = 12,
    history: list[dict[str, str]] | None = None,
    saved_note: str = "",
    session_mode: ModeName | None = None,
    council: bool = False,
    mode_section: str | None = None,
) -> ChatTurnOutcome:
    """Single chat turn for API/eval — no session writes or memory extraction."""
    bundle = compile_context(auth, query=query, memory_limit=memory_limit)
    bundle.setdefault("provenance", {})["composer_version"] = COMPOSER_VERSION
    turn_id = str(uuid4())
    turn, client = new_web_knowledge_turn(turn_id, query)
    web = await maybe_gather_web(turn, client, query, bundle)
    if mode_section is None:
        from virtual_self.services.chat_modes import composer_mode_section

        mode_section = composer_mode_section(
            auth, session_mode=session_mode, council=council
        )
    mode_section = mode_section or ""
    anti = load_composer_anti_patterns(auth)
    system = expectations_system_prompt(
        bundle,
        anti_patterns=anti,
        web_fail_soft=web.fail_soft_system_note,
        mode_section=mode_section,
    )
    exemplars = retrieve_voice_exemplars(
        auth,
        query,
        mode_hint=session_mode,
    )
    exemplar_block = _format_exemplar_block(exemplars)
    turn_user = compose_turn_user(
        bundle,
        query,
        saved_note=saved_note,
        web_block=web.composer_block,
        exemplar_block=exemplar_block,
    )
    reminder = late_composer_reminder(bundle, mode_section=mode_section)
    messages = prepare_chat_history(history, k=CHAT_HISTORY_K)
    messages.append({"role": "user", "content": turn_user})
    messages.append({"role": "user", "content": reminder})
    reply = chat_text(system, messages, temperature=CHAT_TEMPERATURE)
    return ChatTurnOutcome(
        reply=reply,
        model_user_content=turn_user,
        composer_version=COMPOSER_VERSION,
    )


def generate_chat_reply(
    auth: AuthContext,
    query: str,
    *,
    memory_limit: int = 12,
    history: list[dict[str, str]] | None = None,
) -> str | None:
    """Sync wrapper for callers without a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        outcome = asyncio.run(
            generate_chat_reply_async(
                auth,
                query,
                memory_limit=memory_limit,
                history=history,
            )
        )
        return outcome.reply
    raise RuntimeError("generate_chat_reply cannot run inside an event loop; use generate_chat_reply_async")
