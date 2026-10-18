"""Run lifecycle operations: the only write path from the API into the event store.

Each public method is one unit of work and commits its own transaction.

Appends are validated before they are stored: the run's history is projected and the new
events are applied to that state. If any would be rejected by the projector, nothing is
written, so the stored history always stays reconstructible. To keep validation valid
under concurrency, the append then uses the validated `last_sequence` as its expected
sequence; if another writer got in first, the append fails with `SequenceConflictError`
and the caller may re-read and retry.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RunNotFoundError, SequenceConflictError, TaskNotFoundError
from app.events.base import Event, NewEvent
from app.events.factory import new_event
from app.events.types import EventType, TaskCreated
from app.models.run import Run, RunStatus
from app.persistence.repositories import EventRepository, RunRepository
from app.persistence.types import utc_now
from app.orchestration.task_graph import TaskGraph
from app.state.models import RunState, TaskState
from app.state.projector import apply_all, project


class RunService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._runs = RunRepository(session)
        self._events = EventRepository(session)

    async def create_run(
        self,
        goal: str,
        constraints: Sequence[str] = (),
        *,
        conversation_id: UUID | None = None,
        parent_run_id: UUID | None = None,
        document_ids: Sequence[UUID] = (),
        documents: Sequence[Any] = (),
    ) -> Run:
        """Create the run row and its RunCreated event in one transaction."""
        from app.models.conversation import Conversation

        if conversation_id is None:
            now = utc_now()
            conv = Conversation(
                id=uuid4(),
                title=goal[:60].strip() or "New Conversation",
                created_at=now,
                updated_at=now,
            )
            self._session.add(conv)
            await self._session.flush()
            conversation_id = conv.id

        all_docs: list[dict[str, Any]] = [
            d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in documents
        ]
        if document_ids:
            from app.documents.service import DocumentService
            doc_service = DocumentService(self._session)
            persisted_docs = await doc_service.get_documents(document_ids)
            for pd in persisted_docs:
                all_docs.append(DocumentService.to_attached_document(pd).model_dump())

        created = new_event(
            EventType.RUN_CREATED,
            {
                "goal": goal,
                "constraints": list(constraints),
                "documents": all_docs,
            },
        )
        run = await self._runs.create(
            goal,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
        )
        await self._events.append(run.id, [created], expected_sequence=0)
        await self._session.commit()
        return await self.get_run(run.id)

    async def get_run(self, run_id: UUID) -> Run:
        run = await self._runs.get(run_id)
        if run is None:
            raise RunNotFoundError(f"run {run_id} not found")
        return run

    async def get_events(self, run_id: UUID, *, after_sequence: int = 0) -> list[Event]:
        await self.get_run(run_id)
        return await self._events.list_for_run(run_id, after_sequence=after_sequence)

    async def get_state(self, run_id: UUID) -> RunState:
        """Reconstruct the run's state from its full event history."""
        return project(await self.get_events(run_id))

    async def create_tasks(
        self,
        run_id: UUID,
        tasks: Sequence[TaskCreated],
        *,
        agent_id: str | None = None,
        expected_sequence: int | None = None,
    ) -> list[TaskState]:
        """Validate a batch of tasks as a graph and record one TaskCreated per task.

        Graph errors (duplicates, missing dependencies, cycles, ...) are raised as typed
        `TaskGraphError`s before anything is written. The events are appended in
        dependency order, atomically.
        """
        state = await self.get_state(run_id)
        if expected_sequence is not None and expected_sequence != state.last_sequence:
            raise SequenceConflictError(
                f"run {run_id} is at sequence {state.last_sequence}, expected {expected_sequence}"
            )
        ordered = TaskGraph.from_tasks(state.tasks.values()).plan_additions(tasks)
        await self.append_events(
            run_id,
            [NewEvent(payload=spec, agent_id=agent_id) for spec in ordered],
            expected_sequence=state.last_sequence,
        )
        new_state = await self.get_state(run_id)
        return [new_state.tasks[spec.task_id] for spec in tasks]

    async def get_task(self, run_id: UUID, task_id: str) -> TaskState:
        task = (await self.get_state(run_id)).tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id!r} not found in run {run_id}")
        return task

    async def append_events(
        self,
        run_id: UUID,
        drafts: Sequence[NewEvent],
        *,
        expected_sequence: int | None = None,
    ) -> list[Event]:
        state = await self.get_state(run_id)
        if expected_sequence is not None and expected_sequence != state.last_sequence:
            raise SequenceConflictError(
                f"run {run_id} is at sequence {state.last_sequence}, expected {expected_sequence}"
            )

        # Validate against the projector before anything is written.
        provisional = [
            Event(
                id=uuid4(),
                run_id=run_id,
                sequence=state.last_sequence + offset,
                event_type=draft.event_type,
                timestamp=utc_now(),
                agent_id=draft.agent_id,
                task_id=draft.task_id,
                payload=draft.payload,
            )
            for offset, draft in enumerate(drafts, start=1)
        ]
        new_state = apply_all(state, provisional)
        assert new_state is not None

        stored = await self._events.append(
            run_id, drafts, expected_sequence=state.last_sequence
        )
        if new_state.status is not state.status:
            await self._runs.set_status(run_id, RunStatus(new_state.status))
        await self._session.commit()
        return stored
