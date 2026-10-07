# NEXUS Event Model

This page describes the event-sourced state system as implemented in Phase 1.
Code references are relative to `backend/app/`.

## Event envelope

Every stored event (`events/base.py:Event`) has:

| Field | Type | Set by | Meaning |
|---|---|---|---|
| `id` | UUID | event store | Unique event id |
| `run_id` | UUID | writer | The run the event belongs to |
| `sequence` | int ≥ 1 | event store | Position in the run's log. Defines ordering. |
| `event_type` | string | derived from payload | One of the types below |
| `timestamp` | UTC datetime | event store | When the event was stored. Informational only, never used for ordering. |
| `agent_id` | string, nullable | writer | Who emitted the event (provenance) |
| `task_id` | string, nullable | writer | Task context the event was emitted in (provenance) |
| `payload` | JSON object | writer | What happened; schema depends on `event_type` |

Writers submit a `NewEvent` (payload plus optional `agent_id` / `task_id`). The store
assigns `id`, `sequence` and `timestamp`.

The payload says what happened, for example `TaskCreated.task_id` is the task being
created. The envelope's `agent_id` / `task_id` record who emitted the event and in which
task context.

Payload models (`events/types.py`) are frozen Pydantic models with `extra="forbid"`, and
they serialize to plain JSON. Payloads are validated when an event is created and again
when it is read back from the database.

## Event types

| Event type | Payload | Projection (Phase 1) |
|---|---|---|
| `RunCreated` | `goal`, `constraints[]` | Creates the initial state (must be sequence 1) |
| `TaskCreated` | `task_id`, `title`, `description?`, `task_type?`, `agent_type?`, `parent_id?`, `dependencies[]`, `replaces?`, `conflict_id?`, `verification?`, `action?` | Adds a task; status derived from its dependencies. Rejected if the id exists, it depends on itself or on an unknown, failed, cancelled or blocked task (after replacement resolution), or the parent is unknown. With `replaces` (Phase 5): the target must be FAILED and not yet replaced; sets `replaced_by` on it. |
| `TaskStarted` | `task_id` | READY → RUNNING. Rejected for any other status, so a task starts at most once. |
| `TaskCompleted` | `task_id`, `summary?`, `evidence[]`, `metadata{}` | RUNNING → COMPLETED |
| `TaskFailed` | `task_id`, `error`, `failure_type?`, `error_type?`, `tool_call_id?` | RUNNING → FAILED; records `TaskState.failure`. Not-started dependents become BLOCKED (until a replacement exists). |
| `TaskCancelled` | `task_id`, `reason?` | Not started → CANCELLED. Not-started dependents become BLOCKED. |
| `FactAdded` | `fact_id`, `content`, `source?`, `provenance?`, `claim?` | Adds a fact. The id must be new. Records the envelope's `agent_id`, `task_id`, `sequence` and timestamp (`recorded_at`), plus `provenance` (Phase 4: kind, tool, call id, source, time, fake) and `claim` (Phase 6: subject, attribute, value, unit). Facts are never changed afterwards. |
| `ArtifactAdded` | `artifact_id`, `name`, `media_type`, `content` | Adds a text artifact (plain text, Markdown or JSON). The id must be new. |
| `ConflictDetected` | `conflict_id`, `fact_ids[]`, `conflict_type?`, `fact_key?`, `fingerprint?`, `reason?`, `description?` | Adds an OPEN conflict; `detected_at` is the envelope timestamp. Structured form (Phase 6): ≥ 2 distinct facts with claims on `fact_key` whose values disagree exactly as `conflict_type`; the fingerprint must match (type + sorted fact ids) and be new in the run. Free-text form (`description`, pre-Phase 6): the facts must exist. |
| `ConflictResolved` | `conflict_id`, `resolved_fact_id?`, `evidence_fact_ids[]`, `corroborated_fact_ids[]`, `resolver_task_id?`, `reason?`, `resolution?` | OPEN → RESOLVED. Structured form: the resolver task is COMPLETED and is the conflict's resolution task (or its replacement); every evidence fact is from that task, tool-derived, on the same key, from a source other than the conflicting facts'; the evidence agrees; `resolved_fact_id` ∈ evidence; `corroborated_fact_ids` = the conflicting facts with the accepted value. The free-text form (`resolution`) is accepted only for free-text conflicts. Conflicting facts are never removed. |
| `ConflictUnresolved` | `conflict_id`, `resolver_task_id`, `evidence_fact_ids[]`, `reason` | Phase 6. OPEN → UNRESOLVED (final): the resolver task (as above) produced no reliable result. No accepted fact; evidence is kept. |
| `RunCompleted` | `summary?` | Status becomes `completed` (terminal). Phase 8: only if `completion_blockers` is empty (all work completed, no pending approval, no unreplaced refused action, verification passed). Privileged. |
| `RunFailed` | `reason` | Status becomes `failed` (terminal) |
| `ToolCalled` | `tool_call_id`, `tool_name`, `arguments{}` | Opens a call in `tool_calls`. The id must be new. |
| `ToolSucceeded` | `tool_call_id`, `result`, `metadata{}` | Closes the call as succeeded (once) |
| `ToolFailed` | `tool_call_id`, `error`, `error_type?`, `metadata{}` | Closes the call as failed (once) |
| `ReplanTriggered` | `reason`, `failed_task_id?`, `failure_type?`, `replan_number?`, `strategy_summary?`, `new_task_ids[]`, `replacement_task_id?`, `plan_fingerprint?` | Phase 5: the failed task must be FAILED and not replaced, and `replan_number` must be the next attempt. Adds an accepted record to `RunState.recovery`. Without `failed_task_id` (pre-Phase 5 form): recorded only. |
| `ReplanRejected` | `failed_task_id`, `failure_type`, `replan_number`, `stage`, `reason`, `plan_fingerprint?` | Phase 5: same checks. Adds a rejected record and counts against the replan budget; changes no tasks. |
| `VerificationStarted` | `verification_id`, `task_id?`, `based_on_sequence?`, `covered_task_ids[]`, `fact_ids[]`, `artifact_ids[]`, `tool_call_ids[]`, `conflict_ids[]`, `semantic` | Phase 7 (with `task_id`): the checkpoint is RUNNING and not started before; recorded in its task context; the ids and `based_on_sequence` must equal what the state gives. Verification → `running`. Without `task_id`: recorded only. |
| `VerificationPassed` | `verification_id`, `details?`, `task_id?`, `checks[]`, `semantic?` | Phase 7: the verification is running; `checks` must equal the deterministic checks on the current state and all pass; a passing semantic verdict iff the spec asks for one. Verification → `passed`. Without `task_id`: recorded only. |
| `VerificationFailed` | `verification_id`, `reason`, `task_id?`, `checks[]`, `semantic?` | Phase 7: as above; a check or the semantic verdict failed. Verification → `failed`. Without `task_id`: recorded only. |
| `PolicyEvaluated` | `decision` (task, tool, fingerprint, category, outcome, rule, reason) | Phase 8: one decision per READY action task, about its exact action; an action identical to a rejected one can only be DENY. Privileged. |
| `ApprovalRequested` | `approval_id`, `description`, `task_id?`, `action?` | Phase 8 (with `task_id`): the task's decision is APPROVAL_REQUIRED; one approval per task. Without `task_id`: recorded only. Privileged. |
| `ApprovalGranted` | `approval_id`, `task_id?`, `actor?`, `reason?` | Phase 8: pending → granted (exists, right task, still pending). Privileged. |
| `ApprovalRejected` | `approval_id`, `reason?`, `task_id?`, `actor?` | Phase 8: pending → rejected. Privileged. |

**Recorded only.** (Historical: before Phase 7/8,
what these events should change in state isn't known. Their handler is `record_only`: the
event is validated, stored and advances `last_sequence`, but changes nothing else. This is
set explicitly for each type in `state/projections.py:HANDLERS`. It is not a catch-all
fallback.

**Unknown event types are rejected.** An `event_type` string with no `EventType` member
raises `UnsupportedEventError`, whether it comes from the API or from a stored row. A
known type with no projection handler also raises `UnsupportedEventError` in the
projector. A test checks that every `EventType` has a handler.

**Adding an event type** takes an `EventType` member, a payload class in `PAYLOAD_TYPES`
and an entry in `HANDLERS`. The database schema doesn't change, because `event_type` is
stored as a string and the payload as JSON (JSONB on PostgreSQL).

Task events and statuses are described in detail in [task-model.md](task-model.md).

**Additions in Phase 8** (all with defaults, so earlier events still parse): the new
`PolicyEvaluated` type (25 event types); `TaskCreated.action`; the structured
`ApprovalRequested` / `ApprovalGranted` / `ApprovalRejected` fields. Rules: action tasks
start only after the gate decided, complete only if allowed/approved with exactly their
tool call; `RunCompleted` requires the completion gate. **Event authority:** the raw
events API may not append `PolicyEvaluated`, `Approval*`, `Verification*` or
`RunCompleted` (403 `privileged_event`); see [policy-model.md](policy-model.md).

**Additions in Phase 7** (all with defaults, so earlier events still parse; still 24
event types): `TaskCreated.verification` (a checkpoint, see
[verification-model.md](verification-model.md)); the structured `VerificationStarted`,
`VerificationPassed` and `VerificationFailed` fields; `FailureType.VERIFICATION_FAILURE`.
Verification events now change state (`RunState.verifications`). Projector rules: a
checkpoint completes only after `VerificationPassed`; `TaskFailed` with
`VERIFICATION_FAILURE` exactly for a checkpoint whose verification failed; only a
checkpoint with the same spec replaces a checkpoint. A valid sequence:
TaskCreated(checkpoint), ..., TaskStarted, VerificationStarted, VerificationPassed,
TaskCompleted. Verification events without `task_id` (Phase 1 form) are recorded only.

**Additions in Phase 6** (all with defaults, so earlier events still parse):
`FactAdded.claim`; `TaskCreated.conflict_id`; the structured `ConflictDetected` fields
(`description` becomes optional); the structured `ConflictResolved` fields (`resolution`
becomes optional); and the new `ConflictUnresolved` type (24 event types). A valid
sequence: FactAdded(A), FactAdded(B), ConflictDetected, TaskCreated(resolution),
TaskStarted, FactAdded(evidence), TaskCompleted, ConflictResolved. A ConflictResolved
before its ConflictDetected, a second verdict, or a verdict before the resolver task
completes is rejected. See [conflict-model.md](conflict-model.md).

**Additions in Phase 5** (all with defaults, so earlier events still parse):
`TaskCreated.replaces`; `TaskFailed.failure_type`, `.error_type` and `.tool_call_id`; the
structured `ReplanTriggered` fields; and the new `ReplanRejected` type (23 event types).
`ReplanTriggered` now changes state (`RunState.recovery`). A legacy `ReplanTriggered`
with only `reason` is still accepted and changes nothing. A replan is appended
atomically as `ReplanTriggered` followed by its `TaskCreated` events (agent_id
`replanner`). `TaskFailed.error`, `ToolFailed.error` and recovery text are redacted and
truncated before they are stored. See [recovery-model.md](recovery-model.md).

**Additions in Phase 4** (all with defaults): `FactAdded.provenance`, `Evidence.source` value `tool_output`, `ToolSucceeded.metadata`, and `ToolFailed.error_type` and `.metadata`. Tool events now update state (`RunState.tool_calls`). See [tool-model.md](tool-model.md).

**Additions in Phase 3** (all with defaults, so earlier events still parse): `RunCreated.constraints`, `TaskCompleted.evidence` and `.metadata`, and the new `ArtifactAdded` type. There are 22 event types.

**Schema change in Phase 2.** `TaskCreated` gained `task_type`, `agent_type`, `parent_id`
and `dependencies`, all with defaults, so `TaskCreated` events written in Phase 1 still
parse. The lifecycle rule did change: `TaskCompleted` now requires a prior `TaskStarted`.
A Phase 1 history that completed a task without starting it no longer projects.

## Sequence semantics

- Sequences are per run and start at 1. Within a run they are unique and contiguous
  (1, 2, 3, … with no gaps).
- Ordering is defined by `sequence` alone. Events of different runs are not ordered
  relative to each other.
- A multi-event append gets a consecutive block of sequences, in submission order.

### How sequences are assigned

`runs.last_sequence` is a per-run counter. An append of *n* events runs, inside the
caller's transaction:

```sql
UPDATE runs SET last_sequence = last_sequence + :n, updated_at = :now
WHERE id = :run_id [AND last_sequence = :expected]
RETURNING last_sequence
```

and then inserts the events with sequences `new_last - n + 1 … new_last`.

- **PostgreSQL:** the UPDATE takes a row lock on the run that is held until commit or
  rollback. Concurrent appenders to the same run wait on that lock. Under READ COMMITTED,
  a waiting writer re-evaluates the row after the first commits and increments the
  committed value. Appends to different runs don't block each other.
- **SQLite** (used in tests): the first write takes the database write lock, so writers
  are serialized.
- Because the counter update and the inserts share one transaction, a failed or
  rolled-back append leaves neither events nor a consumed sequence number. That is why
  there are no gaps.
- `UNIQUE(run_id, sequence)` is an independent database guard. If anything wrote a
  colliding row, the append fails with an integrity error and rolls back entirely.

We never use `SELECT MAX(sequence) + 1`.

### Optimistic concurrency

`append(..., expected_sequence=k)` succeeds only if the run is still at sequence *k*.
Otherwise it raises `SequenceConflictError` (HTTP 409).

`RunService.append_events` (the API write path) always uses this. It projects the current
state, checks the new events against that state, then appends with
`expected_sequence = state.last_sequence`. So an event is only stored if it was valid
against the exact history it follows. If two such writers race, one commits and the other
gets a conflict and may re-read and retry. There is no automatic retry.

### Guarantee provided

For a single PostgreSQL primary (or a single SQLite file), within one run: committed
events have unique, gap-free, contiguous sequences, and a multi-event append is
all-or-nothing. Validated appends through `RunService` are also checked against the
history they follow.

Tests cover both backends, with 12 simultaneous writers on separate connections
(`tests/test_concurrency.py`).

Nothing beyond that is claimed. There is no multi-database or distributed ordering, no
ordering across runs, and no idempotency for retried requests.

## Append-only behavior

Stored events are never updated or deleted. This is enforced in layers:

1. **API and repository:** `EventRepository` only has `append` and `list_for_run`, and
   no endpoint updates or deletes events.
2. **ORM:** flushing a modified or deleted `EventRecord` raises
   `AppendOnlyViolationError` (`models/event.py`).
3. **Database:** on PostgreSQL, triggers reject `UPDATE`, `DELETE` and `TRUNCATE` on
   `events`. They are created by migration `0001`. The equivalent `UPDATE`/`DELETE`
   triggers exist for SQLite schemas created from the metadata.

Dropping the table (for example `alembic downgrade`) is still possible. That is a schema
operation, not event mutation.

## State projection

`state/projector.py:project(events)` folds an ordered history into a `RunState`
(`state/models.py`):

```
RunState: run_id, goal, status, tasks{}, facts{}, conflicts{},
          completion_summary, failure_reason, last_sequence, updated_at
```

- Handlers (`state/projections.py`) are pure functions `(state, event) -> new state`.
  State models are frozen, and handlers copy collections instead of mutating them.
- Before any handler runs, the projector checks that:
  - the first event is `RunCreated` at sequence 1, and it appears only once;
  - every event has the same `run_id`;
  - sequences are contiguous;
  - nothing follows `RunCompleted` / `RunFailed`.

  A violation raises `InvalidEventError`.
- `project` uses no clock, randomness, I/O or outside memory. `updated_at` is the
  timestamp of the last event applied.

## Reconstruction

`RunService.get_state(run_id)` loads the run's full event history in sequence order and
calls `project` on it every time. Materialized state is never stored: no table holds
tasks, facts or conflicts. Projecting the same ordered events always yields an equal
`RunState`, and projecting incrementally gives the same result as projecting all at once.

The one derived value that is stored is `runs.status`, a copy kept for listing and
lookup. It is written in the same transaction as the `RunCompleted` / `RunFailed` event
that changes it. The projected status remains authoritative.

Current cost: each read or validated write re-projects the whole history, which is
O(events in the run). Snapshots are not implemented.

## Context projection

`state/context_builder.py:build_context(state, fields)` returns a JSON-ready dict with
only the requested fields (`goal`, `status`, `tasks`, `facts`, `conflicts`). It always
includes `run_id` and `last_sequence`, so the reader knows which point in the log the
context reflects and can pass it back as `expected_sequence` when appending. Unknown
field names are rejected.

## HTTP API (for exercising the system)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/runs` | Create a run and its `RunCreated` event |
| `GET` | `/api/v1/runs/{id}` | Run row |
| `GET` | `/api/v1/runs/{id}/events?after_sequence=N` | Events in sequence order |
| `POST` | `/api/v1/runs/{id}/events` | Append validated events: `{events: [...], expected_sequence?}` |
| `GET` | `/api/v1/runs/{id}/state` | State reconstructed from events |
| `GET` | `/api/v1/runs/{id}/context?fields=...` | Context projection |

Errors use `{"error": {"code", "message"}}`:

| Status | Codes |
|---|---|
| 404 | `run_not_found` |
| 409 | `sequence_conflict` |
| 422 | `invalid_event`, `unsupported_event`, or request validation errors |
