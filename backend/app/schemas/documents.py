"""Pydantic schemas for document ingestion and retrieval."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentBase(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="text/plain", max_length=100)


class RawDocumentCreate(DocumentBase):
    content_text: str = Field(min_length=1, max_length=500_000)


class DocumentRead(DocumentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    size_bytes: int
    char_count: int
    content_preview: str
    created_at: datetime


class DocumentDetail(DocumentRead):
    content_text: str


class AttachedDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="text/plain", max_length=100)
    content: str = Field(max_length=500_000)
