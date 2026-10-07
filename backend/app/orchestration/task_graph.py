"""Task dependency graph: validation, derived readiness and runnable-task selection.

A pure, in-memory structure. It holds each task's dependencies and its *lifecycle* status
(the status set by events). PENDING / READY / BLOCKED are never stored as lifecycle; they
are derived from the dependencies:

- a started or finished task keeps its lifecycle status (RUNNING/COMPLETED/FAILED/CANCELLED);
- a not-started task is BLOCKED if any dependency is FAILED, CANCELLED or BLOCKED,
  READY if every dependency is COMPLETED, and PENDING otherwise.

Replacement (Phase 5): a task may `replace` one FAILED task. The failed task keeps its
FAILED status, but *for its dependents* it resolves to the status of its replacement (and
so on along a chain T1 -> T4 -> T7). A dependent blocked by T1 therefore becomes PENDING
when T4 is created and READY once T4 completes; no historical task or edge is changed.

Results never depend on insertion order: statuses are computed over a topological order
and every list returned is sorted by task id. Invalid graphs raise a `TaskGraphError`
subclass; nothing is repaired silently.
"""

import heapq
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

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
from app.state.models import NOT_STARTED_STATUSES, TaskState, TaskStatus

# A not-started task depending on one of these can never run.
NON_VIABLE_STATUSES = frozenset({TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED})


@dataclass
class _Node:
    task_id: str
    dependencies: list[str] = field(default_factory=list)
    lifecycle: TaskStatus = TaskStatus.PENDING
    replaces: str | None = None


class TaskGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, _Node] = {}

    @classmethod
    def from_tasks(cls, tasks: Iterable[TaskState]) -> "TaskGraph":
        graph = cls()
        for task in tasks:
            graph.add_task(
                task.task_id, task.dependencies, status=task.status, replaces=task.replaces
            )
        graph.validate()
        return graph

    def copy(self) -> "TaskGraph":
        clone = TaskGraph()
        clone._nodes = {
            task_id: _Node(task_id, list(node.dependencies), node.lifecycle, node.replaces)
            for task_id, node in self._nodes.items()
        }
        return clone

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    # --- construction --------------------------------------------------------------------

    def add_task(
        self,
        task_id: str,
        dependencies: Iterable[str] = (),
        *,
        status: TaskStatus = TaskStatus.PENDING,
        replaces: str | None = None,
    ) -> None:
        """Add a task. Dependencies may name tasks added later; `validate()` checks them."""
        deps = list(dependencies)
        if task_id in self._nodes:
            raise DuplicateTaskError(f"task {task_id!r} already exists")
        if task_id in deps:
            raise SelfDependencyError(f"task {task_id!r} depends on itself")
        if replaces == task_id:
            raise InvalidReplacementError(f"task {task_id!r} cannot replace itself")
        if len(set(deps)) != len(deps):
            raise TaskGraphError(f"task {task_id!r} lists a dependency more than once")
        lifecycle = TaskStatus.PENDING if status in NOT_STARTED_STATUSES else status
        self._nodes[task_id] = _Node(task_id, deps, lifecycle, replaces)

    def add_dependency(self, task_id: str, depends_on: str) -> None:
        """Add the edge `task_id` depends on `depends_on`, rejecting it if the graph would
        become invalid."""
        node = self._node(task_id)
        if depends_on == task_id:
            raise SelfDependencyError(f"task {task_id!r} depends on itself")
        if depends_on not in self._nodes:
            raise MissingDependencyError(f"task {task_id!r} depends on unknown task {depends_on!r}")
        if depends_on in node.dependencies:
            raise TaskGraphError(f"task {task_id!r} already depends on {depends_on!r}")
        if node.lifecycle is not TaskStatus.PENDING:
            raise TaskGraphError(
                f"cannot add a dependency to task {task_id!r}: it is {node.lifecycle.value}"
            )
        node.dependencies.append(depends_on)
        cycle = self._find_cycle()
        if cycle is not None:
            node.dependencies.pop()
            raise DependencyCycleError(_describe_cycle(cycle))

    def plan_additions(self, new_tasks: Sequence[TaskCreated]) -> list[TaskCreated]:
        """Validate `new_tasks` against this graph and return them in dependency order.

        The graph itself is not modified. Rules: ids are unique; no self-dependencies;
        every dependency exists (here or in the batch); no cycles; `parent_id`, if given,
        names a task that already exists; `replaces`, if given, names an existing FAILED
        task that has no replacement yet (one replacement per failed task); and no new
        task depends on a task that is FAILED, CANCELLED or BLOCKED once replacements are
        taken into account (it could never run).
        """
        replaced = self.replaced_by()
        seen: set[str] = set()
        for spec in new_tasks:
            if spec.replaces is None:
                continue
            target = self._nodes.get(spec.replaces)
            if target is None:
                raise InvalidReplacementError(
                    f"task {spec.task_id!r} replaces unknown task {spec.replaces!r}"
                )
            if target.lifecycle is not TaskStatus.FAILED:
                raise InvalidReplacementError(
                    f"task {spec.task_id!r} replaces {spec.replaces!r}, which is "
                    f"{self.status(spec.replaces).value}; only a FAILED task can be replaced"
                )
            if spec.replaces in replaced or spec.replaces in seen:
                raise InvalidReplacementError(
                    f"task {spec.replaces!r} already has a replacement "
                    f"({replaced.get(spec.replaces, 'in this batch')!r})"
                )
            seen.add(spec.replaces)

        candidate = self.copy()
        for spec in new_tasks:
            candidate.add_task(spec.task_id, spec.dependencies, replaces=spec.replaces)
        candidate.validate()

        for spec in new_tasks:
            if spec.parent_id is not None and spec.parent_id not in self._nodes:
                raise UnknownParentError(
                    f"task {spec.task_id!r} names unknown parent {spec.parent_id!r}; "
                    "a parent must already exist"
                )

        current = self.statuses()
        resolved = candidate.resolved_statuses()
        for spec in new_tasks:
            for dep in spec.dependencies:
                if dep in current and resolved[dep] in NON_VIABLE_STATUSES:
                    raise NonViableDependencyError(
                        f"task {spec.task_id!r} depends on {dep!r}, which is {current[dep].value}"
                    )

        by_id = {spec.task_id: spec for spec in new_tasks}
        return [by_id[task_id] for task_id in candidate.topological_order() if task_id in by_id]

    # --- queries -------------------------------------------------------------------------

    def dependencies(self, task_id: str) -> tuple[str, ...]:
        return tuple(self._node(task_id).dependencies)

    def dependents(self, task_id: str) -> tuple[str, ...]:
        self._node(task_id)
        return tuple(
            sorted(other for other, node in self._nodes.items() if task_id in node.dependencies)
        )

    def replaced_by(self) -> dict[str, str]:
        """failed task id -> the id of the task that replaces it."""
        return {
            node.replaces: task_id for task_id, node in self._nodes.items() if node.replaces
        }

    def validate(self) -> None:
        """Raise if any dependency or replaced task is missing, or the graph has a cycle."""
        for task_id in sorted(self._nodes):
            node = self._nodes[task_id]
            for dep in node.dependencies:
                if dep not in self._nodes:
                    raise MissingDependencyError(
                        f"task {task_id!r} depends on unknown task {dep!r}"
                    )
            if node.replaces is not None and node.replaces not in self._nodes:
                raise InvalidReplacementError(
                    f"task {task_id!r} replaces unknown task {node.replaces!r}"
                )
        cycle = self._find_cycle()
        if cycle is not None:
            raise DependencyCycleError(_describe_cycle(cycle))

    def topological_order(self) -> list[str]:
        """Dependencies before dependents; ties broken by task id. A replaced task comes
        after its replacement (the status it presents to dependents is the replacement's)."""
        self.validate()
        edges = self._edges()
        remaining = {task_id: len(deps) for task_id, deps in edges.items()}
        dependents: dict[str, list[str]] = {task_id: [] for task_id in self._nodes}
        for task_id, deps in edges.items():
            for dep in deps:
                dependents[dep].append(task_id)

        heap = [task_id for task_id, count in remaining.items() if count == 0]
        heapq.heapify(heap)
        order: list[str] = []
        while heap:
            task_id = heapq.heappop(heap)
            order.append(task_id)
            for dependent in dependents[task_id]:
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    heapq.heappush(heap, dependent)
        return order

    def statuses(self) -> dict[str, TaskStatus]:
        """Effective status of every task, keyed and sorted by task id."""
        return self._compute()[0]

    def resolved_statuses(self) -> dict[str, TaskStatus]:
        """The status each task presents *to its dependents*: its own status, except that
        a FAILED task with a replacement resolves to the replacement's resolved status."""
        return self._compute()[1]

    def _compute(self) -> tuple[dict[str, TaskStatus], dict[str, TaskStatus]]:
        replaced_by = self.replaced_by()
        result: dict[str, TaskStatus] = {}
        resolved: dict[str, TaskStatus] = {}
        for task_id in self.topological_order():
            node = self._nodes[task_id]
            if node.lifecycle is not TaskStatus.PENDING:
                result[task_id] = node.lifecycle
            else:
                dep_statuses = [resolved[dep] for dep in node.dependencies]
                if any(status in NON_VIABLE_STATUSES for status in dep_statuses):
                    result[task_id] = TaskStatus.BLOCKED
                elif all(status is TaskStatus.COMPLETED for status in dep_statuses):
                    result[task_id] = TaskStatus.READY
                else:
                    result[task_id] = TaskStatus.PENDING
            replacement = replaced_by.get(task_id)
            if result[task_id] is TaskStatus.FAILED and replacement is not None:
                resolved[task_id] = resolved[replacement]
            else:
                resolved[task_id] = result[task_id]
        return dict(sorted(result.items())), dict(sorted(resolved.items()))

    def status(self, task_id: str) -> TaskStatus:
        self._node(task_id)
        return self.statuses()[task_id]

    def is_ready(self, task_id: str) -> bool:
        return self.status(task_id) is TaskStatus.READY

    def runnable(self) -> list[str]:
        """Tasks that may start now: not started, with every dependency COMPLETED."""
        return [task_id for task_id, status in self.statuses().items() if status is TaskStatus.READY]

    def blocked(self) -> list[str]:
        """Tasks that can never run because a dependency failed, was cancelled or is blocked."""
        return [
            task_id for task_id, status in self.statuses().items() if status is TaskStatus.BLOCKED
        ]

    # --- internals -----------------------------------------------------------------------

    def _node(self, task_id: str) -> _Node:
        node = self._nodes.get(task_id)
        if node is None:
            raise TaskNotFoundError(f"task {task_id!r} not found")
        return node

    def _edges(self) -> dict[str, list[str]]:
        """Dependencies plus replacement edges (a replaced task "depends on" its replacement)."""
        edges = {task_id: list(node.dependencies) for task_id, node in self._nodes.items()}
        for task_id, node in self._nodes.items():
            if node.replaces is not None and node.replaces in edges:
                edges[node.replaces].append(task_id)
        return edges

    def _find_cycle(self) -> list[str] | None:
        """Return one dependency cycle as a path (first == last), or None."""
        white, grey, black = 0, 1, 2
        edges = self._edges()
        color = dict.fromkeys(self._nodes, white)
        for root in sorted(self._nodes):
            if color[root] != white:
                continue
            color[root] = grey
            path = [root]
            stack = [iter(edges[root])]
            while stack:
                for dep in stack[-1]:
                    if dep not in self._nodes:
                        continue  # reported by validate() as a missing dependency
                    if color[dep] == grey:
                        return path[path.index(dep):] + [dep]
                    if color[dep] == white:
                        color[dep] = grey
                        path.append(dep)
                        stack.append(iter(edges[dep]))
                        break
                else:
                    color[path.pop()] = black
                    stack.pop()
        return None


def _describe_cycle(cycle: list[str]) -> str:
    return "dependency cycle: " + " -> ".join(cycle) + " (each task depends on the next)"
