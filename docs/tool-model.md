# NEXUS Tool Model

This page describes the controlled tool system as implemented in Phase 4. Code references
are relative to `backend/app/`.

## Summary of what is real

| Tool | Implementation | Available in a deployment | Verified against the real world |
|---|---|---|---|
| `calculator` | Real (safe AST evaluator, offline) | Always | Yes (it has no external dependency) |
| `http_fetch` | Real (httpx, SSRF-guarded) | Only with `NEXUS_HTTP_FETCH_ENABLED=true` | Yes: real HTTPS fetch and real SSRF refusals (`scripts/verify_real_tools.py`) |
| `web_search` | Interface plus `FakeSearchBackend` only | No (no real backend implemented) | No |
| `python_analysis` | Interface plus `FakeSandboxBackend` only | No (no sandbox available) | No; nothing in NEXUS executes model-written code |

The test suite uses fakes and mock transports only and needs no network.

## Trust boundary

```
LLM ──▶ structured AgentStep {action, tool_call{tool_name, arguments}}   (untrusted)
          │ schema validation (Pydantic)
          ▼
ToolSession (per task): limit check, id assignment, trace
          ▼
ToolExecutor: registry lookup → policy (agent_type × tool) → argument validation
          ▼  nothing runs until all three pass
Tool.execute (timeout) → output validation → ToolResult                    (untrusted)
          ▼
agent runtime → ToolCalled / ToolSucceeded / ToolFailed, facts with provenance → events
```

- The LLM can only *request* a tool call. What exists (the registry), who may use it (the
  policy), the argument schema and all limits are fixed by application code and
  configuration. Nothing in LLM output can change them.
- Tools never touch NEXUS state. They get a `ToolContext` (run id, task id, agent type,
  call id) and return a value. The runtime decides what becomes an event.
- **Tool output is untrusted data**, just like LLM output. It reaches the model only as a
  *user* message inside a `<tool_result …>` block:
  - `<` and `>` are escaped, so fetched content cannot close the block or open new tags;
  - the block ends with a note that it is data, not instructions;
  - output over `NEXUS_TOOL_OUTPUT_MAX_CHARS` is truncated in the prompt (events keep the
    full output).

  The system prompt never contains tool output, and it is identical on every turn.

## Tool interface (`tools/base.py`, `tools/schemas.py`)

- **`ToolDefinition`** holds:
  - `name`, `description` and `capabilities` (both shown to the LLM);
  - `input_model` and `output_model` (Pydantic; `input_schema` / `output_schema` are
    derived from them);
  - `risk_level` (low / medium / high);
  - `category` (Phase 8, required): `read_only`, `network_read`, `reversible_write` or
    `irreversible`. The authoritative input to the policy gate (see
    [policy-model.md](policy-model.md)); the executor refuses gated calls before execution;
  - `timeout_seconds`;
  - `fake`.
- **`Tool.execute(arguments, context) -> output model`**. It raises a `ToolError`
  subclass on failure.
- **`ToolCall`** is `{tool_name, arguments}`, with extra fields forbidden.
- **`ToolResult`** holds `success`, `output`, `metadata` (`duration_ms`, `fake`,
  `risk_level`), `error` and `error_type`.

## Registry and permissions (`tools/registry.py`, `tools/policy.py`, `tools/factory.py`)

`ToolRegistry` supports register (duplicate names are rejected), get (unknown names raise
`UnknownToolError`) and list. `build_tool_registry(settings)` builds the deployment's
registry, and never registers fakes.

The policy is deterministic: `agent_type` + `tool_name` → allowed or denied.

| agent_type | Allowed tools |
|---|---|
| planner | none |
| researcher | `web_search`, `http_fetch` |
| analyst | `calculator`, `python_analysis` |
| specialist | `NEXUS_SPECIALIST_TOOLS` (default `["calculator"]`) |

An agent sees, and can call, only tools that are **both registered and allowed**.
- The step schema sent to the LLM enumerates only those tools.
- A request for anything else still reaches the executor, where it fails with
  `unauthorized` or `unknown_tool` before execution. Tests cover this.

## Tool call validation and execution (`tools/executor.py`, `agents/tooling.py`)

Checks, in order, all before any execution:
1. The LLM step parses as `AgentStep`. If not, the agent's output is malformed, the task
   fails, and no tool events are written.
2. Per-task call limit (`NEXUS_MAX_TOOL_CALLS_PER_TASK`, default 5). The call that would
   exceed it is not executed, and the task fails with
   `tool-call limit of N per task reached`.
3. The tool name is registered (`unknown_tool`).
4. The agent is authorized (`unauthorized`).
5. The arguments validate against the tool's input model (`invalid_arguments`, which
   includes length and count limits).

Execution then runs under the tool's timeout (`timeout`), and the output is validated
against its output model (`invalid_output`). Other `error_type` values:
- `network_error`, `http_error`, `size_limit`, `ssrf_blocked`, `execution_error`;
- `unavailable`;
- `interrupted`: the task ended while the tool was running.

**Failure policy:** the first failed tool call ends the task (`TaskFailed`, with the tool
call id, error type and message). Nothing is retried and no alternative is tried; those
belong to later phases.

The agent loop is bounded: at most N tool calls and N+1 LLM turns per task.

## Events

| Event | When | Payload |
|---|---|---|
| `ToolCalled` | Every tool request the runtime receives, including rejected ones | `tool_call_id` (`<task_id>.t<n>`), `tool_name`, `arguments` |
| `ToolSucceeded` | The call completed | `result` (the full output), `metadata` (`duration_ms`, `fake`, `risk_level`, `completed_at`) |
| `ToolFailed` | Validation, authorization or execution failed, or the task ended mid-call | `error`, `error_type`, `metadata` |

The scheduler records tool events atomically with the task outcome:
- with `TaskCompleted` on success;
- with `TaskFailed` on failure, so failed tool use is never hidden.

Executors may emit only tool events and (on success) `FactAdded` / `ArtifactAdded`.
The projector keeps `RunState.tool_calls`:
- a call is created by `ToolCalled`;
- it is closed exactly once, by `ToolSucceeded` or `ToolFailed`.

## Provenance

`FactAdded.provenance` (`Provenance`) has:
- `kind`: `model_knowledge`, `tool_output`, `task_context` or `user_provided`;
- `tool_name`, `tool_call_id`, `source`, `retrieved_at`;
- `fake`.

The runtime builds it from its own tool trace, not from the LLM:
- A fact may claim `tool_output` only by citing a **successful tool call of the same
  task**.
- Its optional `source_url` must appear in that call's output: a search result URL, or
  the fetch's URL or final URL.
- `source` then defaults to the fetch's final URL, or to `calculator: <expression>` /
  `web_search: <query>`.
- `retrieved_at` is when the tool call completed.
- `fake` is copied from the tool, so a fact built from fake search results says so.

A fact that cites a tool call without basis `tool_output`, or cites an unknown or failed
call or a foreign URL, fails the task with `invalid provenance`. The same applies to
evidence citing a tool call. The facts are never silently downgraded.

Model-only facts get `kind: model_knowledge` and no tool fields, so they are always
distinguishable from tool-derived facts. Facts recorded before Phase 4 have
`provenance: null`.

## SSRF protection (`tools/network.py`, `tools/http_fetch.py`)

A URL is fetched only if **all** of these hold:
1. The scheme is `http` or `https`; no `user:password@`; a host is present.
2. The port is the scheme's default (80 or 443).
3. The hostname is not blocked:
   - `localhost`, `*.localhost`;
   - `*.local`, `*.internal`, `*.localdomain`, `*.home.arpa`, `*.arpa`;
   - `metadata`, `metadata.google.internal`, `metadata.goog`, `instance-data`,
     `instance-data.ec2.internal`.
4. NEXUS resolves the hostname itself, and **every** resolved address must be globally
   routable. One bad record rejects the host. Refused:
   - loopback, and private ranges (RFC 1918, fc00::/7);
   - link-local (169.254.0.0/16, which includes 169.254.169.254, and fe80::/10);
   - CGNAT (100.64.0.0/10, which includes 100.100.100.200);
   - unspecified, multicast, reserved and documentation ranges, and anything with
     `is_global == False`.

   IPv6 addresses that embed an IPv4 address (IPv4-mapped, 6to4, Teredo, NAT64) are also
   checked against the embedded address.

Then:
- **IP pinning.** The request is sent to the validated IP, with a `Host` header and TLS
  SNI set to the hostname. The HTTP client never resolves the name again, so DNS
  rebinding cannot swap the destination. TLS still verifies the certificate for the
  hostname.
- **Redirects** are followed manually, at most 3, and each hop is re-checked from step 1.
- **GET only.** Only the `Accept` and `Accept-Language` request headers can be set.
  Environment proxies are ignored, and a new client is used per fetch.
- **Limits:**
  - per-request timeout at most `NEXUS_HTTP_FETCH_MAX_TIMEOUT_SECONDS` (15), plus the
    tool timeout;
  - body at most `NEXUS_HTTP_FETCH_MAX_BYTES` (256 KiB), counted after decompression, and
    a larger `Content-Length` is refused up front;
  - status ≥ 400 is an error;
  - non-text content types return `content: null`.

Known limits: HTTP to public addresses is allowed (not only HTTPS), and there is no
domain allowlist or rate limit.

## Python / data analysis (`tools/python_analysis.py`)

LLM-written code must never run in the backend process. It should run only in a sandbox
that provides:
- a strict time limit;
- memory and output limits;
- no network;
- no host filesystem;
- no process spawning on the host.

This environment (a Windows host with no container runtime) has no such sandbox, and a
plain subprocess would not qualify. So there is no real backend and the tool is not
registered in deployments. `FakeSandboxBackend` returns scripted output and never executes
the code it receives. A test also scans `app/tools` for `eval`, `exec`, `compile`,
`__import__`, and imports of `subprocess`, `os`, `multiprocessing`, `importlib`, `ctypes`,
`pty` and `shutil`.

## Calculator (`tools/calculator.py`)

`ast.parse(mode="eval")`, then a walk over an explicit whitelist:
- numeric literals (bool is excluded);
- `+ - * / // % **` and unary `+ -`;
- the constants `pi` and `e`;
- calls to `abs`, `round`, `min`, `max`, `sqrt`, `log`, `log10`, `exp`, `floor`, `ceil`,
  with positional arguments only.

Everything else is rejected before evaluation: names, attributes, subscripts, strings,
lambdas, comprehensions, walrus, keyword or starred arguments. Limits:
- 500 characters and 200 AST nodes;
- exponent at most 1000;
- |result| at most 1e100;
- the result must be finite and real.

`eval` and `exec` are never used.

## Fakes (`tools/fakes.py`)

`FakeCalculator`, `FakeSearchBackend`, `FakeHTTPFetch` and `FakeSandboxBackend` are
deterministic and perform no I/O. They are identifiable:
- `definition.fake=True`, which shows in `ToolSucceeded.metadata.fake` and in
  `Provenance.fake`;
- their descriptions say FAKE;
- fake URLs use the reserved `.invalid` top-level domain.

`FakeHTTPFetch` applies the same static URL checks as the real fetcher.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NEXUS_MAX_TOOL_CALLS_PER_TASK` | 5 | Tool-call limit per task; 0 disables tools |
| `NEXUS_HTTP_FETCH_ENABLED` | false | Register the real `http_fetch` tool |
| `NEXUS_HTTP_FETCH_MAX_BYTES` | 262144 | Response size limit |
| `NEXUS_HTTP_FETCH_MAX_TIMEOUT_SECONDS` | 15 | Upper bound on the per-request timeout |
| `NEXUS_SPECIALIST_TOOLS` | `["calculator"]` | Specialist's tools (JSON list) |
| `NEXUS_TOOL_OUTPUT_MAX_CHARS` | 20000 | Tool output shown to the LLM (events keep all) |

There is no public tool-execution API. Tools are reachable only through the agent runtime.
