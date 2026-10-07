# NEXUS Policy and Approval Model (Phase 8)

Some actions have consequences: placing an order, writing to an external system. NEXUS
decides whether such an action may run with **deterministic policy**, asks a **human**
when the policy requires it, and only then lets the **executor** perform it. A model can
propose an action; it can never classify, approve or execute it on its own authority.

```
LLM proposes        planner / agents / replanner (untrusted proposals)
policy decides      PolicyEngine: tool metadata + deployment policy + run history  [code]
human approves      approval endpoints, only when the policy requires it            [human]
executor performs   ToolExecutor, the only path to a tool, re-checks authorization  [code]
verifier verifies   Phase 7 checkpoint: authorized AND executed AND requirements met [code (+LLM)]
```

## Risk categories

Every registered tool declares one (`ToolDefinition.category`, required; there is no
default and no model input):

| category | meaning | built-in tools | default outcome |
|---|---|---|---|
| `read_only` | pure computation / reading run data | calculator, python_analysis | ALLOW (always) |
| `network_read` | reads from the network, no side effects | web_search, http_fetch (GET) | ALLOW |
| `reversible_write` | changes something that can be undone | (none in production) | ALLOW |
| `irreversible` | external side effect that cannot be undone | (none in production) | APPROVAL_REQUIRED |

Production NEXUS has no side-effecting tool yet; tests use `FakeSideEffectTool`
(`place_order`: irreversible, `save_draft`: reversible), which counts its executions.

## Policy engine

`PolicyEngine.evaluate(ActionRequest, ToolDefinition | None, rejected_fingerprints) ->
PolicyDecision` (app/policy/engine.py). No LLM, no I/O, deterministic. The request has
only a task id, a tool name and arguments (extra fields are rejected); the category is
looked up from the registry. Rules, first match wins:

1. tool not registered → DENY `unknown_tool`
2. tool on `NEXUS_POLICY_DENIED_TOOLS` → DENY `denied_tool`
3. category configured to deny → DENY `category:<c>`
4. identical action (tool + canonical arguments) rejected by a human earlier in the run →
   DENY `previously_rejected` (prevents a rejection → re-request loop)
5. tool on `NEXUS_POLICY_APPROVAL_TOOLS` → APPROVAL_REQUIRED `approval_tool`
6. the category's configured outcome → `category:<c>`

`PolicyDecision` is structured: task id, tool, `action_fingerprint`, `category`,
`outcome` (`allow` / `approval_required` / `deny`), `rule`, `reason`. The time and
context are the event envelope's.

Configuration: `NEXUS_POLICY_NETWORK_READ` (allow), `NEXUS_POLICY_REVERSIBLE_WRITE`
(allow), `NEXUS_POLICY_IRREVERSIBLE` (approval_required), `NEXUS_POLICY_DENIED_TOOLS`,
`NEXUS_POLICY_APPROVAL_TOOLS`. `read_only` is always allowed.

## Where the gate is enforced

**1. In the tool executor (every tool call).** `ToolExecutor.execute` is the only path to a
tool's `execute`. Order: lookup → permission → **policy gate** → argument validation →
execution. For agents' tool loops only ALLOW executes: APPROVAL_REQUIRED fails as
`approval_required` and DENY as `policy_denied`, with nothing executed (`ToolFailed`; the
task fails as `POLICY_FAILURE`). Agents are only shown ALLOW tools; a model that names a
gated tool anyway, or adds "approved": true / "category": "read_only" to its arguments,
is still refused before execution.

**2. Before an action task starts.** An *action task* (`TaskCreated.action`: agent
`action_executor`, type `action`, an `ActionSpec` with tool, arguments and intent) is a
predeclared side effect. It is created by a user or caller (API `/tasks`) or proposed by
the planner (Phase 9; never by the replanner). When it is READY the scheduler calls `PolicyManager.gate`, which
records the decision **before anything runs**:

```
READY action task
   ↓ PolicyManager.gate              PolicyEvaluated (+ ApprovalRequested), atomic
   ├── allow              → claim → execute the exact action
   ├── approval_required  → stays READY (waiting for approval); nothing executes
   │        ├── granted   → next scheduler pass: claim → execute the exact action
   │        └── rejected  → claim → TaskFailed(POLICY_FAILURE, approval_rejected)
   └── deny               → claim → TaskFailed(POLICY_FAILURE, action_denied)
```

Execution (`ActionTaskExecutor`) runs exactly the `ActionSpec` through the
`ToolExecutor` with an `Authorization` built by NEXUS from recorded state (the decision,
plus the granted approval). The executor checks that it names this task and exactly this
tool and arguments, and re-evaluates the policy, so a tool denied since the approval is
still refused. The result is recorded like any task's: `ToolCalled`, `ToolSucceeded`, a
tool-derived fact, `TaskCompleted`.

No new task status was needed: "waiting for approval" is a READY action task with a
pending approval (`action_status = awaiting_approval`, `ScheduleReport.approvals_pending`).

## Approval lifecycle (event-sourced)

| event | written by | projector rules |
|---|---|---|
| `PolicyEvaluated(decision)` | PolicyManager | action task, READY, in its own task context, one decision per task, about its exact action; an action identical to a rejected one can only be DENY |
| `ApprovalRequested(approval_id, task_id, action, description)` | PolicyManager | the decision is APPROVAL_REQUIRED; one approval per task; the action matches |
| `ApprovalGranted(approval_id, task_id, actor, reason)` | ApprovalManager | exists in this run, belongs to that task, still pending |
| `ApprovalRejected(approval_id, task_id, actor, reason)` | ApprovalManager | same |

Further rules: an action task starts only once the gate decided (allowed / approved, or
denied / rejected to fail it); it completes only if allowed or approved and after a
successful call of exactly its action; tool calls in its context must be exactly that
action; a refused action fails with `action_denied` / `approval_rejected`, and only a
refused action uses those error types. Approval events without `task_id` (the Phase 1
form) are recorded only, and cannot touch a Phase 8 approval.

State: `RunState.policy[task_id]` (decision, sequence, time) and
`RunState.approvals[approval_id]` (task, action, description, status
pending/granted/rejected, requested/decided time, actor, reason). `app.policy.rules` derives
`action_status`, `authorization_for`, `rejected_fingerprints` and `completion_blockers`.

**APPROVED ≠ EXECUTED ≠ VERIFIED.** Granting writes one event and executes nothing.
Execution is the action task completing with its tool call. Verification is a Phase 7
checkpoint; its `actions` check requires each covered action to be authorized *and*
executed exactly as approved.

## Human approval API

| method | path | |
|---|---|---|
| `GET` | `/api/v1/runs/{id}/approvals` | approvals (pending first) with the action (tool, arguments, intent), risk category, rule and reason, task and action status, and the results it builds on (dependency summaries and facts) |
| `POST` | `/api/v1/runs/{id}/approvals/{approval_id}/approve` | `{actor, reason}` → `ApprovalGranted`. 404 unknown (in this run), 409 already decided or task no longer waiting, 409 run not active |
| `POST` | `/api/v1/runs/{id}/approvals/{approval_id}/reject` | `{actor, reason}` → `ApprovalRejected` |

These endpoints only record the decision. The action runs on the next `/schedule`,
through the gated path. There is no authentication yet: `actor` is recorded as given.
A DENY decision has no approval to grant.

## Run completion gate

`RunCompleted` is accepted by the projector only if `completion_blockers(state)` is empty:

- every task resolves to COMPLETED (a failed task counts only through a completed
  Phase 5 replacement), so no pending or blocked work, no unreplaced refused action;
- no approval is pending;
- the run has a verification checkpoint, and the current attempt of every checkpoint
  passed (`run_verified`).

`RunCompletion.complete_if_ready` (called by the scheduler at the end of a pass, API
`/schedule` with `complete: true`, the default) is its only writer;
`ScheduleReport.completion_blockers` says why a run is not complete.

## Event authority (raw events trust boundary)

`POST /runs/{id}/events` may not append `PolicyEvaluated`, `ApprovalRequested`,
`ApprovalGranted`, `ApprovalRejected`, `VerificationStarted`, `VerificationPassed`,
`VerificationFailed` or `RunCompleted` (403 `privileged_event`, nothing written). Their
writers are the PolicyManager, the ApprovalManager (approval endpoints), the
VerificationManager and RunCompletion. Worker/agent results can only contain facts,
artifacts and tool events (scheduler rule since Phase 3), so an agent cannot approve,
evaluate, verify or complete anything. This is a deterministic writer rule, not
authentication.

## Recovery (Phase 5, unchanged mechanism)

| case | result | recovery |
|---|---|---|
| DENY | never executes; `TaskFailed(POLICY_FAILURE, action_denied)` | PROPAGATE: a denial is never routed around; dependents stay BLOCKED |
| rejected | never executes; `TaskFailed(POLICY_FAILURE, approval_rejected)` | REPLAN allowed (bounded by the replan budget): the replanner may propose another route; it cannot create action tasks, and an identical action is denied (`previously_rejected`) |
| pending | stays READY, nothing executes | none: no automatic timeout (none is configured) |
| granted | normal scheduler execution | as any task |
| agent tool loop hits a gated tool | `ToolFailed(approval_required / policy_denied)` → `POLICY_FAILURE` | PROPAGATE |

## Limitations

- No authentication or roles: anyone who can reach the approval endpoints can approve;
  `actor` is self-declared. The event-authority rule stops forged *events*, not a caller
  using the real endpoints.
- No approval timeout / expiry; a pending approval waits indefinitely.
- (Phase 9) The planner may now *propose* action tasks (schema-closed; validated
  against the tool's input model); the policy engine still decides. An agent still
  cannot turn a mid-task tool call into an approval request.
- Policy is deployment-wide (settings); there is no per-run or per-user policy.
- Semantic (LLM) verification and real-LLM behavior with gated actions are untested.
