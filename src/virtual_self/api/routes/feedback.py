from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from virtual_self.api.deps import auth_with_scope
from virtual_self.auth.credentials import AuthContext
from virtual_self.services.chat_feedback import submit_chat_turn_feedback

router = APIRouter(tags=["chat"])


class ChatTurnFeedbackBody(BaseModel):
    turn_id: UUID
    rating: Literal["sounds_like_me", "off", "wrong_fact"]
    note: str | None = Field(default=None, max_length=2000)
    rewrite: str | None = Field(default=None, max_length=8000)


@router.post("/v1/chat:feedback")
def chat_turn_feedback(
    body: ChatTurnFeedbackBody,
    auth: AuthContext = Depends(auth_with_scope("context.read")),
):
    return submit_chat_turn_feedback(
        auth,
        turn_id=body.turn_id,
        rating=body.rating,  # type: ignore[arg-type]
        note=body.note,
        rewrite=body.rewrite,
    )
