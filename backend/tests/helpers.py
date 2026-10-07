from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.events.base import Event, NewEvent
from app.events.types import EventPayload

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def history(run_id: UUID, *payloads: EventPayload, start: int = 1) -> list[Event]:
    """Build stored-looking events with contiguous sequences and fixed timestamps."""
    return [
        Event(
            id=uuid4(),
            run_id=run_id,
            sequence=start + offset,
            event_type=payload.event_type,
            timestamp=BASE_TIME + timedelta(seconds=start + offset),
            payload=payload,
        )
        for offset, payload in enumerate(payloads)
    ]


def drafts(*payloads: EventPayload, agent_id: str | None = None) -> list[NewEvent]:
    return [NewEvent(payload=payload, agent_id=agent_id) for payload in payloads]


# --- Phase 8: completing a run needs a passing verification --------------------------------


def verified_completion(prefix: list[Event], *, summary: str | None = None, checkpoint: str = "verify") -> list[Event]:
    """Events that take a run whose tasks have all completed through a passing
    verification checkpoint to RunCompleted, continuing `prefix`'s sequences. Built with
    the projector and the real deterministic checks, as the managers would record them."""
    from app.events.types import (
        RunCompleted,
        TaskCompleted,
        TaskCreated,
        TaskStarted,
        VerificationPassed,
        VerificationSpec,
        VerificationStarted,
    )
    from app.state.projector import apply_all, project
    from app.verification.checks import context_refs, run_checks

    events = list(prefix)
    run_id = events[0].run_id

    def add(payload: EventPayload, task_id: str | None = None) -> None:
        n = events[-1].sequence + 1
        events.append(Event(id=uuid4(), run_id=run_id, sequence=n, event_type=payload.event_type,
                            timestamp=BASE_TIME + timedelta(seconds=n), task_id=task_id, payload=payload))

    work = sorted(project(events).tasks)
    add(TaskCreated(task_id=checkpoint, title="Verify", agent_type="verifier", task_type="verification",
                    dependencies=work, verification=VerificationSpec(objective="All planned work is complete.")))
    add(TaskStarted(task_id=checkpoint), checkpoint)
    state = project(events)
    refs = {k: list(v) for k, v in context_refs(state, checkpoint).items()}
    add(VerificationStarted(verification_id=checkpoint, task_id=checkpoint, based_on_sequence=state.last_sequence, **refs), checkpoint)
    state = apply_all(state, events[-1:])
    assert state is not None
    add(VerificationPassed(verification_id=checkpoint, task_id=checkpoint, checks=list(run_checks(state, checkpoint))), checkpoint)
    add(TaskCompleted(task_id=checkpoint, summary="verified"), checkpoint)
    add(RunCompleted(summary=summary))
    return events[len(prefix):]


async def complete_run(service: "RunService", run_id: UUID, summary: str | None = None) -> None:  # type: ignore[name-defined]  # noqa: F821
    """Append `verified_completion` through the service (an internal, trusted writer)."""
    prefix = await service.get_events(run_id)
    drafts = [NewEvent(payload=e.payload, task_id=e.task_id, agent_id=e.agent_id) for e in verified_completion(prefix, summary=summary)]
    await service.append_events(run_id, drafts)
