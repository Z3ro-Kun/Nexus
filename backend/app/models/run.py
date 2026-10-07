from datetime import datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import CheckConstraint, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base
from app.persistence.types import UTCDateTime


class RunStatus(str, Enum):
    CREATED = "created"
    COMPLETED = "completed"
    FAILED = "failed"
    # Phase 11: the planner found the objective not executable as stated; nothing was
    # planned or run. Neither a success nor a failure, and final for this run.
    NEEDS_CLARIFICATION = "needs_clarification"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.NEEDS_CLARIFICATION})


class Run(Base):
    """One NEXUS execution.

    `status` mirrors the status derived from the run's events and is written in the same
    transaction as the event that changes it; the event log remains authoritative.
    `last_sequence` is the per-run event sequence counter used by the event store.
    """

    __tablename__ = "runs"
    __table_args__ = (CheckConstraint("last_sequence >= 0", name="last_sequence_non_negative"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_sequence: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
