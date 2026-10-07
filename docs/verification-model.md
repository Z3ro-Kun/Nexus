# NEXUS Verification Model (Phase 7)

Verification checks completed work against fixed requirements, independently of the
agents that did the work. A worker's own summary ("all done, verified") counts for
nothing: the verifier works from what the run recorded (tasks, facts with provenance,
artifacts, tool calls, conflicts), and every verdict is re-checked by the projector.

```
completed work (tasks, facts, artifacts, tool calls, conflicts)   ← recorded by events
        ↓
verification checkpoint      [code]  a task (agent "verifier"), READY once its dependencies completed;
                                     deferred while a relevant conflict resolution is pending
        ↓
VerificationStarted          [code]  exactly what is being verified (covered tasks, fact/artifact/
                                     tool-call/conflict ids, based_on_sequence)
        ↓
deterministic checks         [code]  app/verification/checks.py
        ↓
  any failed? ── yes ─────────────────────────────────────────────┐
        │ no                                                       │
        ↓                                                          │
semantic verifier (optional) [LLM]   structured, schema-validated; only if the spec asks
        ↓                                                          ↓
VerificationPassed + TaskCompleted              VerificationFailed + TaskFailed(VERIFICATION_FAILURE)
        ↓                                                          ↓
run verified (Phase 8: approval / RunCompleted)       Phase 5 RecoveryManager → remediation replan
                                                      → replacement checkpoint (same requirements) → scheduler
```

## Verification as a task (the lifecycle decision)

A checkpoint is an ordinary task in the task graph: `TaskCreated` with
`agent_type="verifier"`, `task_type="verification"` and a `verification`
(`VerificationSpec`). This reuses everything the task graph already guarantees:

- **Boundary:** its dependencies are what it verifies. The task graph makes it READY only
  when they have all COMPLETED (through Phase 5 replacements), so it can never run before
  the work it verifies. If covered work FAILED and was not replaced, the checkpoint is
  BLOCKED (and its verification state shows `blocked`): it never passes.
- **At most once:** the scheduler claims it with `TaskStarted` like any task (projector:
  READY → RUNNING once; optimistic concurrency on append). Competing schedulers cannot
  verify twice.
- **No self-verification:** a task cannot depend on itself; the verifier writes no facts
  or artifacts; and the projector only accepts a verdict in the checkpoint's own task
  context.
- **Recovery and history:** a failed checkpoint is a FAILED task, so Phase 5 handles it,
  and the replacement chain (`replaces` / `replaced_by`) records every attempt.

It runs only when asked for: a checkpoint is created explicitly (`POST
/runs/{id}/verification`, or any writer of `TaskCreated`), not after every task.

**Coverage:** the checkpoint's dependencies and, transitively, theirs; a replaced failed
task is covered through its replacement. Conflict-resolution tasks are not covered
directly; conflicts are checked through their facts.

## Requirements (`VerificationSpec`)

Fixed at creation; a replacement checkpoint must carry the identical spec.

| field | meaning |
|---|---|
| `objective` | what is being verified (default: the run's goal) |
| `required_facts[]` | `subject`, `attribute`; `tool_derived`; optional `constraint` (`eq ne lt le gt ge`, value, unit) |
| `required_artifacts[]` | `name` (case-insensitive), optional `media_type`, `json_fields[]` (JSON object, fields present and not null) |
| `tool_evidence_tasks[]` | covered tasks that must have produced a valid tool-derived fact |
| `semantic` | also ask the LLM verifier about the objective and the run's free-text constraints |

## Deterministic checks

Pure functions of state, run by the verifier *and* by the projector:

| check | passes when |
|---|---|
| `tasks_completed` | every covered task is COMPLETED |
| `provenance` | every covered fact has provenance; a tool-derived fact cites an existing, successful tool call **of the same task**, with the same tool name, a source that appears in that call's output (or is its default source) and the call's fake flag; task evidence citing a tool call cites a successful call of that task |
| `conflicts` | no relevant conflict (involving a covered fact, or on a required key) is OPEN or UNRESOLVED, and no unrecorded disagreement involves a covered fact |
| `required_fact[i]:<key>` | Phase 6 `current_value` is `accepted`, or `corroborated` by covered facts; tool-derived if required; the constraint holds (units must match) |
| `required_artifact[i]:<name>` | a covered task produced it, with the media type and JSON fields |
| `tool_evidence:<task>` | the task (through replacements) has a fact with valid tool provenance |
| `actions` (Phase 8, only if action tasks are covered) | each covered action was authorized (allowed, or approved by a human) **and** executed: COMPLETED with a successful call of exactly its action |

Each check is a structured `VerificationCheck`: `check_id`, `kind`, `passed`, `message`,
and `references[]` (`EvidenceRef`: kind task / fact / artifact / tool_call / conflict,
and id). Provenance is never taken from model text: it is recomputed from the run's own
tool calls.

## The verifier

`Verifier.verify(VerificationContext) -> VerificationResult` (app/verification/verifier.py)
is provider-independent:

1. the deterministic checks are in the context (computed from state);
2. if any failed → FAIL. The LLM is **not** asked, so it can never overrule a failed check;
3. if all passed and `spec.semantic` → the `SemanticVerifier` (LLM) judges; its verdict
   decides;
4. otherwise → PASS.

`VerificationContext` (app/verification/context.py) is built from projected state, not
from the event log or a worker's report: objective, run constraints, covered tasks
(with the worker's summary labelled `worker_summary`, a claim), facts with provenance and
a `provenance_valid` flag computed by NEXUS, artifacts, tool calls, relevant conflicts,
and the check results.

### LLM semantic verifier

`LLMSemanticVerifier` (purpose `"verifier"`) uses the Phase 3 provider abstraction and
structured output (`VerifierOutput`, strict-compatible). The output is untrusted:

1. **schema:** exactly `objective`, `constraints[]`, `summary`; each judgement is
   `verdict` (pass/fail), `evidence[]` (ids), `explanation`. Extra fields are rejected,
   so there is nowhere to put actions, instructions or state changes;
2. **coverage:** exactly one judgement per run constraint, by index;
3. **citations:** every cited id must exist in the context (kinds are assigned by NEXUS);
   a "pass" must cite at least one item;
4. **verdict:** computed by NEXUS (every judgement passed). The model has no "overall
   verdict" field, and criterion texts are copied from the context.

Violations raise `LLMResponseError` (nothing is repaired). Explanations and the summary
are redacted, truncated and stored as data; they are never interpreted.

## Events

| event | written by | rules (projector) |
|---|---|---|
| `TaskCreated` (+ `verification`) | API / RecoveryManager | agent `verifier`, type `verification`, ≥ 1 dependency, no `conflict_id`; `tool_evidence_tasks` covered. A task with the verifier agent or verification type must have a spec. Only a checkpoint can replace a checkpoint (identical spec, superset of dependencies); a checkpoint never replaces work |
| `VerificationStarted` | VerificationManager | checkpoint RUNNING, not started before, recorded in its task context; `based_on_sequence` = the run's last sequence; the recorded ids equal what the state gives |
| `VerificationPassed` | VerificationManager | verification RUNNING; recorded checks **equal** `run_checks` on the current state; all pass; semantic verdict present and passing iff the spec asks for one |
| `VerificationFailed` | VerificationManager | as above; a check failed or the semantic verdict failed |
| `TaskCompleted` (checkpoint) | VerificationManager | only after `VerificationPassed` |
| `TaskFailed` (checkpoint) | VerificationManager | never after a pass; `failure_type = VERIFICATION_FAILURE` exactly when the verdict failed |

Pass and its `TaskCompleted` (or fail and its `TaskFailed`) are appended atomically.
Verification events without `task_id` (the pre-Phase 7 form) are still accepted and
recorded only.

A valid sequence: `TaskCreated(checkpoint)`, …work…, `TaskStarted(checkpoint)`,
`VerificationStarted`, `VerificationPassed`, `TaskCompleted(checkpoint)`.

Since Phase 8 the verification event types are also privileged: the raw events API
cannot append them at all (see [policy-model.md](policy-model.md)).

**Independence:** a worker's executor result may contain only facts, artifacts and tool
events (the scheduler records nothing else; a verification event in it fails the task as
`POLICY_FAILURE`). Through any writer, including the raw events API, a verdict needs a
RUNNING checkpoint, the right task context, and checks that match the deterministic
checks, so no one can record a pass the checks contradict. The semantic part cannot be
re-derived by the projector; it is recorded with its provider and model.

## State

`RunState.verifications[verification_id]` (`VerificationState`): `checkpoint_id` (the
first attempt), `attempt`, `spec`, `dependencies`, `status` (`pending`, `blocked`,
`running`, `passed`, `failed`, `error`, `cancelled`), the recorded context (covered tasks,
fact / artifact / tool-call / conflict ids, `based_on_sequence`), `checks`, `semantic`,
`failed_references`, `reason`, timestamps and `replaced_by`. All of it is derived from
events. `run_verified(state)`: the run has a checkpoint and the current attempt of every
checkpoint passed.

## Scheduler

The scheduler stays deterministic and owns execution (app/orchestration/scheduler.py):

- a READY checkpoint is never given to the task executor; it is claimed (`TaskStarted`)
  and run by the `VerificationManager`;
- it is deferred while a relevant OPEN conflict still has a resolution task pending
  (`pending_resolution`), so verification waits for Phase 6 instead of failing on a
  conflict that is about to be decided;
- without a verification handler, checkpoints stay READY (`verifications_waiting`);
- the report lists `verifications_passed`, `verifications_failed` and
  `verifications_waiting`.

`VerificationManager.run`: append `VerificationStarted` → `Verifier.verify` (bounded by
`NEXUS_AGENT_TIMEOUT_SECONDS`) → re-run the deterministic checks on the latest state →
append the verdict with the task outcome. If the work changed while verifying, the
verdict reflects the latest state. If the verifier itself cannot run (timeout, provider
error, invalid output, no LLM verifier for a semantic checkpoint), the checkpoint fails
with that cause (`verifier_timeout`, `llm_*`, `verifier_unavailable`), with no verdict:
status `error`.

## Recovery (Phase 5, not a second system)

`VerificationFailed` → the checkpoint's `TaskFailed(VERIFICATION_FAILURE)` → the existing
`RecoveryManager`, policy and replan budget:

- `VERIFICATION_FAILURE` is recoverable. A checkpoint that failed for any other reason
  (the verifier could not run) is `PROPAGATE`: replanning work cannot fix the verifier;
- the replanner runs in **remediation mode**: its context includes `verification` (the
  failed checks with references, failed semantic judgements), and it proposes new work
  only; no task may set `replaces`;
- the manager appends `ReplanTriggered`, the remediation tasks, and a **replacement
  checkpoint built by code** (`replacement_checkpoint`: same spec, old dependencies plus
  the remediation tasks, `replaces` the failed checkpoint), atomically;
- the scheduler runs the remediation, then the new attempt (`<checkpoint>_attempt2`, …);
- when the budget is spent, the run fails (`RunFailed`), as for any failure.

A model can therefore never write, skip or weaken a checkpoint.

## Conflicts (Phase 6 semantics unchanged)

- OPEN with a pending resolution task → the checkpoint waits;
- OPEN without one (budget spent, or the resolver failed and was not replaced) or
  UNRESOLVED → the `conflicts` check fails and names the conflict and its facts. No side
  is chosen;
- RESOLVED → passes; `required_fact` uses the accepted fact (`current_value`), and the
  references include the conflict, the accepted fact and the evidence. Original facts
  stay untouched.

Note that an UNRESOLVED conflict is final in Phase 6, so remediation cannot fix it; such
a run fails verification until the replan budget is spent.

## API

| method | path | |
|---|---|---|
| `POST` | `/api/v1/runs/{id}/verification` | create a checkpoint: `task_id` (default `verify_objective`), `dependencies` (default: every current work task), `objective`, `required_facts`, `required_artifacts`, `tool_evidence_tasks`, `semantic`. 201 → the task; 422 for invalid or non-viable dependencies, duplicates |
| `GET` | `/api/v1/runs/{id}/verification` | `{verified, verifications[]}` |
| `POST` | `/api/v1/runs/{id}/schedule` | `verification: true` (default) runs READY checkpoints; the LLM verifier is used with the agent executor |

## Limitations / later phases

- Real-LLM semantic verification has **not** been validated; all tests use
  `FakeLLMProvider`.
- (Resolved in Phase 8) `RunCompleted` is now gated: it requires `run_verified`, no
  pending approval and all work completed, and it is written only by `RunCompletion`.
- (Phase 9) The orchestrator now creates the final checkpoint `verify.objective` with
  the plan, from trusted code (see [orchestration-model.md](orchestration-model.md));
  other checkpoints are still created explicitly.
- Requirements are deterministic and structured; free-text constraints are only checked
  by the semantic verifier.
- If the verifier itself fails (`error`), nothing retries it automatically.
- The semantic verdict cannot be re-derived by the projector; it is stored with its
  provider/model, and a verdict can only fail, or pass on top of checks that pass.
