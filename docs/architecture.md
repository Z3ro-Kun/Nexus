# NEXUS Architecture

## Purpose

NEXUS is a hackathon project for **PS01 — Autonomous Systems: From Intent to Execution**.
The goal is a multi-agent system that takes a user's goal and carries it through to a
verified result.

## Guiding principle

> **LLMs make decisions. Deterministic application code controls state, concurrency,
> permissions, events, and execution.**

LLMs are used where judgement is needed: breaking a goal into tasks, choosing actions,
interpreting observations, deciding whether to recover or replan. They never own the
system's state or run side effects directly. Application code does that:

| Concern | Owner |
|---|---|
| Planning, choosing actions, interpreting results | LLM |
| State and its persistence | Deterministic code |
| Scheduling and concurrency | Deterministic code |
| Permissions and tool execution | Deterministic code |
| Event recording | Deterministic code |

An LLM proposes; the orchestrator validates, authorizes, executes, and records.

```
LLM proposes        planner, agents, replanner: untrusted proposals
policy decides      deterministic PolicyEngine (tool metadata + deployment policy)
human approves      approval endpoints, only when the policy requires it
executor performs   ToolExecutor: the only path to a tool; re-checks authorization
verifier verifies   independent verification checkpoint (deterministic checks first)
```

### Integrated execution (Phase 9)

One entry point, `POST /runs/{id}/execute` ([orchestration-model.md](orchestration-model.md)):

```
USER OBJECTIVE
      ↓
   PLANNER                    [LLM]   proposes tasks and actions
      ↓
  TASK GRAPH                  [code]  plan + final verification checkpoint, one atomic append
      ↓
  SCHEDULER                   [code]  repeated passes while there is progress
      ↓
┌───────────────┐
│ POLICY GATE   │             [code]  ALLOW / APPROVAL_REQUIRED (pause for a human) / DENY
└───────┬───────┘
        ↓
    AGENTS/TOOLS              [LLM+code]
        ↓
     EVENTS                   [code]  append-only
        ↓
  SHARED STATE                [code]  projected from events
   ↙    ↓    ↘
RECOVERY CONFLICT VERIFICATION        add tasks / record verdicts
   ↘    ↓    ↙
    SCHEDULER
        ↓
   RUN COMPLETE               [code]  completion gate
```

- **Planner proposes.**
- **Scheduler controls execution.**
- **Policy controls side effects.**
- **Human approval authorizes where required.**
- **Agents produce evidence.**
- **Conflicts trigger resolution.**
- **Recovery adapts the graph.**
- **Verifier independently checks the result.**
- **Only the deterministic system can complete the run.**

### End-to-end flow (Phase 8)

```
USER GOAL
   ↓
PLANNER                      [LLM]
   ↓
TASK GRAPH                   [code]
   ↓
SCHEDULER                    [code]
   ↓
POLICY GATE                  [code]   tool metadata → ALLOW / APPROVAL_REQUIRED / DENY
   ↓
┌──────────────┬───────────────────┬──────────┐
↓              ↓                   ↓
ALLOW     APPROVAL_REQUIRED       DENY → TaskFailed (action_denied) → recovery: PROPAGATE
↓              ↓
execute      HUMAN  (approve / reject via API; recorded as events, executes nothing)
↓              │
↓        ┌─────┴──────┐
↓        ↓            ↓
↓     execute      REJECTED → TaskFailed (approval_rejected) → recovery (bounded replan)
↓        │
└────┬───┘
     ↓
VERIFY                       [code (+LLM)]  checkpoint: requirements, provenance, conflicts, actions
     ↓
PASS / FAIL (→ recovery)
     ↓
COMPLETE                     [code]   RunCompleted only if verified, no approval pending,
                                      all work completed (completion gate)
```

See [policy-model.md](policy-model.md).

## Target architecture

```
User Goal
  → Planner
  → Task Graph
  → Parallel Specialist Agents
  → Shared State
  → Tools
  → Observation
  → Recovery / Replanning
  → Independent Verification
  → Final Result
```

Built so far: integrated orchestration (Phase 9), policy and approval gate (Phase 8), independent verification (Phase 7), conflict detection and resolution
(Phase 6), recovery and replanning (Phase 5), controlled tools (Phase 4), planner and
agent runtime (Phase 3), task graph and deterministic scheduler (Phase 2), event-sourced
state (Phase 1). Not built: frontend, background job runner.

Real-LLM status: the planner, agents (including a real `http_fetch` tool loop),
replanning and recovery have been run against a real OpenAI-compatible endpoint. Phase 6
conflict resolution and Phase 7 semantic verification have only been tested with
`FakeLLMProvider`.

## Execution pipeline

Details: [orchestration-model.md](orchestration-model.md), [llm-routing.md](llm-routing.md), [agent-model.md](agent-model.md), [tool-model.md](tool-model.md), [recovery-model.md](recovery-model.md), [conflict-model.md](conflict-model.md), [verification-model.md](verification-model.md), [policy-model.md](policy-model.md),
[task-model.md](task-model.md), [event-model.md](event-model.md).

```
USER                              POST /runs
 ↓
PLANNER                 [LLM]     proposes a task graph
 ↓
PLAN VALIDATION         [code]    schema → policy → graph; rejects, never repairs
 ↓
TASK GRAPH              [code]    TaskCreated events
 ↓
SCHEDULER               [code]    when tasks run; concurrency; at-most-once start
 ↓
AGENT                   [LLM]     reasons; may request a tool call (structured AgentStep)
 ↓
TOOL REGISTRY + POLICY  [code]    exists? authorized for this agent? valid arguments? under limit?
 ↓
CONTROLLED TOOL         [code]    calculator / http_fetch (SSRF-guarded) / fakes; timeouts
 ↓
TOOL RESULT             [data]    untrusted; returned to the agent as escaped data
 ↓
AGENT                   [LLM]     continues (bounded) or finishes with a report
 ↓
EVENT STORE             [code]    Tool*, FactAdded (with provenance), ArtifactAdded, TaskCompleted/Failed
 ↓
STATE                   [code]    RunState rebuilt from events (tasks, facts, tool_calls, ...)
```

Failure recovery (Phase 5), entered when the scheduler records a `TaskFailed`:

```
Task Failure                [code]  TaskFailed with failure_type / error_type / tool_call_id
    ↓
Failure Classification      [code]  TOOL / AGENT / PLANNING / VALIDATION / DEPENDENCY / TIMEOUT / POLICY
    ↓
Recovery Policy             [code]  REPLAN | PROPAGATE (dependents blocked) | FAIL_RUN (budget spent)
    ↓
Replanner                   [LLM]   proposes replacement tasks from a RecoveryContext
    ↓
Plan Validation             [code]  schema → policy → replan rules → graph → duplicate
    ↓
New Tasks                   [code]  ReplanTriggered + TaskCreated(replaces=failed) — history untouched
    ↓
Scheduler                   [code]  runs them; the failed task's dependents resolve through the replacement
```

Bounded by `NEXUS_MAX_REPLANS_PER_RUN` (default 2, counting every replanner invocation);
when it is spent the run gets `RunFailed`. There are no retries.

Conflict handling (Phase 6), after every recorded `TaskCompleted`:

```
Agent facts                 [data]  FactAdded with a structured claim (subject / attribute = value unit)
   ↓
Conflict Detector           [code]  per fact key; equal values = corroboration; fingerprint dedup
   ↓
Conflict?
 ├── NO  → continue
 └── YES
       ↓
 ConflictDetected           [code]  structured: type, key, fact ids, reason
       ↓
 Resolution Task            [code]  TaskCreated(conflict_id): ordinary researcher task, normal runtime/tools
       ↓
 Additional Evidence        [LLM+tools]  facts from a *different* source, tool-derived
       ↓
 ConflictResolved           [code]  only if independent tool evidence agrees; else ConflictUnresolved
```

Original facts and conflicts are never deleted or rewritten; a failed resolution task
goes through Phase 5 recovery like any task. See [conflict-model.md](conflict-model.md).

Independent verification (Phase 7), at an explicit checkpoint:

```
completed work              [data]  tasks, facts (+provenance), artifacts, tool calls, conflicts
   ↓
Checkpoint task             [code]  TaskCreated(verification): READY when its dependencies completed;
                                    deferred while a relevant conflict resolution is pending
   ↓
VerificationStarted         [code]  the exact context verified
   ↓
Deterministic checks        [code]  tasks, provenance, conflicts, required facts/constraints,
                                    artifacts/structured fields, tool evidence
   ↓
Semantic verifier           [LLM]   optional; only if every check passed; schema-validated
   ↓
 ├── PASS → VerificationPassed + TaskCompleted   (re-checked by the projector)
 └── FAIL → VerificationFailed + TaskFailed(VERIFICATION_FAILURE)
             → Phase 5 RecoveryManager → remediation tasks + replacement checkpoint (code)
```

See [verification-model.md](verification-model.md).

**LLM-controlled decisions** (proposals, treated as untrusted):
- goal decomposition: tasks, types, agent types, dependencies;
- the semantic verification judgement (objective / constraints), only after every
  deterministic check passed, with citations checked against the run;
- within a task: whether to request a tool call and with which arguments, and the final
  summary, facts, evidence and artifacts.

**Deterministic system control** (authoritative):
- plan validation and task creation;
- scheduling and concurrency;
- which tools exist, and which agent may call which;
- argument validation, limits, timeouts and SSRF rules;
- the per-task tool-call limit;
- recording every tool call;
- failure classification, whether recovery is allowed, the replan budget, replan
  validation and graph evolution;
- conflict detection, resolution tasks, and the evidence rules that decide a conflict;
- verification checkpoints, the deterministic checks, the verdict rules, and the
  replacement checkpoint after a failed verification;
- the policy gate (every tool call and every action task), approval records, the
  completion gate, and which writer may append which event type;
- deriving fact provenance from the actual tool trace;
- persisting events and projecting state.

An LLM never writes to the database, mutates `RunState`, starts tasks, controls
concurrency, grants itself tools or runs code. Tool output is data, never instructions.

Real vs fake:
- The OpenAI-compatible adapter has been exercised against a real endpoint (planner,
  agents, tool loop, recovery); the Anthropic adapter has not.
- The real `http_fetch` and calculator were verified against the real world
  (`scripts/verify_real_tools.py`).
- `web_search` and `python_analysis` exist only with fake backends.

## Execution control (Phase 2)

- The scheduler decides **when** each task may run. A task is runnable when it has not
  started and all its dependencies are COMPLETED. Independent runnable tasks execute
  concurrently as asyncio tasks.
- Every lifecycle change is an event (`TaskCreated`, `TaskStarted`, `TaskCompleted`,
  `TaskFailed`, `TaskCancelled`) that goes through the Phase 1 event store and projector.
  There is no task table, so task state is always reconstructable from events.
- A task starts at most once. This is enforced by the projector (`TaskStarted` only from
  READY) together with Phase 1 optimistic concurrency. It holds across competing
  schedulers that share one database. It is not a distributed scheduler: there are no
  leases or crash recovery.
- A failed task blocks its dependents. There are no retries. From Phase 5, an eligible
  failure can be replanned (see below and [recovery-model.md](recovery-model.md)).

## Event-sourced state

Implemented in Phase 1. Full details are in [event-model.md](event-model.md).

```
EVENT STORE (append-only, source of truth)   runs + events tables
      │  ordered by (run_id, sequence)
      ▼
PROJECTOR (pure, deterministic)              app/state/projector.py
      ▼
MATERIALIZED STATE (RunState, never stored)  app/state/models.py
      ▼
CONTEXT PROJECTION (requested fields only)   app/state/context_builder.py
```

- **The event log is authoritative.** A run's state is whatever its ordered events say.
- **Events are append-only.** They are never updated or deleted. This is enforced by the
  repository API, an ORM guard and database triggers.
- **Materialized state is derived.** `RunState` is rebuilt from the log on every read and
  never saved. The one stored copy, `runs.status`, is written in the same transaction as
  the event that changes it, for lookup only.
- **The projector reconstructs state.** It is a pure fold over the events: the same
  ordered events always give the same state. It rejects events it cannot apply instead of
  skipping them.
- **Writes are validated.** New events are applied to the current projected state before
  they are stored, so the stored history always stays reconstructible.
- **Envelope:** `id`, `run_id`, `sequence`, `event_type`, `timestamp`, `agent_id?`,
  `task_id?` and a typed JSON `payload`.
- **Ordering:** `sequence` is per run, starts at 1, and is unique and gap-free. It is
  assigned by incrementing a per-run counter on the `runs` row, which serializes writers
  to the same run. `UNIQUE(run_id, sequence)` backs this up, and optional
  `expected_sequence` checks give optimistic concurrency.

**Planned, not built:** agents will read context projections of this state and record
what they do by emitting events. They will not modify shared state directly. The
component that will do this does not exist yet.

## Current scope

### Phase 0 (foundation)

- **Backend** (`backend/`): FastAPI app built by an application factory
  (`app/main.py:create_app`).
  - `GET /api/v1/health`: liveness check returning service name, version and environment.
    It does not touch the database.
  - Settings from environment variables / `.env` via pydantic-settings
    (`app/core/config.py`). The database password is a `SecretStr`.
  - Async SQLAlchemy engine and session factory (`app/persistence/database.py`), exposed
    through FastAPI dependencies (`app/core/dependencies.py`).
  - `NexusError` exceptions with a JSON error handler.
- **Frontend** (`frontend/`): React + TypeScript + Vite page with the NEXUS title, a live
  API health indicator and a placeholder dashboard card.
- **Infrastructure:** `docker-compose.yml` with a PostgreSQL 16 service; `.env.example`.
- **Scripts:** `scripts/check_db.py` checks connectivity to the configured PostgreSQL.

### Phase 1 (event-sourced state)

- ORM models `Run` and `EventRecord` (`app/models/`), plus Alembic migration `0001`
  (`backend/migrations/`).
- Typed event contract (`app/events/`): 18 event types with Pydantic payload schemas.
- `EventRepository` (append, append batch, list, list after a sequence) and
  `RunRepository` (`app/persistence/repositories/`).
- Projector, projection handlers, `RunState` and the context builder (`app/state/`).
- `RunService` (`app/services/runs.py`): the transactional write path.
- Run and event endpoints under `/api/v1/runs` (listed in event-model.md).

### Phase 2 (task graph + deterministic scheduler)

- Task lifecycle events `TaskStarted`, `TaskFailed`, `TaskCancelled`; `TaskCreated`
  extended with `task_type`, `agent_type`, `parent_id` and `dependencies` (21 event types).
- `TaskGraph` (`app/orchestration/task_graph.py`): readiness, runnable and blocked tasks,
  and typed validation errors.
- `Scheduler` (`app/orchestration/scheduler.py`) and the executor interface with
  `ScriptedTaskExecutor` (`app/orchestration/task_executor.py`).
- Task endpoints and `POST /api/v1/runs/{id}/schedule` (listed in task-model.md).

### Phase 3 (agent runtime + planner)

- LLM provider abstraction (`app/llm/`): `LLMProvider` interface, `AnthropicProvider`
  (structured outputs, bounded timeouts; not verified against the real API) and
  `FakeLLMProvider` (deterministic, used by all tests).
- Agents (`app/agents/`): interface and structured results, registry (planner,
  researcher, analyst, specialist), planner with schema/policy/graph validation, and
  `AgentTaskExecutor` implementing the Phase 2 executor interface.
- `PlanningService` (`app/services/planning.py`), per-task context projection
  (`build_task_context`), `ArtifactAdded` event, run constraints, and task evidence and
  metadata on `TaskCompleted`.
- `POST /api/v1/runs/{id}/plan`. `/schedule` now defaults to the agent executor.

### Phase 4 (controlled tool system)

- Tools (`app/tools/`):
  - interface, registry, per-agent policy and executor;
  - real `calculator` (AST whitelist) and real `http_fetch` (SSRF guard, IP pinning, size
    and time limits; opt-in);
  - `web_search` and `python_analysis` over pluggable backends, with fake backends only;
  - fakes for all four.
- Agent tool loop (`app/agents/tooling.py`, `reasoning.py`, `runtime.py`): bounded by
  `NEXUS_MAX_TOOL_CALLS_PER_TASK`, with tool results returned as untrusted data.
- Tool events projected into `RunState.tool_calls`; fact provenance
  (`FactAdded.provenance`) derived from the tool trace.
- `scripts/verify_real_tools.py`: opt-in real-network verification.

### Phase 5 (failure recovery + replanning)

- `app/recovery/`: deterministic failure classifier, recovery policy, recovery context and
  `RecoveryManager`; `app/agents/replanner.py`: LLM replanner behind the same
  validation gates as the planner, plus replan rules and a plan fingerprint.
- Graph evolution by replacement (`TaskCreated.replaces`): failed tasks stay FAILED, and
  dependents resolve through the replacement.
- Events: classified `TaskFailed`, structured `ReplanTriggered`, new `ReplanRejected`,
  `RunFailed` when the replan budget (`NEXUS_MAX_REPLANS_PER_RUN`) is spent.
  `RunState.recovery` summarizes recovery.
- Failure messages are redacted and truncated (`app/core/redaction.py`).
- `scripts/demo_recovery.py`: deterministic end-to-end demo (fake LLM, injected outage).

### Phase 6 (conflict detection + resolution)

- Structured fact claims (`FactAdded.claim`) with a deterministic fact key.
- `app/state/conflict_detector.py`: pure, per-key detection (numeric / textual /
  attribute disagreement), corroboration, fact applicability, fingerprint dedup,
  evidence assessment and `current_value` (no last-write-wins).
- `app/conflicts/`: resolution task builder, deterministic verdict, and
  `ConflictManager` (called by the scheduler after each completed task).
- Events: structured `ConflictDetected` / `ConflictResolved`, new `ConflictUnresolved`,
  `TaskCreated.conflict_id`. State: `RunState.conflicts` extended.
- `NEXUS_MAX_CONFLICT_RESOLUTIONS_PER_RUN` (default 5).
- `scripts/demo_conflicts.py`: deterministic end-to-end demo through the API.

### Phase 7 (independent verification)

- `app/verification/`: deterministic checks (`checks.py`), verification context
  (`context.py`), provider-independent `Verifier` (`verifier.py`), LLM
  `LLMSemanticVerifier` (`semantic.py`), checkpoint builders (`checkpoint.py`) and
  `VerificationManager` (`manager.py`, called by the scheduler for READY checkpoints).
- Events: `TaskCreated.verification`; structured `VerificationStarted` /
  `VerificationPassed` / `VerificationFailed`; `FailureType.VERIFICATION_FAILURE`.
  State: `RunState.verifications`.
- Recovery: a failed verdict is replanned in remediation mode; the replacement
  checkpoint is built by code.
- API: `POST` / `GET /runs/{id}/verification`; `ScheduleRequest.verification`.

### Phase 8 (policy / approval gate)

- `ToolDefinition.category` (read_only, network_read, reversible_write, irreversible).
- `app/policy/`: deterministic `PolicyEngine` (`engine.py`), state rules and the completion
  gate (`rules.py`), `ActionTaskExecutor` (`actions.py`), `PolicyManager` /
  `ApprovalManager` (`manager.py`), `RunCompletion` (`completion.py`).
- The `ToolExecutor` gates every call; action tasks are gated before they start.
- Events: new `PolicyEvaluated` (25 types); structured `ApprovalRequested` / `Granted` /
  `Rejected`; `TaskCreated.action`. State: `RunState.policy`, `RunState.approvals`.
- `RunCompleted` gated by `completion_blockers`; `app/events/authority.py` keeps
  privileged event types out of the raw events API.
- API: `GET /runs/{id}/approvals`, `POST .../approvals/{id}/approve|reject`.

### Phase 9 (integrated orchestration)

- `app/orchestration/orchestrator.py`: the thin `Orchestrator` (plan once, ensure the
  checkpoint, scheduler passes until no progress, stop at completed / failed / waiting
  for approval / blocked).
- `app/orchestration/result.py`: derived run phase and `FinalResult`.
- Planner: optional, schema-closed action proposals (`PlannerOutput.actions`), validated
  against the action tools' input models; the policy engine still decides.
- `PlanningService`: plan + final checkpoint `verify.objective` in one append.
- API: `POST /runs/{id}/execute`, `GET /runs/{id}/result`.
- `scripts/demo_orchestration.py`: deterministic end-to-end demo through the API.

Not implemented yet: a real search backend, a Python sandbox, generic retries,
authentication/roles, approval timeouts, a background job runner, crash recovery for claimed tasks, state
snapshots, Redis, and a frontend for runs.

## Repository layout

```
backend/
  app/
    api/            HTTP routes (v1/health.py, v1/runs.py)
    core/           config, logging, errors, dependencies
    events/         event contract: envelope, types, factory
    agents/         agent interface, registry, planner, reasoning agents, runtime
    llm/            provider interface, Anthropic + OpenAI-compatible adapters, role-based router, fake provider
    tools/          tool interface, registry, policy, executor, tools, SSRF guard, fakes
    models/         ORM tables: runs, events
    orchestration/  task graph, scheduler, executor interface, orchestrator, run phase / final result
    recovery/       failure classifier, recovery policy, recovery context, recovery manager
    conflicts/      conflict resolution task + verdict, conflict manager
    verification/   checks, context, verifier (+ LLM semantic verifier), checkpoints, manager
    policy/         policy engine, gate rules, action executor, policy/approval managers, completion
    persistence/    engine/session, column types, repositories
    schemas/        API request/response models
    services/       transactional use cases (runs.py)
    state/          RunState, projector, handlers, context builder
  migrations/       Alembic environment and versions
  tests/
frontend/           React + TypeScript + Vite UI
scripts/            Developer utilities
docs/               Documentation
docker-compose.yml
```

## Running locally

```bash
cp .env.example .env              # then set POSTGRES_PASSWORD
docker compose up -d postgres

cd backend
uv sync
uv run alembic upgrade head               # create/upgrade the schema
uv run pytest                             # SQLite only; PostgreSQL variants skip
uv run uvicorn app.main:app --reload       # http://localhost:8000

cd ../frontend
npm install
npm run dev                                 # http://localhost:5173 (proxies /api to :8000)
```

To run every database test against PostgreSQL as well, point
`NEXUS_TEST_DATABASE_URL` at a disposable database (its tables are dropped and
recreated). The live connectivity test also needs `NEXUS_RUN_DB_TESTS=1`:

```bash
NEXUS_TEST_DATABASE_URL=postgresql+asyncpg://nexus:<pw>@localhost:5432/nexus_test NEXUS_RUN_DB_TESTS=1 uv run pytest
```
