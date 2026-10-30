from __future__ import annotations

from pydantic import BaseModel, Field


class MemoryCreate(BaseModel):
    kind: str = Field(default="preference", max_length=64)
    content: str = Field(min_length=1, max_length=32000)
    scope: str = Field(default="private", max_length=32)
    trust: str = Field(default="user_asserted", max_length=32)
    sensitivity: int = Field(default=1, ge=0, le=4)


class MemoryCorrect(BaseModel):
    content: str = Field(min_length=1, max_length=32000)


class MemorySearch(BaseModel):
    query: str = Field(default="", max_length=500)
    limit: int = Field(default=20, ge=1, le=100)
