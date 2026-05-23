from __future__ import annotations

from pydantic import BaseModel, Field


class CollectorPreviewRequest(BaseModel):
    prompt: str = Field(min_length=1)
    industry_hint: str = ''
    competitor_hints: list[str] = Field(default_factory=list)
