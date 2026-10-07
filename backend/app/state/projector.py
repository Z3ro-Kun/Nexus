"""Deterministic fold of an ordered event history into `RunState`.

`project(events)` depends only on the events passed in: no clock, randomness, I/O or
external memory. The same ordered events always produce an equal state.

Rules enforced for every event, before its handler runs:
- the first event is `RunCreated` with sequence 1, and it appears only once;
- all events belong to the same run;
- sequences are contiguous (each is the previous + 1);
- nothing may follow `RunCompleted` / `RunFailed`.

Event types with no handler raise `UnsupportedEventError`; they are never skipped.
"""

from collections.abc import Iterable, Mapping

from app.core.exceptions import InvalidEventError, UnsupportedEventError
from app.events.base import Event
from app.events.types import EventType
from app.models.run import TERMINAL_RUN_STATUSES
from app.state.models import RunState
from app.state.projections import HANDLERS, Handler, initial_state


def apply(
    state: RunState | None, event: Event, handlers: Mapping[EventType, Handler] = HANDLERS
) -> RunState:
    if state is None:
        if event.event_type is not EventType.RUN_CREATED:
            raise InvalidEventError(
                f"first event must be RunCreated, got {event.event_type.value}"
            )
        if event.sequence != 1:
            raise InvalidEventError(f"RunCreated must have sequence 1, got {event.sequence}")
        return initial_state(event)

    if event.event_type is EventType.RUN_CREATED:
        raise InvalidEventError(f"run {state.run_id} already has a RunCreated event")
    if event.run_id != state.run_id:
        raise InvalidEventError(f"event {event.id} belongs to run {event.run_id}, not {state.run_id}")
    if event.sequence != state.last_sequence + 1:
        raise InvalidEventError(
            f"expected sequence {state.last_sequence + 1}, got {event.sequence}"
        )
    if state.status in TERMINAL_RUN_STATUSES:
        raise InvalidEventError(f"run {state.run_id} is {state.status.value}; no further events allowed")

    handler = handlers.get(event.event_type)
    if handler is None:
        raise UnsupportedEventError(f"no projection handler for {event.event_type.value}")

    new_state = handler(state, event)
    return new_state.model_copy(
        update={"last_sequence": event.sequence, "updated_at": event.timestamp}
    )


def apply_all(state: RunState | None, events: Iterable[Event]) -> RunState | None:
    for event in events:
        state = apply(state, event)
    return state


def project(events: Iterable[Event]) -> RunState:
    """Reconstruct a run's state from its complete event history."""
    state = apply_all(None, events)
    if state is None:
        raise InvalidEventError("cannot project an empty event history")
    return state
