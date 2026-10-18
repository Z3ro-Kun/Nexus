from datetime import datetime
from uuid import UUID

from sqlalchemy import Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base
from app.persistence.types import UTCDateTime


class Document(Base):
    """Uploaded or ingested document storage for user-provided run context."""

    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="text/plain")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
