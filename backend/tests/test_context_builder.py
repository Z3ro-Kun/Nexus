from uuid import uuid4

import pytest

from app.events.types import FactAdded, RunCreated, TaskCreated
from app.state.context_builder import build_context
from app.state.projector import project
from tests.helpers import history

STATE = project(
    history(
        uuid4(),
        RunCreated(goal="g"),
        TaskCreated(task_id="t1", title="a"),
        FactAdded(fact_id="f1", content="x"),
    )
)


def test_context_contains_only_requested_fields() -> None:
    context = build_context(STATE, ["goal", "facts"])

    assert set(context) == {"run_id", "last_sequence", "goal", "facts"}
    assert context["goal"] == "g"
    assert context["facts"]["f1"]["content"] == "x"
    assert context["last_sequence"] == 3
    assert context["run_id"] == str(STATE.run_id)


def test_context_is_json_ready_and_detached_from_state() -> None:
    context = build_context(STATE, ["tasks"])
    context["tasks"]["t1"]["title"] = "changed"

    assert STATE.tasks["t1"].title == "a"


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown context fields"):
        build_context(STATE, ["goal", "secrets"])
