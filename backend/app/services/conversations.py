"""Service layer for conversational chat threads and follow-ups."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import RunNotFoundError
from app.events.factory import new_event
from app.events.types import AttachedDocument, EventType
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.event import EventRecord
from app.models.run import Run, RunStatus
from app.persistence.types import utc_now
from app.schemas.conversations import (
    ConversationDetail,
    ConversationSummary,
    ConversationTurn,
)
from app.services.runs import RunService
from app.state.projector import project


def _extract_deliverable_summary(state) -> str | None:
    # 1. Gather substantive task summaries (excluding verifier)
    task_summaries: list[tuple[datetime, str | None, str]] = []
    for t in state.tasks.values():
        if t.status.value == "completed" and t.summary and t.agent_type != "verifier":
            ts = t.completed_at or utc_now()
            task_summaries.append((ts, t.agent_type, t.summary.strip()))

    # Sort by completion time so latest synthesizing agent comes last
    task_summaries.sort(key=lambda x: x[0])
    chosen_summary = task_summaries[-1][2] if task_summaries else None

    # 2. Check artifacts for markdown or text content
    md_artifacts: list[str] = []
    for a in state.artifacts.values():
        if a.content and a.content.strip():
            md_artifacts.append(a.content.strip())

    if md_artifacts and chosen_summary:
        last_artifact = md_artifacts[-1]
        if last_artifact in chosen_summary:
            return chosen_summary
        return f"{chosen_summary}\n\n{last_artifact}"
    elif md_artifacts:
        return md_artifacts[-1]
    elif chosen_summary:
        return chosen_summary

    # 3. Fallback: verified facts if available
    if state.facts:
        return "\n".join(f"- {f.content}" for f in state.facts.values())

    # 4. Fallback: any completed task description
    for task in state.tasks.values():
        if task.status.value == "completed" and task.description:
            return task.description

    return None


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._runs = RunService(session)

    async def list_conversations(self) -> list[ConversationSummary]:
        """List all conversations ordered by recent activity."""
        stmt = select(Conversation).order_by(desc(Conversation.updated_at))
        result = await self._session.execute(stmt)
        conversations = result.scalars().all()

        summaries: list[ConversationSummary] = []
        for conv in conversations:
            # Count runs and get latest run status
            run_stmt = (
                select(Run)
                .where(Run.conversation_id == conv.id)
                .order_by(desc(Run.created_at))
            )
            runs_res = await self._session.execute(run_stmt)
            runs = list(runs_res.scalars().all())

            if runs:
                evs = await self._runs.get_events(runs[0].id)
                st = project(evs) if evs else None
                last_status = st.status if st else runs[0].status
            else:
                last_status = None

            summaries.append(
                ConversationSummary(
                    id=conv.id,
                    title=conv.title,
                    created_at=conv.created_at,
                    updated_at=conv.updated_at,
                    turn_count=len(runs),
                    last_status=last_status,
                )
            )
        return summaries

    async def get_conversation(self, conversation_id: UUID) -> ConversationDetail:
        """Get full conversation detail including all turns and deliverables."""
        conv = await self._session.get(Conversation, conversation_id)
        if conv is None:
            raise RunNotFoundError(f"Conversation {conversation_id} not found")

        # Get all runs in chronological order
        stmt = (
            select(Run)
            .where(Run.conversation_id == conversation_id)
            .order_by(Run.created_at.asc())
        )
        runs_res = await self._session.execute(stmt)
        runs = list(runs_res.scalars().all())

        turns: list[ConversationTurn] = []
        for r in runs:
            # Reconstruct state to extract deliverable, facts, and attached docs
            events = await self._runs.get_events(r.id)
            state = project(events) if events else None

            deliverable_data: dict | None = None
            if state:
                deliverable_data = {
                    "goal": state.goal,
                    "status": state.status.value,
                    "failure_reason": state.failure_reason,
                    "facts": [
                        {"fact_id": f.fact_id, "content": f.content, "source": f.source}
                        for f in state.facts.values()
                    ],
                    "artifacts": [
                        {"artifact_id": a.artifact_id, "name": a.name, "media_type": a.media_type, "content": a.content}
                        for a in state.artifacts.values()
                    ],
                    "deliverable_summary": _extract_deliverable_summary(state),
                    "verification": None,
                }

                if getattr(state, "verifications", None):
                    v = next(iter(state.verifications.values()))
                    deliverable_data["verification"] = {
                        "passed": v.status.value == "passed",
                        "details": {
                            "reason": v.reason,
                            "checks": [
                                {
                                    "kind": getattr(c.kind, "value", str(c.kind)),
                                    "passed": c.passed,
                                    "message": c.message,
                                }
                                for c in v.checks
                            ],
                        },
                    }

            turns.append(
                ConversationTurn(
                    run_id=r.id,
                    parent_run_id=r.parent_run_id,
                    objective=r.goal,
                    status=state.status if state else r.status,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                    deliverable=deliverable_data,
                    documents=list(state.documents) if state else [],
                )
            )

        return ConversationDetail(
            id=conv.id,
            title=conv.title,
            created_at=conv.created_at,
            updated_at=conv.updated_at,
            turns=turns,
        )

    async def create_conversation(
        self,
        title: str | None = None,
        objective: str | None = None,
        document_ids: list[UUID] = (),
    ) -> tuple[Conversation, Run | None]:
        """Create a new conversation, optionally starting its first run."""
        now = utc_now()
        conv_id = uuid4()
        effective_title = title or (objective[:60].strip() if objective else "New Conversation")

        conv = Conversation(
            id=conv_id,
            title=effective_title,
            created_at=now,
            updated_at=now,
        )
        self._session.add(conv)
        await self._session.flush()

        # Update documents conversation_id
        if document_ids:
            await self._session.execute(
                update(Document)
                .where(Document.id.in_(document_ids))
                .values(conversation_id=conv_id)
            )

        initial_run: Run | None = None
        if objective:
            initial_run = await self._runs.create_run(
                goal=objective,
                conversation_id=conv_id,
                document_ids=document_ids,
            )

        await self._session.commit()
        return conv, initial_run

    async def send_message(
        self,
        conversation_id: UUID,
        objective: str,
        document_ids: list[UUID] = (),
        constraints: list[str] = (),
    ) -> Run:
        """Send a follow-up message into an existing conversation."""
        conv = await self._session.get(Conversation, conversation_id)
        if conv is None:
            raise RunNotFoundError(f"Conversation {conversation_id} not found")

        # Find the latest run in this conversation to act as parent_run_id
        stmt = (
            select(Run)
            .where(Run.conversation_id == conversation_id)
            .order_by(desc(Run.created_at))
        )
        runs_res = await self._session.execute(stmt)
        latest_run = runs_res.scalars().first()

        # Update documents to link to this conversation
        if document_ids:
            await self._session.execute(
                update(Document)
                .where(Document.id.in_(document_ids))
                .values(conversation_id=conversation_id)
            )

        run = await self._runs.create_run(
            goal=objective,
            constraints=constraints,
            conversation_id=conversation_id,
            parent_run_id=latest_run.id if latest_run else None,
            document_ids=document_ids,
        )

        conv.updated_at = utc_now()
        await self._session.commit()
        return run

    async def delete_conversation(self, conversation_id: UUID) -> bool:
        """Permanently delete a conversation, its runs, events, documents, and artifacts."""
        conv = await self._session.get(Conversation, conversation_id)
        if conv is None:
            return False

        # Find all runs belonging to this conversation
        stmt = select(Run).where(Run.conversation_id == conversation_id)
        runs_res = await self._session.execute(stmt)
        runs = list(runs_res.scalars().all())
        run_ids = [r.id for r in runs]

        # Temporarily disable triggers for cascade delete of this conversation's data
        from sqlalchemy import delete, text
        bind = self._session.bind
        dialect_name = bind.dialect.name if bind else ""

        if dialect_name == "postgresql":
            await self._session.execute(text("ALTER TABLE events DISABLE TRIGGER events_append_only;"))
        elif dialect_name == "sqlite":
            await self._session.execute(text("DROP TRIGGER IF EXISTS events_no_delete;"))

        try:
            # Delete events
            if run_ids:
                await self._session.execute(
                    delete(EventRecord).where(EventRecord.run_id.in_(run_ids))
                )
                await self._session.execute(
                    delete(Run).where(Run.id.in_(run_ids))
                )

            # Delete documents
            await self._session.execute(
                delete(Document).where(Document.conversation_id == conversation_id)
            )

            # Delete conversation
            await self._session.delete(conv)
            await self._session.commit()
        finally:
            if dialect_name == "postgresql":
                await self._session.execute(text("ALTER TABLE events ENABLE TRIGGER events_append_only;"))
                await self._session.commit()
            elif dialect_name == "sqlite":
                from app.models.event import SQLITE_DELETE_TRIGGER
                await self._session.execute(text(SQLITE_DELETE_TRIGGER))
                await self._session.commit()

        return True
