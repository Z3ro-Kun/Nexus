# NEXUS Task Model

This page describes the task graph and deterministic scheduler as implemented in Phase 2.
Code references are relative to `backend/app/`.

Phase 3 added the planner (which proposes tasks) and LLM agents (which perform them);
see [agent-model.md](agent-model.md). Tasks can also be created directly through the API,
and the scripted executor below remains available for deterministic scheduler tests.

## Task

A task lives in the run's event log. Its current form is the projected `TaskState`
(`state/models.py`):

| Field | Meaning |
|---|---|
| `task_id` | Unique within the run (string, up to 128 chars) |
| `title`, `description?` | Human-readable description |
| `task_type?`, `agent_type?` | Recorded labels. Nothing interprets them yet. |
| `parent_id?` | Reference to an existing task. Recorded only; it has no scheduling effect. |
| `dependencies` | Tasks that must be COMPLETED before this one may start |
| `status` | See below |
| `summary?`, `error?` | From `TaskCompleted` / `TaskFailed` |
| `created_at`, `started_at?`, `completed_at?` | Timestamps of the events that caused each transition. `completed_at` is set for any terminal status. |

The API adds `run_id` when returning a task.

## Lifecycle

```
               TaskCreated
                    │
        ┌───────────┴─────────── not started ───────────────┐
        │  PENDING  ⇄  READY        BLOCKED                  │   (derived from dependencies)
        └──────────────┬──────────────────────────────────── ┘
                       │ TaskStarted (only from READY)          TaskCancelled (only if not started)
                       ▼                                                  ▼
                    RUNNING                                           CANCELLED
              TaskCompleted │ TaskFailed
                  ▼                 ▼
              COMPLETED           FAILED
```

- **Lifecycle statuses** come directly from events: RUNNING (`TaskStarted`), COMPLETED
  (`TaskCompleted`), FAILED (`TaskFailed`) and CANCELLED (`TaskCancelled`).
- **Derived statuses** apply to not-started tasks and are recomputed from the graph after
  every task event:
  - **READY:** every dependency is COMPLETED (or the task has no dependencies).
  - **PENDING:** waiting for dependencies that are still PENDING, READY or RUNNING.
  - **BLOCKED:** at least one dependency is FAILED, CANCELLED or BLOCKED, so the task can
    never run. Blocking propagates transitively.
- The projector enforces the transitions for every writer (`state/projections.py`).
  `TaskStarted` is accepted only for a READY task, so **a task can enter RUNNING at most
  once**. `TaskCompleted` and `TaskFailed` require RUNNING. Running tasks cannot be
  cancelled.

Note on terminology: PENDING (still waiting) and BLOCKED (can never run) are kept
separate. A task waiting on unfinished dependencies is PENDING, not BLOCKED.

## Graph semantics and validation

`orchestration/task_graph.py:TaskGraph` is a pure in-memory graph over task ids,
dependencies and lifecycle statuses.

- **Runnable** means status READY: not started, and every dependency is COMPLETED.
  `runnable()` and `blocked()` return task ids sorted by id.
- Statuses are computed in topological order. Results never depend on insertion order;
  a test checks every permutation of a 5-task graph.
- Queries: `dependencies(task)`, `dependents(task)`, `topological_order()` (ties broken
  by task id), `status(task)`, `is_ready(task)`.

Invalid graphs raise typed errors. They are never repaired.

| Error (`core/exceptions.py`) | HTTP code | Rule |
|---|---|---|
| `DuplicateTaskError` | 422 `duplicate_task` | Task id already exists (in the run or the batch) |
| `SelfDependencyError` | 422 `self_dependency` | A task depends on itself |
| `MissingDependencyError` | 422 `missing_dependency` | A dependency names an unknown task |
| `DependencyCycleError` | 422 `dependency_cycle` | Dependencies form a cycle. The message names the cycle. |
| `NonViableDependencyError` | 422 `non_viable_dependency` | A new task depends on a FAILED, CANCELLED or BLOCKED task |
| `UnknownParentError` | 422 `unknown_parent` | `parent_id` is not an existing task. A parent in the same batch is not enough. |
| `TaskGraphError` (base) | 422 `invalid_task_graph` | Duplicate entry in a dependency list; adding a dependency to a started task |

**Creating tasks.** `POST /runs/{id}/tasks` accepts a batch in any order.
`RunService.create_tasks`:
1. validates the batch against the existing graph (`plan_additions`);
2. orders it so every dependency comes first;
3. appends one `TaskCreated` per task, atomically.

An invalid batch writes nothing. The projector re-checks each `TaskCreated` using the same
graph code, so the same rules also apply to events appended directly.

## Scheduler responsibility

`orchestration/scheduler.py:Scheduler.run(run_id)` decides **when** a task may execute.
It loops:

1. Project the run's state from its events and build the task graph.
2. For each runnable task (sorted by id), **claim** it by appending `TaskStarted`.
3. Start each claimed task on the executor as its own asyncio task. Independent tasks
   therefore run concurrently, and the scheduler, not the task, controls this.
4. Wait for any execution to finish, append `TaskCompleted` or `TaskFailed`, and repeat.

It stops when it has no executions in flight and nothing is runnable. It returns a
`ScheduleReport` with the tasks it started, completed and failed, and the final status of
every task.

- It never marks the run itself completed or failed; that decision is left to later phases.
- It refuses a run that is already completed or failed (`RunNotActiveError`, 409).
- If an executor raises, the exception is logged and recorded as `TaskFailed` with the
  exception type and message.
- If the scheduler itself fails (for example on a database error), it cancels its
  in-flight executions and re-raises.

## Executor responsibility

An executor (`orchestration/task_executor.py`) implements
`async execute(task, context) -> TaskExecutionResult` (`succeeded`, `summary?`, `error?`).
It performs a task and reports the result. It never writes events or touches state; the
scheduler records its results. `context` is the task's `TaskContext` projection
(`build_task_context`: goal, constraints, the task, and its direct dependencies'
results).

A successful result may also carry `events` (only `FactAdded` / `ArtifactAdded`),
`evidence` and `metadata`. They are recorded atomically with `TaskCompleted`. Any other
event type, or result events the projector rejects, turn the outcome into `TaskFailed`.
`AgentTaskExecutor` (Phase 3) is the agent-backed implementation.

The only implementation is `ScriptedTaskExecutor`. Each task's outcome is fixed in advance
(`SUCCESS`, `FAILURE`, optionally after `WAIT_FOR_SIGNAL` on an `asyncio.Event` and/or a
fixed delay). It performs no work, makes no decisions, and is not an agent. It records
calls, start order and peak concurrency so tests can observe the scheduler. The schedule
endpoint uses it, with outcomes supplied in the request.

## Failure behavior

When an executor reports failure, or raises:
- `TaskFailed` is appended and the task becomes FAILED;
- its not-started dependents, transitively, become BLOCKED and are never started;
- independent branches continue.

Nothing is retried and no replanning happens. The outcome is deterministic and visible in
the event log.

## Concurrency guarantees

Protection against duplicate execution works at **both** levels:

- **Process-local:** within one `Scheduler.run` call, a task in the in-flight set is never
  launched again.
- **Database-backed:** a task executes only after its `TaskStarted` commits. The claim
  reuses the Phase 1 mechanism instead of adding a new one:
  1. it projects the state;
  2. it checks the task is READY;
  3. it appends `TaskStarted` with `expected_sequence` equal to the state it checked.

  The append runs under the run-row lock.
  - If another writer appended first, the claim gets `SequenceConflictError`, re-reads
    and retries. If the task is no longer READY, it gives up.
  - A claimant that re-reads after the winner committed is rejected by the projector,
    because the task is RUNNING.

Guarantee: for one run on one PostgreSQL primary (or one SQLite file), **at most one
`TaskStarted` is ever committed per task**. It follows that any number of `Scheduler`
instances, in one process or in several sharing the database, execute a given task at most
once. Tests cover this with 4 competing schedulers, with 10 simultaneous raw claims, and
with the projector rejecting a second start.

Not provided (this is not a distributed scheduler):
- no leases, heartbeats or crash recovery. A task whose scheduler dies after claiming it
  stays RUNNING, and nothing restarts it;
- no exactly-once execution. If the executor finishes but recording the result fails, the
  scheduler raises and the task stays RUNNING;
- no concurrency limit, priorities, retries or fairness.

Each claim or record retries on sequence conflicts up to 50 times, then raises
`SchedulerContentionError` (503).

## Relationship to events

```
scheduler ──TaskStarted/TaskCompleted/TaskFailed──▶ RunService.append_events (validated)
                                                          │
                                                   event store (Phase 1)
                                                          │
                                           projector ──▶ RunState.tasks
```

- **There is no task table.** Tasks exist only as events, and their state is projected on
  read. That keeps the event log the single source of truth; the schema is unchanged from
  Phase 1 (`alembic check` reports no drift).
- The scheduler's only memory is its in-flight set. Everything else is re-read from the
  projected state on each loop, so a run's task state can always be rebuilt from its
  events alone.
- Cost: every loop iteration, claim and record re-projects the whole history, and the
  graph is rebuilt on every task event. That is O(events) per read and O(tasks) per event.
  It is fine at this scale; there are no snapshots or indexes yet.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/runs/{id}/tasks` | Create a batch of tasks: `{tasks: [TaskCreated payloads]}` |
| `GET` | `/api/v1/runs/{id}/tasks` | All tasks, in creation order |
| `GET` | `/api/v1/runs/{id}/tasks/{task_id}` | One task (404 `task_not_found`) |
| `POST` | `/api/v1/runs/{id}/schedule` | Run the scheduler with the scripted executor: `{outcomes?: {task_id: "success" \| "failure"}}` (default success). Returns a `ScheduleReport`. |

The schedule request blocks until nothing is runnable.
