from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from virtual_self.api.deps import auth_with_scope
from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import NotFoundError, ValidationError
from virtual_self.services.chat_modes import (
    SessionModeState,
    mode_command_ack,
    parse_mode_command,
    resolve_mode_name,
)
from virtual_self.services.chat_reply import generate_chat_reply_async
from virtual_self.services.mode_cards import ModeName

router = APIRouter(tags=["chat"])


class ChatTurnBody(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    memory_limit: int = Field(default=12, ge=1, le=50)


class ChatHistoryTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=8000)


class ChatTurnWithHistoryBody(ChatTurnBody):
    history: list[ChatHistoryTurn] = Field(default_factory=list, max_length=16)
    session_mode: str | None = Field(
        default=None,
        description="Active manual mode from prior turn (analyst|guide|mediator|explorer)",
    )


@router.post("/v1/chat:turn")
async def chat_turn(
    body: ChatTurnWithHistoryBody,
    auth: AuthContext = Depends(auth_with_scope("context.read")),
):
    history = [{"role": h.role, "content": h.content} for h in body.history]
    active_mode: ModeName | None = None
    if body.session_mode:
        active_mode = resolve_mode_name(body.session_mode)
        if active_mode is None:
            raise ValidationError(f"unknown session_mode {body.session_mode!r}")

    council = False
    llm_query = body.query
    instant_reply: str | None = None
    try:
        mode_cmd = parse_mode_command(body.query)
    except ValidationError:
        raise
    if mode_cmd is not None:
        prev = active_mode
        state = SessionModeState(active_mode)
        if mode_cmd.kind == "council":
            council = True
            if not mode_cmd.user_query.strip():
                raise ValidationError("council requires a question: /council — <question>")
            llm_query = mode_cmd.user_query.strip()
        elif mode_cmd.kind == "show":
            instant_reply = mode_command_ack(mode_cmd, previous=prev)
        elif mode_cmd.kind == "off":
            active_mode = None
            if not mode_cmd.user_query.strip():
                instant_reply = mode_command_ack(mode_cmd, previous=prev)
            else:
                llm_query = mode_cmd.user_query.strip()
        elif mode_cmd.kind == "set" and mode_cmd.mode:
            active_mode = mode_cmd.mode
            if not mode_cmd.user_query.strip():
                instant_reply = mode_command_ack(mode_cmd, previous=prev)
            else:
                llm_query = mode_cmd.user_query.strip()

    if instant_reply is not None:
        return {
            "reply": instant_reply,
            "session_mode": active_mode.lower() if active_mode else None,
        }

    try:
        outcome = await generate_chat_reply_async(
            auth,
            llm_query,
            memory_limit=body.memory_limit,
            history=history,
            session_mode=active_mode,
            council=council,
        )
    except NotFoundError as exc:
        raise ValidationError(exc.message) from exc
    reply = outcome.reply
    if reply is None:
        raise ValidationError("chat model unavailable — check LLM on localhost")
    return {
        "reply": reply,
        "session_mode": active_mode.lower() if active_mode else None,
    }
