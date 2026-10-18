"""Pydantic schemas for conversations and multi-turn chat threads."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.documents import AttachedDocument


class ConversationTurn(BaseModel):
    """One user-assistant exchange in a conversation."""

    run_id: UUID
    parent_run_id: UUID | None = None
    objective: str
    status: str
    created_at: datetime
    updated_at: datetime
    deliverable: dict[str, Any] | None = None
    documents: list[AttachedDocument] = Field(default_factory=list)


class ConversationSummary(BaseModel):
    """Brief representation of a conversation for list/sidebar display."""

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    turn_count: int = 0
    last_status: str | None = None


class ConversationDetail(BaseModel):
    """Full conversation with all turns and context."""

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    turns: list[ConversationTurn] = Field(default_factory=list)


class CreateConversationRequest(BaseModel):
    """Request to create a new conversation thread."""

    title: str | None = Field(default=None, max_length=255)
    objective: str | None = Field(default=None, max_length=10_000)
    document_ids: list[UUID] = Field(default_factory=list, max_length=20)


class SendConversationMessageRequest(BaseModel):
    """Send a new message / follow-up into an existing conversation."""

    objective: str = Field(min_length=1, max_length=10_000)
    document_ids: list[UUID] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
