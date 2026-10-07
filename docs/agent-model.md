# NEXUS Agent Model

This page describes the planner, agents and LLM provider layer (Phase 3) and how agents use
tools (Phase 4; details in [tool-model.md](tool-model.md)).
Code references are relative to `backend/app/`.

**Status of the real LLM:** an Anthropic adapter (`llm/anthropic_provider.py`) and an
OpenAI-compatible adapter (`llm/openai_provider.py`) exist, but neither has **been run
against a real API** yet. `scripts/smoke_llm.py` is the intended first real check. Every test, and the end-to-end scenario, uses the deterministic fake provider.

## Trust boundary

| LLM decides (untrusted proposals) | Deterministic code decides (authority) |
|---|---|
| How to decompose a goal into tasks | Whether the plan is accepted (schema, policy, graph checks) |
| Task types, agent types, dependencies, descriptions | Creating tasks (`TaskCreated` events) |
| An agent's summary, facts, evidence, artifacts | Which results are recorded, as which events |
| | When tasks run, and how many run at once (the scheduler) |
| | Event persistence, state projection, run status |

LLM output never reaches the database directly. Planner output becomes tasks only after
validation. Agent output becomes events only through the runtime and the scheduler. Those
allow just the result event types (`FactAdded`, `ArtifactAdded`) and the tool events
(`ToolCalled`, `ToolSucceeded`, `ToolFailed`), and still validate them through the
projector.

Planner output cannot express executable instructions. The plan schema has no field for
code, shell commands, URLs, queries or file paths, and extra fields are rejected.
Descriptions are not keyword-filtered; the protection is structural. An agent can act on
the outside world only through the controlled tool layer (Phase 4). There, the
application decides which tools exist, which agent may use which tool, the argument
schemas and the limits.

## LLM provider abstraction (`llm/`)

- **`LLMProvider.generate(LLMRequest) -> LLMResponse`** (`llm/base.py`). A request has:
  - a `system` prompt and `messages`;
  - the required `output_schema` (JSON Schema) and `max_tokens`;
  - `purpose` (for example `planner` or `agent:researcher`) and `metadata`. These two are
    caller labels and are never sent to the provider.

  The response carries the parsed JSON object (`data`), the provider, the model and token
  usage.
- Providers raise NEXUS errors, never vendor exceptions:
  - `LLMTimeoutError` (504);
  - `LLMResponseError` (502, unusable output: refusal, truncation, non-JSON, non-object);
  - `LLMError` (502, anything else).
- **`strict_json_schema(model)`** turns a Pydantic model into the subset structured outputs
  accept: `additionalProperties: false` on every object, and `minLength`, `pattern`,
  `maxItems` and similar keywords removed. The removed constraints are still enforced,
  because callers validate every response with the full Pydantic model.
- **`AnthropicProvider`**:
  - uses the official `anthropic` SDK (1.8.0) with `client.beta.messages.create`;
  - requests structured output via `output_config.format` (`json_schema`);
  - enables server-side refusal fallbacks (`fallbacks="default"`, beta
    `server-side-fallback-2026-07-01`);
  - bounds each call with the SDK timeout (`NEXUS_LLM_TIMEOUT_SECONDS`) and retries
    (`NEXUS_LLM_MAX_RETRIES`);
  - rejects refusals, `max_tokens` truncation and non-JSON output.

  The default model is `claude-opus-5` (`NEXUS_LLM_MODEL`).
- **`OpenAIProvider`** (`llm/openai_provider.py`), for OpenAI or any OpenAI-compatible
  endpoint:
  - uses the official `openai` SDK (`AsyncOpenAI(api_key=..., base_url=...)`) with the
    Chat Completions API (`chat.completions.create`), which compatible servers implement;
  - sends `system` as the first message, then the user/assistant messages;
  - structured output via `response_format` `json_schema`, **strict** by default (`auto`):
    - a schema that already meets OpenAI's strict rules (root object, every property
      required, `additionalProperties: false`, supported keywords only;
      `is_strict_compatible`) is sent unchanged: the planner and replanner;
    - otherwise a strict *wire* schema is derived (`strict_wire_schema`): every property
      becomes required, and an optional property that did not accept null becomes
      `anyOf: [<original>, null]`. Nothing else changes. This covers agent reports,
      tool-loop steps and tool arguments (e.g. `http_fetch`);
    - on the response, `strip_wire_nulls` removes a null only where the original schema
      made the property optional *and* did not accept null (so the null can only mean
      "absent"). Nulls the original schema allows are kept, nothing is ever added, and
      ambiguous `anyOf` matches are left unchanged. NEXUS's Pydantic validation then runs
      exactly as before (e.g. `minLength` limits dropped from the wire schema are still
      enforced);
    - a schema that cannot be made strict (e.g. a free-form object) is sent non-strict.
    `true` behaves like `auto` but keeps strict even for such schemas; `false` sends every
    schema unchanged and non-strict.
    `json_object` mode is available for servers without schema support, with the schema
    added to the system prompt;
  - sends the output cap as `max_tokens` or, if configured, `max_completion_tokens`;
  - accepts a response wrapped in a single code fence; falls back to the configured model
    id when the server returns none, and tolerates missing `usage`;
  - rejects `length` truncation, `content_filter`/refusals, empty or non-JSON output;
    maps SDK timeout/status/connection errors to `LLMTimeoutError`/`LLMError`.

  The planner, agents and replanner are unchanged: they see only `LLMProvider`.
- **`FakeLLMProvider`** (`llm/fake.py`): deterministic replies chosen by `purpose`. A reply
  can be data, an error, or a block on an `asyncio.Event`. It records requests and peak
  concurrency. It is used by all tests.
- **Configuration.** `NEXUS_LLM_PROVIDER=anthropic` or `openai` enables an adapter. Credentials come
  from `ANTHROPIC_API_KEY` in the environment or `.env`; otherwise the SDK resolves its
  own. The default, `none`, disables every LLM-backed endpoint (503
  `llm_not_configured`). No key is stored in source code.

## Agent interface (`agents/base.py`, `agents/result.py`)

```
AgentContext (TaskContext, read-only)  ──▶  Agent.run()  ──▶  AgentResult
```

- `Agent.run(context, tools=None) -> AgentResult`. `tools` is the per-task `ToolSession`,
  the agent's only route to tools. Agents get no database, session, repository or
  event-store access. A test scans `agents/`, `llm/` and `tools/` and fails if any of them
  imports persistence, services, ORM models, the API or SQLAlchemy.
- **`AgentReport`** is the schema the LLM must fill:
  - `success`;
  - `summary`;
  - `facts[]` (at most 20 short statements, each with a `basis`, plus `tool_call_id` and
    `source_url` for tool-derived facts);
  - `evidence[]` (at most 20; each item's `source` is `task_context` with a reference to a
    dependency task or fact, `model_knowledge`, or `tool_output` with the tool call id);
  - `artifacts[]` (at most 5 text documents: plain text, Markdown or JSON);
  - `error`, which is required exactly when `success` is false.

  Extra fields are rejected.
- **`AgentResult`** = `AgentReport` plus `metadata` (agent type, provider, model, token
  usage). The metadata is set by code, not by the LLM.

## Agents and registry (`agents/registry.py`, `agents/reasoning.py`, `agents/planner.py`)

| agent_type | Accepts task types | Role |
|---|---|---|
| `planner` | none (does not execute tasks) | Turns a goal into a task-graph proposal |
| `researcher` | `research` | Gathers and organizes relevant information (tools: `web_search`, `http_fetch` if available) |
| `analyst` | `analysis` | Compares and evaluates results of its dependency tasks (tools: `calculator`, `python_analysis` if available) |
| `specialist` | `domain_task` | Applies domain expertise to produce a focused deliverable (tools: `NEXUS_SPECIALIST_TOOLS`) |

Researcher, analyst and specialist are one class (`ReasoningAgent`) with role-specific
instructions. **Capabilities are explicit:**
- an agent can use only the tools available to it (registered and allowed; see below);
- otherwise it reasons over its task context plus the model's general knowledge, with no
  web, API, file or code access;
- the prompt lists its tools and limits, forbids claiming any search or verification it
  did not do with a tool in this task, and requires evidence and facts to be labelled with
  their source.

`AgentRegistry.resolve(agent_type)` returns the task agent, or raises
`UnknownAgentTypeError` for unknown types and for `planner`.

## Planner and plan validation

Phase 9: when side-effecting tools are registered, the planner may also propose
`actions` (tool + arguments + dependencies; no category / approval / policy field). They
are validated against the tool's input model and become Phase 8 action tasks, which the
policy gate decides on. See [orchestration-model.md](orchestration-model.md).


`PlannerAgent.plan(goal, constraints, max_tasks)` sends the goal and constraints, marked as
user data, together with the available agents and rules. It requests `PlannerOutput`
(`tasks[]` of `id`, `title`, `task_type`, `agent_type`, `description`, `dependencies`),
with `agent_type` and `task_type` narrowed to enums of the registered values.

Intent first (Phase 11): the planner decides before planning (`decision`, required in the
schema). `plan` is for objectives whose requested work is clear, even if short.
`needs_clarification` comes with a `clarification`, whose `reason` is either:
- `underspecified`: a statement, wish or preference that does not say what work is wanted;
- `not_a_request`: conversational input.

The `clarification` also holds one neutral `question` and a short list of what is
`missing`. The prompt tells the planner not to turn statements into research or
deliverables, not to invent missing constraints or outputs, and to prefer a clarification
over a plan built on assumptions.

The schema gate rejects a clarification that carries tasks or actions, and a plan that
carries a clarification. `PlanningService` then records `ClarificationRequested`
(privileged; only for a run without tasks) instead of tasks. The run becomes
`needs_clarification`, a terminal status that is neither completed nor failed, so the
projector refuses anything after it. No task, scheduler pass, agent, tool, recovery,
verification or artifact can follow. `/plan`, `/result` and `/execute` expose the
clarification. There is no way to answer it yet (no chat): a future layer would start a
new run with the clarified objective.

Decomposition guidance (the prompt, not a gate): the planner is told to find the
independent units of work in the goal (separate items, sources, places or questions) and
give each its own task with no dependency between them; to add a dependency only when a
task actually requires another task's output; and to combine independent results in a task
that depends on exactly those tasks. It is told that more tasks are not better, never to
plan two tasks for the same work, and not to plan a final check (NEXUS adds the
verification checkpoint itself). When agents have tools, it is also told the per-task
tool-call limit (`NEXUS_MAX_TOOL_CALLS_PER_TASK`). Independent tasks then run concurrently
as instances of the same roles; there is no fixed graph shape and no role per instance.
`tests/test_parallel_swarm.py` covers four concurrent branches converging on one analyst,
the sequential control case, a failed branch replaced without rerunning the others, and a
conflict between two branches.

The output then passes three deterministic gates. Any failure raises `PlanRejectedError`
(422 `plan_rejected`, with `stage` set to `schema`, `policy` or `graph`). Nothing is
repaired and nothing is written.

1. **Schema:** full Pydantic validation. That covers required fields, ids matching
   `^[a-z][a-z0-9_]{0,47}$`, title and description lengths, and no extra fields.
2. **Policy:**
   - between 1 and `NEXUS_MAX_PLANNED_TASKS` tasks (default 10);
   - known agent types, excluding `planner`;
   - known task types;
   - each task type accepted by its agent.
3. **Graph:** the Phase 2 `TaskGraph` rules: unique ids, no self-dependencies, no missing
   dependencies, no cycles.

**`PlanningService.plan_run`** (`services/planning.py`) runs the planner under
`NEXUS_AGENT_TIMEOUT_SECONDS`. It then records one `TaskCreated` per task, with envelope
`agent_id="planner"`, atomically and in dependency order.
- It refuses runs that already have tasks (409 `plan_exists`; there is no replanning) and
  runs that are completed or failed.
- It writes with the sequence it read before planning, so a concurrent change causes a
  409 instead of a double plan.
- It never executes anything.

## Agent context (`state/context_builder.py`)

`build_task_context(state, task_id)` is a projection of `RunState`, not the event log. It
contains:
- the run id, goal and constraints;
- the task's own brief (id, title, description, task type, agent type);
- the results of its **direct dependencies** only: each dependency's summary plus the facts
  and artifacts that task produced;
- `last_sequence`.

It is a frozen model. The scheduler builds it and passes it to whichever executor runs
the task.

## Runtime: task executor relationship (`agents/runtime.py`)

```
Scheduler ──▶ AgentTaskExecutor ──▶ AgentRegistry ──▶ Agent ──▶ AgentResult
    ▲                                                              │
    └──── TaskExecutionResult (summary, events, evidence, metadata)┘
```

`AgentTaskExecutor` implements the Phase 2 `TaskExecutor` interface, so the scheduler is
unaware of individual agents. For each task it:
1. resolves the agent and checks that the task type is one the agent accepts;
2. runs the agent under a hard `asyncio.wait_for` timeout;
3. re-validates the result;
4. converts it:
   - each fact → `FactAdded` (`<task_id>.f<n>`);
   - each artifact → `ArtifactAdded` (`<task_id>.a<n>`);
   - summary, evidence and metadata → the `TaskCompleted` payload.

**Failures become TaskFailed.** No retries, no alternate providers, no replanning. The
failure cases are:
- an unsupported agent type, or a task type the agent doesn't accept;
- a provider error or provider timeout;
- the agent timeout;
- a malformed result;
- `success: false`.

Dependents of a failed task become BLOCKED (Phase 2 rules).

## Agent → tool relationship (Phase 4)

```
Agent ──AgentStep{call_tool}──▶ ToolSession ──▶ ToolExecutor ──▶ Tool
  ▲                               (limit, id,     (registry, policy,
  └──── <tool_result> as a        trace)          arguments, timeout)
        user message (untrusted data)
```

- The runtime gives each task a `ToolSession`. It exposes only the tools that are
  registered **and** allowed for the agent type, and is the agent's only route to tools.
- An agent with tools answers each turn with an `AgentStep`: either
  `{action: "call_tool", tool_call}` or `{action: "finish", report}`. Its structured-output
  schema offers one `tool_call` variant per available tool.
- The loop is bounded: at most `NEXUS_MAX_TOOL_CALLS_PER_TASK` calls and one more LLM
  turn. A failed tool call, or reaching the limit, ends the task (`TaskFailed`); there are
  no retries.
- Tool results return to the agent as user messages inside escaped `<tool_result>` blocks,
  never in the system prompt. The prompt tells the agent that they are untrusted data.
- Agents without available tools keep the Phase 3 single-report behavior and the
  "no tools" capability statement.
- Facts declare a `basis`. A `tool_output` fact must cite a successful tool call of the
  task, and the runtime, not the LLM, builds its provenance (see tool-model.md).
- The planner's prompt lists each agent's available tools, so plans match real
  capabilities.

## Event relationship

The scheduler records a successful result as a single atomic append of the result events
(`FactAdded` / `ArtifactAdded`) plus `TaskCompleted`. The envelope carries
`task_id` and the agent type as `agent_id`.
- If an executor returns any other event type, nothing it produced is recorded and the
  task fails.
- If the projector rejects the result events (for example a duplicate fact id), the task
  fails with `result events rejected: …`.

Facts and artifacts in `RunState` therefore carry the agent and task that produced them.
A run's full planning and execution history can be rebuilt from its events alone.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_LLM_PROVIDER` | `none` | `anthropic` or `openai` (case-insensitive) enables a real adapter |
| `NEXUS_LLM_MODEL` | `claude-opus-5` | Model id for the adapter; must be set explicitly with `openai` |
| `ANTHROPIC_API_KEY` | unset | Optional; the SDK can also use its own credential sources |
| `OPENAI_API_KEY` | unset | Key for the OpenAI-compatible endpoint (empty = unset) |
| `OPENAI_BASE_URL` | unset | Endpoint, e.g. `https://host/v1`; unset = api.openai.com |
| `NEXUS_OPENAI_RESPONSE_FORMAT` | `json_schema` | `json_schema` or `json_object` |
| `NEXUS_OPENAI_STRICT_SCHEMA` | `auto` | `auto` (strict, tightening schemas where needed), `true`, or `false` (never strict) |
| `NEXUS_OPENAI_MAX_TOKENS_PARAM` | `max_tokens` | or `max_completion_tokens` (some reasoning models) |
| `NEXUS_LLM_TIMEOUT_SECONDS` | 60 | Per-request SDK timeout |
| `NEXUS_LLM_MAX_RETRIES` | 1 | SDK retries on 408/409/429/5xx and connection errors |
| `NEXUS_LLM_MAX_TOKENS` | 16000 | Output cap per call |
| `NEXUS_AGENT_TIMEOUT_SECONDS` | 180 | Hard wall-clock bound per planner or agent call |
| `NEXUS_MAX_PLANNED_TASKS` | 10 | Maximum tasks per plan |

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/runs` | `{goal, constraints?}` |
| `POST` | `/api/v1/runs/{id}/plan` | Run the planner and record the validated graph. Returns tasks, provider and model. |
| `GET` | `/api/v1/runs/{id}/tasks` | Inspect the graph |
| `POST` | `/api/v1/runs/{id}/schedule` | `{executor: "agent"}` (default) runs tasks with agents. `{executor: "scripted", outcomes}` is the Phase 2 scripted executor. |

Errors:

| Status | Code | When |
|---|---|---|
| 503 | `llm_not_configured` | No provider is configured |
| 502 | `llm_error` / `llm_invalid_response` | Provider failure or unusable output |
| 504 | `llm_timeout` | Provider or planner timed out |
| 422 | `plan_rejected` | Plan failed validation |
| 409 | `plan_exists` | The run already has tasks |
