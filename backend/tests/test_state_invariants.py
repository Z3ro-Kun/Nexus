"""Materialized state is derived from events and never acts as the source of truth."""

import ast
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from app.events.types import FactAdded, RunCreated, TaskCreated
from app.persistence.database import Base, Database
from app.persistence.repositories import EventRepository
from app.state.projector import apply, project
from tests.helpers import drafts, history

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_no_table_stores_materialized_state() -> None:
    assert set(Base.metadata.tables) == {"runs", "events"}
    run_columns = set(Base.metadata.tables["runs"].columns.keys())
    assert run_columns.isdisjoint({"tasks", "facts", "conflicts", "state"})


def test_state_objects_are_immutable() -> None:
    state = project(history(uuid4(), RunCreated(goal="g"), TaskCreated(task_id="t", title="x")))

    with pytest.raises(ValueError):
        state.goal = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError):
        state.tasks["t"].title = "changed"  # type: ignore[misc]


def test_applying_an_event_does_not_mutate_the_previous_state() -> None:
    events = history(uuid4(), RunCreated(goal="g"), FactAdded(fact_id="f", content="x"))
    before = project(events[:1])
    snapshot = before.model_dump()

    after = apply(before, events[1])

    assert before.model_dump() == snapshot
    assert "f" not in before.facts and "f" in after.facts


def test_run_state_is_only_constructed_by_the_projection_layer() -> None:
    """Static guard: nothing outside app/state builds RunState objects directly."""
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if path.parent.name == "state":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "RunState":
                offenders.append(str(path.relative_to(APP_DIR)))
    assert offenders == []


async def test_state_endpoint_is_rebuilt_from_the_event_log(
    api: httpx.AsyncClient, database: Database
) -> None:
    run = (await api.post("/api/v1/runs", json={"goal": "g"})).json()
    run_id = run["id"]

    # Write straight to the event store, bypassing the service and API entirely.
    async with database.session_factory() as session:
        await EventRepository(session).append(
            UUID(run_id), drafts(FactAdded(fact_id="direct", content="from the log"))
        )
        await session.commit()

    state = (await api.get(f"/api/v1/runs/{run_id}/state")).json()
    assert state["facts"]["direct"]["content"] == "from the log"
    assert state["last_sequence"] == 2
