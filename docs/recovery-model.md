# NEXUS Recovery Model (Phase 5)

A failure is information. When a task fails and recovery is possible, NEXUS asks a
replanner for a different route to the same result. The replacement tasks are validated,
recorded and scheduled like any other tasks. Recovery is bounded, deterministic wherever
possible, and fully visible in the event log.

```
Task failure            [code]  executor result -> TaskFailed (classified, redacted)
    ↓
Failure classification  [code]  app/recovery/classifier.py -> FailureType
    ↓
Recovery policy         [code]  app/recovery/policy.py -> REPLAN | PROPAGATE | FAIL_RUN
    ↓
Replanner               [LLM]   app/agents/replanner.py: proposes replacement tasks
    ↓
Plan validation         [code]  schema -> policy -> replan rules -> graph -> duplicate
    ↓
New tasks               [code]  ReplanTriggered + TaskCreated(replaces=...) in one append
    ↓
Scheduler               [code]  runs the new tasks; unblocked dependents follow
```

The LLM only proposes. Deterministic code decides whether replanning is allowed, how
many replans a run gets, which task failed, what the replanner is shown, whether the
proposal is valid, how the graph changes, and what runs. The LLM never mutates the task
graph and never restarts tasks.

## Failure classification

`app/recovery/classifier.py` maps a failed execution result to a `FailureType`. The agent
runtime reports each failure's cause as a stable `error_type`. For tool failures, the
tool's own error type (from the `ToolFailed` event of the failed call) decides.

| FailureType | Produced by (error_type) |
|---|---|
| `TOOL_FAILURE` | a tool call failed: `unavailable`, `http_error`, `network_error`, `invalid_output`, `execution_error`, ... |
| `AGENT_FAILURE` | `llm_error`, `agent_reported_failure`, `tool_limit`, `executor_error`, unclassified (e.g. the scripted executor) |
| `PLANNING_FAILURE` | `unknown_agent`, `unsupported_task_type` (the task cannot run as planned) |
| `VALIDATION_FAILURE` | `malformed_result`, `invalid_provenance`, `llm_invalid_response`, `unrecordable_result`, `result_rejected` |
| `TIMEOUT` | `agent_timeout`, `llm_timeout`, a tool `timeout` |
| `POLICY_FAILURE` | a tool `unauthorized`, `ssrf_blocked`, `policy_denied` or `approval_required` (Phase 8); `disallowed_events` (executor tried to write reserved events); an action task refused at the gate: `action_denied`, `approval_rejected` (Phase 8) |
| `DEPENDENCY_FAILURE` | a BLOCKED task whose dependency failed (reported in the recovery context, never recorded as `TaskFailed`) |
| `VERIFICATION_FAILURE` | Phase 7: a verification checkpoint's verdict failed (`verification_failed`). A checkpoint whose verifier could not run gets `TIMEOUT` (`verifier_timeout`, `llm_timeout`), `AGENT_FAILURE` (`verifier_unavailable`, `llm_error`) or `VALIDATION_FAILURE` (`llm_invalid_response`) |

A failure (`FailureRecord`) contains the run id, task id, agent id, failure type, error
type, a safe error message, the failed tool call id (if any) and the timestamp. The
scheduler writes the classification into `TaskFailed` (`failure_type`, `error_type`,
`tool_call_id`). It shows up in state as `TaskState.failure`.

**Safe messages.** `TaskFailed.error`, `ToolFailed.error` and all recovery event text pass
through `app/core/redaction.py:safe_message`. It redacts URL credentials, bearer/basic
tokens, `key=value` pairs whose key looks like a credential (`api_key`, `token`,
`password`, `secret`, ...) and well-known key prefixes, and truncates to 2000 characters.
This is a best-effort filter. The primary rule is still that credentials never go into
error messages in the first place.

## Recovery policy

`RecoveryPolicy.decide(failure, state)` applies these rules in order. No LLM is involved.

1. run already terminal → PROPAGATE; a verification checkpoint that failed for any
   reason other than `VERIFICATION_FAILURE` (the verifier itself could not run) → PROPAGATE
2. failure type not recoverable (`POLICY_FAILURE`, `DEPENDENCY_FAILURE`) → PROPAGATE
3. internal error (`executor_error`: an exception inside NEXUS) → PROPAGATE
4. task already has a replacement → PROPAGATE
5. replan budget spent → FAIL_RUN
6. otherwise → REPLAN

Recoverable: `TOOL_FAILURE`, `AGENT_FAILURE`, `TIMEOUT`, `VALIDATION_FAILURE`,
`PLANNING_FAILURE`, (Phase 7) `VERIFICATION_FAILURE`, and (Phase 8) `POLICY_FAILURE` with
error type `approval_rejected` (a human said no: another route may be planned, never the
same action; an identical re-request is denied), all within the budget. A policy DENY
(`action_denied`, `policy_denied`) stays PROPAGATE. A policy violation is never routed around by
replanning.

- **PROPAGATE**: normal Phase 2 behavior. Dependents become BLOCKED and the run is not
  terminated.
- **FAIL_RUN**: the scheduler stops starting tasks, lets running tasks finish and records
  them, then appends `RunFailed` with the reason.

## Replan budget

`NEXUS_MAX_REPLANS_PER_RUN` (default **2**, range 0–5; 0 disables recovery) bounds the
number of **replanner invocations per run**, accepted or rejected. It counts
`ReplanTriggered` + `ReplanRejected` events (`RunState.recovery.replan_attempts`). A
replanner that keeps producing invalid, identical or unusable plans therefore exhausts
the budget, and the run fails. There is no unlimited or recursive replanning.

## RecoveryContext

`app/recovery/context.py` builds the replanner's input deterministically from projected
state:

- goal and constraints;
- the classified failure (`FailureRecord`);
- the failed task: title, description, agent/task type, dependencies, plus the failed
  tool's name and arguments (truncated);
- the current plan: every task's id, title, types, status, dependencies and
  `replaces`/`replaced_by`;
- completed tasks' summaries and facts (up to 10 each);
- blocked tasks (`DEPENDENCY_FAILURE`) and what blocks them;
- earlier replan attempts in the run (outcome, summary or rejection reason);
- the available agents with their task types and tools;
- this replan's number, the remaining budget, and the maximum number of new tasks.

It never includes the raw event log, full tool outputs or credentials. Inside the
prompt, all of it is marked as data, not instructions.

## Replanner

`ReplannerAgent` (`app/agents/replanner.py`, LLM purpose `"replanner"`) uses the same
provider abstraction and the planner's task schema. It adds `replaces` per task and a
short `strategy_summary` (1–2 sentences; no chain of thought is requested or stored).

Validation pipeline. Any failure raises `PlanRejectedError` with a stage, and nothing is
written or repaired:

1. **schema**: Pydantic validation of the structured output;
2. **policy**: the planner's gate — non-empty; at most `max_new_tasks` (=
   `NEXUS_MAX_PLANNED_TASKS`); known agent types (never `planner` or `replanner`); known
   task types accepted by the agent;
3. **replan rules**: exactly one task `replaces` the failed task, no other task replaces
   anything, and the replacement is not the failed task repeated unchanged (same agent
   type, task type and normalized description) — the "duplicate" stage;
4. **graph**: the Phase 2 rules *against the run's current graph*: new ids only,
   dependencies exist, no cycles (replacement edges included), no dependency on a failed
   or blocked task, and the replacement target is FAILED and not already replaced;
5. **duplicate**: the plan's fingerprint differs from every earlier replan in the run.

The manager then re-validates against the latest state when it appends (the run may
have moved on while the replanner worked). The projector checks everything a third time.

**Plan fingerprint.** SHA-256 over the sorted tasks' (agent type, task type, normalized
title, normalized description, dependencies on existing tasks, number of dependencies on
new tasks). Task ids and the replaced task are ignored, so renaming ids doesn't make a
repeated plan look new. Normalization is lowercase with collapsed whitespace. This is
deliberately not semantic equivalence: a reworded plan gets a new fingerprint.

## RecoveryManager

`app/recovery/manager.py` handles one failure:

1. load state; build the `FailureRecord` from the recorded `TaskFailed`;
2. `RecoveryPolicy.decide`; if not REPLAN, return that outcome;
3. while the budget allows: build the `RecoveryContext`, call the replanner (bounded by
   `NEXUS_AGENT_TIMEOUT_SECONDS`);
   - accepted: append `ReplanTriggered` followed by the new `TaskCreated` events
     atomically (with optimistic concurrency) and return REPLAN;
   - rejected (validation, provider error or timeout): append `ReplanRejected`, then try
     again;
4. budget spent: return FAIL_RUN.

**Failed verification (Phase 7).** For a checkpoint whose verdict failed, the replanner
runs in remediation mode: the `RecoveryContext` carries `verification` (failed checks
with references, failed semantic judgements), and the proposal may only add new tasks
(no `replaces`). The manager appends `ReplanTriggered`, the remediation tasks and a
replacement checkpoint built by code (same requirements, old dependencies plus the
remediation tasks), atomically; `replacement_task_id` is the new checkpoint. Same policy,
same budget, same events. See [verification-model.md](verification-model.md).

It is the only writer of recovery events. A failure of the replanner itself becomes
`ReplanRejected`, never a task failure, so recovery cannot recurse.

**Scheduler integration.** `Scheduler(database, executor, recovery=None)`. After recording
a `TaskFailed`, the scheduler calls `recovery.handle_failure(run_id, task_id)` and only
acts on the outcome. It contains no recovery logic and never calls an LLM. Without a
recovery manager it behaves exactly as in Phase 2. The API's `/schedule` enables
recovery for the agent executor unless the body sets `"recovery": false`.

## Graph evolution

History is never rewritten:

- The failed task stays FAILED with its error and classification. Its events stay as
  they are.
- Replacement tasks are added through new `TaskCreated` events carrying
  `replaces: <failed task id>`. The projection links the two: `TaskState.replaces` and
  `TaskState.replaced_by`.
- **Replacement resolution** (`TaskGraph.resolved_statuses`): a FAILED task with a
  replacement presents its replacement's status *to its dependents*, following chains
  such as T1 → T4 → T7. A historical dependent that was BLOCKED therefore becomes PENDING
  when the replacement is created and READY once the replacement completes. Its declared
  dependencies are unchanged.
- The dependent's task context receives the replacement's results in place of the
  failed task (`DependencyResult.replaces` names the failed task).
- One replacement per failed task. If a replacement fails, a later replan replaces the
  replacement.
- A replan can change the dependency structure upstream of the replacement. For
  example, two new independent search tasks can feed a new merge task that replaces the
  failed one. It cannot edit, restart or delete existing tasks.

Example (the deterministic demo, `scripts/demo_recovery.py`):

```
TaskCreated(research_source_a) TaskCreated(research_source_b) TaskCreated(compare_products)
TaskStarted(a) TaskStarted(b)
ToolCalled(a) ToolFailed(a, unavailable) TaskFailed(a, TOOL_FAILURE/unavailable)
ReplanTriggered(a, #1 -> [research_source_c])   TaskCreated(research_source_c, replaces=a)
TaskStarted(c) ... TaskCompleted(b) ... TaskCompleted(c)
TaskStarted(compare_products) ... TaskCompleted(compare_products)
(run stays "created": nothing declares it complete; verification is a later phase)
```

## Event history

| Event | Written by | Meaning |
|---|---|---|
| `TaskFailed` (+ `failure_type`, `error_type`, `tool_call_id`) | scheduler | classified task failure |
| `ReplanTriggered` | recovery manager | a replan was accepted; its tasks follow in the same append |
| `ReplanRejected` | recovery manager | a replanner invocation produced nothing usable; the graph is unchanged |
| `TaskCreated` (+ `replaces`) | recovery manager (agent_id `replanner`) | a replacement or supporting task |
| `RunFailed` | scheduler | recovery was needed but the budget is spent |

State: `RunState.recovery` holds `replan_count` (accepted), `replan_attempts` (accepted +
rejected) and a concise `history` of records (number, outcome, failed task, failure type,
strategy summary or rejection reason, new task ids, fingerprint, sequence, time). It does
not duplicate the event log. Events stay authoritative, and state is derived from them.

## Safety limits

| Risk | Control |
|---|---|
| infinite replanning | budget on every replanner invocation; FAIL_RUN when spent |
| identical repeated plans | replacement ≠ failed task; fingerprint ≠ any earlier replan |
| recursive planner spawning | `planner`/`replanner` are not assignable agent types and cannot execute tasks; replanner failures are `ReplanRejected`, not task failures |
| task explosion | `NEXUS_MAX_PLANNED_TASKS` per replan × the replan budget |
| repeating a permanently failed action | a failed task is never restarted; one replacement per failed task; replacement must differ |
| LLM bypassing validation | same gates as the original plan + re-validation at append + projector |
| recovery bypassing tool policy | replacement tasks run through the same agent runtime, tool policy and limits |
| secrets in failure events | `safe_message` redaction and truncation |

## Retry vs replan

A **retry** re-runs the same task with the same tool and arguments. NEXUS does **not**
retry. The only retries anywhere are the LLM provider client's own, bounded by
`NEXUS_LLM_MAX_RETRIES`.

A **replan** is a new, validated proposal for a *different* route. It creates new tasks
with new ids, informed by the classified failure. The failed task and its events remain
part of history. If generic retries are ever needed, they will be a separate, explicit
policy, not replanning.

## Boundary with approvals (not implemented)

Recovery creates tasks only through the normal validation → `TaskCreated` → scheduler →
agent runtime → tool policy path. It has no side channel, so a future policy/approval
layer placed on that path (e.g. before a task or tool call that performs an
irreversible action) also intercepts every replan. Recovery must not be given a way
around it.

## Limitations

- Real LLM replanning has not been verified; every test and the demo use `FakeLLMProvider`.
- Fingerprints are syntactic, not semantic.
- Recovery assumes one scheduler per run. With several concurrent schedulers,
  appends stay safe (optimistic concurrency and projector checks), but a replan that
  races another may be rejected instead of merged.
- A replacement may replace only one failed task, and blocked historical tasks cannot
  be replaced directly (their root failure is replaced instead).
