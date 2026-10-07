"""Pure TaskGraph tests: readiness, runnable/blocked selection and every validation rule."""

import itertools

import pytest

from app.core.exceptions import (
    DependencyCycleError,
    DuplicateTaskError,
    InvalidReplacementError,
    MissingDependencyError,
    NonViableDependencyError,
    SelfDependencyError,
    TaskGraphError,
    TaskNotFoundError,
    UnknownParentError,
)
from app.events.types import TaskCreated
from app.orchestration.task_graph import TaskGraph
from app.state.models import TaskStatus

S = TaskStatus


def graph(edges: dict[str, list[str]], statuses: dict[str, TaskStatus] | None = None) -> TaskGraph:
    """Build a validated graph from {task: [dependencies]}."""
    g = TaskGraph()
    for task_id, deps in edges.items():
        g.add_task(task_id, deps, status=(statuses or {}).get(task_id, S.PENDING))
    g.validate()
    return g


def spec(task_id: str, *deps: str, parent: str | None = None) -> TaskCreated:
    return TaskCreated(task_id=task_id, title=task_id, dependencies=list(deps), parent_id=parent)


# --- Readiness -----------------------------------------------------------------------------


def test_a_independent_tasks_are_all_runnable() -> None:
    g = graph({"t1": [], "t2": [], "t3": []})

    assert g.runnable() == ["t1", "t2", "t3"]
    assert g.blocked() == []


def test_b_linear_chain_only_first_is_runnable() -> None:
    edges = {"t1": [], "t2": ["t1"], "t3": ["t2"]}

    assert graph(edges).statuses() == {"t1": S.READY, "t2": S.PENDING, "t3": S.PENDING}
    assert graph(edges, {"t1": S.COMPLETED}).runnable() == ["t2"]
    assert graph(edges, {"t1": S.COMPLETED, "t2": S.COMPLETED}).runnable() == ["t3"]


def test_c_diamond_waits_for_all_dependencies() -> None:
    edges = {"t1": [], "t2": ["t1"], "t3": ["t1"], "t4": ["t2", "t3"]}

    assert graph(edges).runnable() == ["t1"]
    assert graph(edges, {"t1": S.COMPLETED}).runnable() == ["t2", "t3"]
    one_branch = graph(edges, {"t1": S.COMPLETED, "t2": S.COMPLETED, "t3": S.RUNNING})
    assert one_branch.status("t4") is S.PENDING
    both = graph(edges, {"t1": S.COMPLETED, "t2": S.COMPLETED, "t3": S.COMPLETED})
    assert both.runnable() == ["t4"]


def test_join_becomes_ready_only_after_both_inputs_complete() -> None:
    edges = {"t1": [], "t2": [], "t3": ["t1", "t2"]}

    assert graph(edges).statuses() == {"t1": S.READY, "t2": S.READY, "t3": S.PENDING}
    assert not graph(edges, {"t1": S.COMPLETED}).is_ready("t3")
    assert graph(edges, {"t1": S.COMPLETED, "t2": S.COMPLETED}).is_ready("t3")


def test_started_and_finished_tasks_are_not_runnable() -> None:
    g = graph({"a": [], "b": [], "c": [], "d": []}, {"a": S.RUNNING, "b": S.COMPLETED, "c": S.FAILED})

    assert g.runnable() == ["d"]


def test_results_do_not_depend_on_insertion_order() -> None:
    edges = {"t1": [], "t2": ["t1"], "t3": ["t1"], "t4": ["t2", "t3"], "t5": []}
    statuses = {"t1": S.COMPLETED, "t3": S.FAILED}
    expected = graph(edges, statuses)

    for order in itertools.permutations(edges):
        shuffled = graph({task: edges[task] for task in order}, statuses)
        assert shuffled.statuses() == expected.statuses()
        assert shuffled.runnable() == expected.runnable()
        assert shuffled.topological_order() == expected.topological_order()


def test_dependency_queries() -> None:
    g = graph({"t1": [], "t2": ["t1"], "t3": ["t1"], "t4": ["t2", "t3"]})

    assert g.dependencies("t4") == ("t2", "t3")
    assert g.dependents("t1") == ("t2", "t3")
    assert g.dependents("t4") == ()
    assert g.topological_order() == ["t1", "t2", "t3", "t4"]
    with pytest.raises(TaskNotFoundError):
        g.dependencies("missing")


def test_add_dependency_changes_readiness() -> None:
    g = graph({"t1": [], "t2": []})
    g.add_dependency("t2", "t1")

    assert g.runnable() == ["t1"]
    assert g.dependencies("t2") == ("t1",)


# --- Failure propagation -------------------------------------------------------------------


@pytest.mark.parametrize("dead", [S.FAILED, S.CANCELLED])
def test_failed_or_cancelled_dependency_blocks_dependents_transitively(dead: TaskStatus) -> None:
    g = graph({"t1": [], "t2": ["t1"], "t3": ["t2"], "t4": []}, {"t1": dead})

    assert g.statuses() == {"t1": dead, "t2": S.BLOCKED, "t3": S.BLOCKED, "t4": S.READY}
    assert g.blocked() == ["t2", "t3"]
    assert g.runnable() == ["t4"]


def test_one_failed_branch_blocks_the_join_but_not_the_other_branch() -> None:
    edges = {"t1": [], "t2": ["t1"], "t3": ["t1"], "t4": ["t2", "t3"]}
    g = graph(edges, {"t1": S.COMPLETED, "t2": S.FAILED})

    assert g.statuses() == {"t1": S.COMPLETED, "t2": S.FAILED, "t3": S.READY, "t4": S.BLOCKED}


# --- Validation ----------------------------------------------------------------------------


def test_duplicate_task_id_is_rejected() -> None:
    g = graph({"t1": []})
    with pytest.raises(DuplicateTaskError):
        g.add_task("t1")
    with pytest.raises(DuplicateTaskError):
        g.plan_additions([spec("t2"), spec("t2")])


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(SelfDependencyError):
        TaskGraph().add_task("t1", ["t1"])
    g = graph({"t1": []})
    with pytest.raises(SelfDependencyError):
        g.add_dependency("t1", "t1")


def test_missing_dependency_is_rejected() -> None:
    g = TaskGraph()
    g.add_task("t2", ["t1"])
    with pytest.raises(MissingDependencyError, match="'t1'"):
        g.validate()
    with pytest.raises(MissingDependencyError):
        graph({"t1": []}).add_dependency("t1", "nope")
    with pytest.raises(MissingDependencyError):
        graph({"t1": []}).plan_additions([spec("t2", "nope")])


def test_d_cycle_is_rejected() -> None:
    g = TaskGraph()
    g.add_task("t1", ["t3"])
    g.add_task("t2", ["t1"])
    g.add_task("t3", ["t2"])

    with pytest.raises(DependencyCycleError, match="t1 -> t3 -> t2 -> t1"):
        g.validate()
    with pytest.raises(DependencyCycleError):
        TaskGraph().plan_additions([spec("t1", "t3"), spec("t2", "t1"), spec("t3", "t2")])


def test_cycle_via_add_dependency_is_rejected_and_graph_unchanged() -> None:
    g = graph({"t1": [], "t2": ["t1"], "t3": ["t2"]})

    with pytest.raises(DependencyCycleError):
        g.add_dependency("t1", "t3")
    assert g.dependencies("t1") == ()
    assert g.runnable() == ["t1"]


def test_two_node_cycle_is_rejected() -> None:
    with pytest.raises(DependencyCycleError):
        TaskGraph().plan_additions([spec("a", "b"), spec("b", "a")])


@pytest.mark.parametrize("dead", [S.FAILED, S.CANCELLED, S.BLOCKED])
def test_new_task_cannot_depend_on_non_viable_task(dead: TaskStatus) -> None:
    edges = {"t0": [], "t1": ["t0"]} if dead is S.BLOCKED else {"t1": []}
    statuses = {"t0": S.FAILED} if dead is S.BLOCKED else {"t1": dead}
    g = graph(edges, statuses)
    assert g.status("t1") is dead

    with pytest.raises(NonViableDependencyError):
        g.plan_additions([spec("t2", "t1")])


def test_parent_must_already_exist() -> None:
    g = graph({"root": []})

    assert g.plan_additions([spec("child", parent="root")])
    with pytest.raises(UnknownParentError):
        g.plan_additions([spec("child", parent="nope")])
    with pytest.raises(UnknownParentError):
        g.plan_additions([spec("p"), spec("child", parent="p")])  # same batch is not enough


def test_duplicate_dependency_is_rejected() -> None:
    with pytest.raises(TaskGraphError):
        TaskGraph().add_task("t2", ["t1", "t1"])
    g = graph({"t1": [], "t2": ["t1"]})
    with pytest.raises(TaskGraphError):
        g.add_dependency("t2", "t1")


def test_cannot_add_dependency_to_started_task() -> None:
    g = graph({"t1": [], "t2": []}, {"t2": S.RUNNING})
    with pytest.raises(TaskGraphError, match="running"):
        g.add_dependency("t2", "t1")


def test_errors_are_typed_nexus_errors() -> None:
    for cls in (
        DuplicateTaskError,
        SelfDependencyError,
        MissingDependencyError,
        DependencyCycleError,
        NonViableDependencyError,
        UnknownParentError,
        InvalidReplacementError,  # Phase 5
    ):
        assert issubclass(cls, TaskGraphError)
        assert cls.status_code == 422
    assert len({cls.code for cls in TaskGraphError.__subclasses__()}) == 7


# --- Planning batches ----------------------------------------------------------------------


def test_plan_orders_batch_by_dependencies_without_mutating_graph() -> None:
    g = graph({"existing": []})
    batch = [spec("t4", "t2", "t3"), spec("t3", "t1"), spec("t2", "t1"), spec("t1", "existing")]

    ordered = g.plan_additions(batch)

    assert [s.task_id for s in ordered] == ["t1", "t2", "t3", "t4"]
    assert len(g) == 1 and "t1" not in g
