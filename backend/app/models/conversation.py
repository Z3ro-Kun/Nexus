from datetime import datetime
from uuid import UUID

from sqlalchemy import String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base
from app.persistence.types import UTCDateTime


class Conversation(Base):
    """A user conversation thread containing one or more runs/turns."""

    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
