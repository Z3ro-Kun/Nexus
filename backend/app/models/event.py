from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.core.exceptions import AppendOnlyViolationError
from app.persistence.database import Base
from app.persistence.types import UTCDateTime


class EventRecord(Base):
    """Stored row of the append-only event log. Use `app.events` types outside persistence."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(128))
    task_id: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )


# --- Append-only enforcement -------------------------------------------------------------
# 1. ORM: flushing a modified or deleted EventRecord raises.
# 2. Database: triggers reject UPDATE/DELETE (and TRUNCATE on PostgreSQL) on `events`.
#    The Alembic migration creates the same PostgreSQL objects; these listeners cover
#    schemas built with `Base.metadata.create_all` (tests).


@sa_event.listens_for(EventRecord, "before_update")
def _reject_update(_mapper: Mapper[Any], _connection: Connection, target: EventRecord) -> None:
    raise AppendOnlyViolationError(f"event {target.id} cannot be updated: events are append-only")


@sa_event.listens_for(EventRecord, "before_delete")
def _reject_delete(_mapper: Mapper[Any], _connection: Connection, target: EventRecord) -> None:
    raise AppendOnlyViolationError(f"event {target.id} cannot be deleted: events are append-only")


PG_REJECT_FUNCTION = """
CREATE OR REPLACE FUNCTION nexus_reject_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING MESSAGE = 'events are append-only: ' || TG_OP || ' is not allowed';
END;
$$
"""
PG_ROW_TRIGGER = """
CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
FOR EACH ROW EXECUTE FUNCTION nexus_reject_event_mutation()
"""
PG_TRUNCATE_TRIGGER = """
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events
FOR EACH STATEMENT EXECUTE FUNCTION nexus_reject_event_mutation()
"""
SQLITE_UPDATE_TRIGGER = """
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only: UPDATE is not allowed'); END
"""
SQLITE_DELETE_TRIGGER = """
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only: DELETE is not allowed'); END
"""

for _sql in (PG_REJECT_FUNCTION, PG_ROW_TRIGGER, PG_TRUNCATE_TRIGGER):
    sa_event.listen(
        EventRecord.__table__, "after_create", DDL(_sql).execute_if(dialect="postgresql")
    )
for _sql in (SQLITE_UPDATE_TRIGGER, SQLITE_DELETE_TRIGGER):
    sa_event.listen(EventRecord.__table__, "after_create", DDL(_sql).execute_if(dialect="sqlite"))
