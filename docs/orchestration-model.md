# NEXUS Orchestration Model (Phase 9)

One objective, one entry point, one run: `POST /api/v1/runs/{id}/execute` (the
`Orchestrator`, app/orchestration/orchestrator.py). The orchestrator is thin: it
coordinates the existing components and owns no execution, recovery, conflict, policy or
verification logic.

```
USER OBJECTIVE                 POST /runs {goal, constraints}
      ↓
   PLANNER          [LLM]      proposes tasks (+ action proposals); validated by code
      ↓
  TASK GRAPH        [code]     plan + final checkpoint "verify.objective" (built by code), one atomic append
      ↓
  SCHEDULER         [code]     passes until no progress; concurrency, at-most-once starts
      ↓
┌──────────────┐
│ POLICY GATE  │    [code]     action tasks: ALLOW / APPROVAL_REQUIRED / DENY before anything runs
└──────┬───────┘
       ↓
  AGENTS / TOOLS    [LLM+code] tool calls through the gated ToolExecutor; facts with provenance
       ↓
    EVENTS          [code]     append-only log
       ↓
  SHARED STATE      [code]     projected from events
   ↙    ↓     ↘
RECOVERY  CONFLICT  VERIFICATION      add tasks (replacements, resolution tasks, remediation +
   ↘    ↓     ↙                       replacement checkpoints) or record verdicts
  SCHEDULER         [code]     next pass picks the new work up
       ↓
 RUN COMPLETE       [code]     RunCompleted only through the completion gate
```

- Planner proposes.
- Scheduler controls execution.
- Policy controls side effects.
- Human approval authorizes where required.
- Agents produce evidence.
- Conflicts trigger resolution.
- Recovery adapts the graph.
- Verifier independently checks the result.
- Only the deterministic system can complete the run.

## `Orchestrator.run(run_id)`

1. **Plan once.** If the run has no tasks: the planner proposes; the plan passes the
   schema / policy / action / graph gates; `PlanningService` appends the tasks *and* the
   final verification checkpoint in one append with `expected_sequence`. A run that
   already has tasks is never re-planned.
2. **Ensure a checkpoint** for runs whose tasks were created another way (API `/tasks`,
   legacy `/plan`): a checkpoint over every current work task.
3. **Scheduler passes.** Each pass is the existing `Scheduler.run` with every handler:
   recovery (Phase 5), conflicts (6), verification (7), policy gate + completion (8). A
   pass runs until nothing is runnable. The orchestrator repeats passes while a pass
   appended events, at most `NEXUS_MAX_ORCHESTRATION_PASSES` (10); a pass that changes
   nothing ends the call, so there is no busy loop.
4. **Stop** at `completed`, `failed`, `waiting_for_approval` or `blocked`, and return an
   `OrchestrationResult`: the phase, what this call started / completed / failed /
   replanned, pending approvals, and the final result.

Resuming is calling it again. After a human approves (`POST .../approvals/{id}/approve`),
the next `/execute` runs the approved action through the gated path, then verification
and completion follow.

## Final verification checkpoint (planner → verification)

The checkpoint (`verify.objective`; planner ids cannot contain a dot, so they cannot
collide) is built by trusted code (`objective_checkpoint`): objective = the run's goal,
dependencies = all planned work (tasks and actions). Its deterministic checks always
apply (work completed, provenance, conflicts, actions authorized *and* executed); with
`NEXUS_VERIFICATION_SEMANTIC=true` (default) and an LLM in use, the semantic verifier
also judges the objective and the run's constraints. The planner cannot write, skip or
weaken it: it has no verification field, the verifier agent type is not plannable, and a
replacement checkpoint must keep the identical spec (Phase 7).

## Planner → action tasks

When side-effecting tools are registered (category `reversible_write` or
`irreversible`), the planner's schema offers `actions`: `{id, title, description (the
intended effect), tool_name (enum of action tools), arguments [{name, value}],
dependencies}`. There is no field for a category, approval or policy outcome; extra
fields are rejected. Gates: the tool is an action tool, argument names are unique, the
arguments validate against the tool's input model (so e.g. a smuggled `approved: true`
argument fails), ids and dependencies pass the graph rules, actions count towards the
task limit. Each action becomes an action task (Phase 8); the policy engine decides
ALLOW / APPROVAL_REQUIRED / DENY when it is READY.

## Adaptive task graph (all through the existing graph and events)

| adaptation | mechanism |
|---|---|
| parallel work | independent READY tasks are started in the same pass |
| dependencies | a task is READY only when its dependencies completed |
| failure | Phase 5: classified `TaskFailed` → replan → replacement; dependents resolve through it |
| conflict | Phase 6: `ConflictDetected` + resolution task; verification waits for it |
| approval | Phase 8: action READY + pending approval → pause; approve → next pass executes |
| verification failure | Phase 7: remediation tasks + replacement checkpoint (same spec) |

## Run phase (derived, `app/orchestration/result.py`)

`created` → `executing` ⇄ (`waiting_for_approval`) → `verifying` → `completed`;
`blocked` when nothing can run and the run cannot complete (e.g. a denied action, an
unrecovered failure, an unresolved conflict without recovery); `failed` after
`RunFailed` (e.g. the replan budget is spent). It is computed from `RunState`, never
stored: no second state machine. Recovery and conflict resolution add tasks, so the run
is `executing` while they run.

## Final result (`GET /runs/{id}/result`, also in `/execute`)

`FinalResult`, derived from state: objective and constraints, phase, `verified`,
completion summary / failure reason, `completion_blockers`, **deliverables** (agent tasks
nothing else builds on, through replacements, with their artifacts; action tasks are
effects, not deliverables), **supporting facts** (the facts the passing checkpoint
covered plus accepted conflict evidence, with provenance), every task (status,
replacement links, action / checkpoint flags), verification attempts (failed checks,
semantic verdict), approvals (status, actor, executed), conflicts (accepted fact),
recovery history, tool-call counts, `last_sequence`.

## Idempotency and concurrency

No new locks; the existing guarantees carry it:

| risk | guarantee |
|---|---|
| duplicate plan | planning requires an empty graph and appends with `expected_sequence`; a concurrent planner's append fails and that call uses the persisted plan (the planner LLM may have been called twice; only one plan is ever recorded) |
| duplicate checkpoint | created with the plan; `ensure` only if none exists, with `expected_sequence` |
| re-executing work | `TaskStarted` only from READY (projector) + optimistic append |
| duplicate side effect | one decision / approval per action; started at most once; exact authorization |
| duplicate verification | one `VerificationStarted` and one verdict per checkpoint |
| duplicate completion | `RunCompleted` once; the run is terminal afterwards |

Calling `/execute` on a completed run makes no scheduler pass and appends nothing.

## API

| method | path | |
|---|---|---|
| `POST` | `/runs/{id}/execute` | the entry point: plan if needed, then run / resume until completed, failed, waiting for approval or blocked. Body (optional): `{executor: "agent" (default) | "scripted", outcomes}`; `scripted` runs pre-created tasks without agents (testing) |
| `GET` | `/runs/{id}/result` | the derived final result |

The lower-level endpoints (`/plan`, `/schedule`, `/verification`, `/approvals`,
`/events`, `/state`) remain.

## Demo

`uv run python ../scripts/demo_orchestration.py` (from backend/): one objective through
the API: plan, parallel research, conflicting prices, resolution, analysis, an
approval-gated order, verification, completion (9 checks, fake LLM and tools).

## Limitations

- Everything here has been exercised only with `FakeLLMProvider` and fake tools.
- `/execute` runs synchronously within the request; there is no background worker, so a
  long run holds the request (fine for the demo; a job runner is future work).
- Competing first calls may each call the planner LLM before one plan wins.
- No approval timeout; a waiting run waits until a human decides.
- The final checkpoint's deterministic requirements are generic (no required facts or
  artifacts are inferred from the objective); the semantic verifier covers the objective.
