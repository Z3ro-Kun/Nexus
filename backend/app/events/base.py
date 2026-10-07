"""Event envelopes.

`NewEvent` is what a writer submits: a payload plus optional provenance. The event store
assigns `id`, `sequence` and `timestamp` and returns the stored `Event`.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

from app.events.types import EventPayload, EventType


class NewEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    payload: SerializeAsAny[EventPayload]
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)

    @property
    def event_type(self) -> EventType:
        return self.payload.event_type


class Event(BaseModel):
    """A stored, immutable event. Ordering within a run is defined by `sequence` alone."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    event_type: EventType
    timestamp: datetime
    agent_id: str | None = None
    task_id: str | None = None
    payload: SerializeAsAny[EventPayload]
