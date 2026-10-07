"""Construction and parsing of events from untyped data (API input, stored rows)."""

from typing import Any

from pydantic import ValidationError

from app.core.exceptions import InvalidEventError, UnsupportedEventError
from app.events.base import Event, NewEvent
from app.events.types import PAYLOAD_TYPES, EventPayload, EventType
from app.models.event import EventRecord


def resolve_event_type(value: str) -> EventType:
    try:
        return EventType(value)
    except ValueError:
        raise UnsupportedEventError(f"unsupported event type: {value!r}") from None


def parse_payload(event_type: EventType | str, data: dict[str, Any]) -> EventPayload:
    resolved = resolve_event_type(event_type) if isinstance(event_type, str) else event_type
    payload_cls = PAYLOAD_TYPES.get(resolved)
    if payload_cls is None:
        raise UnsupportedEventError(f"no payload schema for event type {resolved.value!r}")
    try:
        return payload_cls.model_validate(data)
    except ValidationError as exc:
        raise InvalidEventError(f"invalid {resolved.value} payload: {exc}") from exc


def new_event(
    event_type: EventType | str,
    payload: dict[str, Any],
    *,
    agent_id: str | None = None,
    task_id: str | None = None,
) -> NewEvent:
    try:
        return NewEvent(
            payload=parse_payload(event_type, payload), agent_id=agent_id, task_id=task_id
        )
    except ValidationError as exc:
        raise InvalidEventError(f"invalid event envelope: {exc}") from exc


def event_from_record(record: EventRecord) -> Event:
    event_type = resolve_event_type(record.event_type)
    return Event(
        id=record.id,
        run_id=record.run_id,
        sequence=record.sequence,
        event_type=event_type,
        timestamp=record.timestamp,
        agent_id=record.agent_id,
        task_id=record.task_id,
        payload=parse_payload(event_type, record.payload),
    )
