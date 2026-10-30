from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from virtual_self.api.deps import auth_with_scope
from virtual_self.auth.credentials import AuthContext
from virtual_self.services import proposal_review as review

router = APIRouter(tags=["proposals"])


class ProposalEditBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class LifeChapterEditBody(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=4000)


class PersonalityEditBody(BaseModel):
    proposed_content: str | None = Field(default=None, min_length=1, max_length=8000)
    reason: str | None = Field(default=None, max_length=2000)


@router.post("/v1/proposals/{ref}:approve")
def proposal_approve(ref: str, auth: AuthContext = Depends(auth_with_scope("personality.approve"))):
    return review.approve_by_ref(auth, ref)


@router.post("/v1/proposals/{ref}:reject")
def proposal_reject(ref: str, auth: AuthContext = Depends(auth_with_scope("personality.approve"))):
    return review.reject_by_ref(auth, ref)


@router.post("/v1/proposals/{ref}:edit")
def proposal_edit(
    ref: str,
    body: ProposalEditBody,
    auth: AuthContext = Depends(auth_with_scope("personality.approve")),
):
    return review.edit_by_ref(auth, ref, text=body.text)


@router.post("/v1/life-chapters/{chapter_id}:approve")
def life_chapter_approve(
    chapter_id: str,
    auth: AuthContext = Depends(auth_with_scope("personality.approve")),
):
    _, cid = review.resolve_proposal(auth, chapter_id)
    return review.approve_life_chapter(auth, cid)


@router.post("/v1/life-chapters/{chapter_id}:reject")
def life_chapter_reject(
    chapter_id: str,
    auth: AuthContext = Depends(auth_with_scope("personality.approve")),
):
    _, cid = review.resolve_proposal(auth, chapter_id)
    return review.reject_life_chapter(auth, cid)


@router.post("/v1/life-chapters/{chapter_id}:edit")
def life_chapter_edit(
    chapter_id: str,
    body: LifeChapterEditBody,
    auth: AuthContext = Depends(auth_with_scope("personality.approve")),
):
    _, cid = review.resolve_proposal(auth, chapter_id)
    return review.edit_life_chapter(auth, cid, title=body.title, summary=body.summary)


@router.post("/v1/personality/proposals/{proposal_id}:approve")
def personality_proposal_approve(
    proposal_id: str,
    auth: AuthContext = Depends(auth_with_scope("personality.approve")),
):
    kind, pid = review.resolve_proposal(auth, proposal_id)
    if kind != "personality":
        from virtual_self.domain.errors import NotFoundError

        raise NotFoundError("not a personality proposal")
    return review.approve_personality_proposal(auth, pid)


@router.post("/v1/personality/proposals/{proposal_id}:reject")
def personality_proposal_reject(
    proposal_id: str,
    auth: AuthContext = Depends(auth_with_scope("personality.approve")),
):
    kind, pid = review.resolve_proposal(auth, proposal_id)
    if kind != "personality":
        from virtual_self.domain.errors import NotFoundError

        raise NotFoundError("not a personality proposal")
    return review.reject_personality_proposal(auth, pid)


@router.post("/v1/personality/proposals/{proposal_id}:edit")
def personality_proposal_edit(
    proposal_id: str,
    body: PersonalityEditBody,
    auth: AuthContext = Depends(auth_with_scope("personality.approve")),
):
    kind, pid = review.resolve_proposal(auth, proposal_id)
    if kind != "personality":
        from virtual_self.domain.errors import NotFoundError

        raise NotFoundError("not a personality proposal")
    return review.edit_personality_proposal(
        auth,
        pid,
        proposed_content=body.proposed_content,
        reason=body.reason,
    )
