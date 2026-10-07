# NEXUS LLM Routing (heterogeneous models)

NEXUS can use different LLM providers and models for different roles. Agents stay
provider-agnostic: they call the one `LLMProvider` interface; a `RoutingProvider`
(`app/llm/routing.py`) sends each request to the provider configured for its role.

```
planner / agents / replanner / verifier
   ↓  LLMRequest(purpose, metadata)        built by NEXUS code
RoutingProvider.role_for(request)          deterministic: purpose + code-set metadata
   ↓
role's provider (configuration)            existing adapters: OpenAIProvider (Gemini /
   ↓                                       OpenRouter OpenAI-compatible; Gemini / OpenRouter / Groq), or the default
LLM API                                    provider (NEXUS_LLM_PROVIDER, e.g. Navigate Labs)
```

- **Routing is configuration**, never a model decision: `NEXUS_<ROLE>_PROVIDER` and
  `NEXUS_<ROLE>_MODEL`. A request has no field that could name a provider or model, and
  the role comes only from `purpose` (`planner`, `replanner`, `verifier`,
  `agent:<type>`) and the `conflict_id` metadata the agent runtime sets from the task's
  own state (metadata is never sent to a provider). Model output cannot change routing.
- **Agents are unchanged.** The only agent-side change is that a conflict-resolution task
  tags its requests with `conflict_id`, so it can have its own route.
- **Model assignments can change without code changes.**
- **Backward compatible.** With no role variables set, `build_provider` returns exactly
  the provider it returned before (no router). Unrouted roles use the default provider.
  `NEXUS_<ROLE>_PROVIDER=default` routes a role to it explicitly.
- **Free-tier models** are selected for development and demo cost control.

## Roles

| role | requests | needs |
|---|---|---|
| planner | `planner` | task-graph reasoning, strict schema adherence |
| researcher | `agent:researcher` | tool use via structured JSON steps, provenance discipline |
| analyst | `agent:analyst` | reasoning over facts, structured output |
| specialist | `agent:specialist` | straightforward deliverables, low latency |
| conflict_resolver | `agent:researcher` with `conflict_id` | evidence comparison, tool use |
| replanner | `replanner` | failure analysis, graph edits |
| verifier | `verifier` | independent, evidence-based judgment |

NEXUS agents use tools through **structured output** (a JSON step validated by NEXUS),
not native function calling, so the key capability is reliable JSON-schema output
(`response_format` / `structured_outputs`).

## Current assignment (revised 2026-10-06)

Free at the time this configuration was selected; availability and rate limits are
provider-controlled.

| role | provider | model | status / why |
|---|---|---|---|
| planner | groq | `openai/gpt-oss-120b` | **real-tested in NEXUS**: strict output, task graph accepted |
| researcher | openrouter | `nvidia/nemotron-3-super-120b-a12b:free` | **real-tested in NEXUS**: strict output, valid `http_fetch` step, provenance accepted |
| analyst | gemini | `gemini-3.5-flash-lite` | **real-tested in NEXUS** (2026-10-06): strict output, calculator `call_tool` step then finish, tool provenance accepted |
| specialist | gemini | `gemini-3.5-flash-lite` | **real-tested in NEXUS**: strict output, direct finish |
| conflict_resolver | gemini | `gemini-3.5-flash-lite` | **real-tested in NEXUS** (2026-10-06): one `http_fetch` of an undisputed source, no repeated call, tool-derived claim on the exact subject/attribute, original facts kept, ConflictResolved; same model as the analyst and specialist; the verdict is deterministic and needs tool evidence independent of the disputed sources |
| replanner | groq | `openai/gpt-oss-20b` | **real-tested in NEXUS**: strict output, replacement task accepted and executed; spreads load (Groq limits are per model) |
| verifier | groq | `openai/gpt-oss-120b` | **real-tested in NEXUS**: strict output, verdict accepted, failed verdict fed recovery; different from every evidence-producing model |

Groq is reached through the same `OpenAIProvider` (strict `json_schema`; no native tool
calling: agents return AgentStep JSON and NEXUS runs the tools). Groq free plan, per
model: 30 requests/min, 1,000/day, 8,000 tokens/min, 200,000 tokens/day.

Replaced: Gemini 3.8 Flash and 3.5 Flash (503 "high demand" in real NEXUS tests);
OpenRouter `qwen/qwen3.8-27b:free` (analyst, conflict resolver): no longer listed by
OpenRouter, requests return 404 (2026-10-06; only the paid `qwen/qwen3.8-27b` remains).
Rejected for tool-using roles (real NEXUS tests): Groq `openai/gpt-oss-*` emits a native
function call even though NEXUS sends no tools, which Groq rejects (`tool_use_failed`);
Groq `qwen/qwen3.8-27b` returned an empty `call_tool` step (`tool_call: null`), although
the same model produces valid steps through OpenRouter. Both remain usable for roles
without tool steps (planner, replanner, verifier). Not used as conflict resolver:
Nemotron 3 Super requested the same `http_fetch` again instead of finishing (twice).
Not used: Groq `llama-3.3-70b-versatile` (Enterprise tier; no strict structured output),
GitHub Models (retired 2026-07-30), Cerebras (no permanent free tier; payment method
required), `openrouter/free` (rotating), OpenRouter free models without structured
output, preview / stealth models.

## Configuration

```
NEXUS_GEMINI_API_KEY=            # required only if a role uses gemini
NEXUS_GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
NEXUS_OPENROUTER_API_KEY=        # required only if a role uses openrouter
NEXUS_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
NEXUS_GROQ_API_KEY=              # required only if a role uses groq
NEXUS_GROQ_BASE_URL=https://api.groq.com/openai/v1
NEXUS_<ROLE>_PROVIDER=default | gemini | openrouter | groq
NEXUS_<ROLE>_MODEL=<model id>    # required for gemini / openrouter / groq
```

`<ROLE>`: `PLANNER`, `RESEARCHER`, `ANALYST`, `SPECIALIST`, `CONFLICT_RESOLVER`,
`REPLANNER`, `VERIFIER`. The routed adapters share the OpenAI adapter settings
(`NEXUS_OPENAI_RESPONSE_FORMAT`, `NEXUS_OPENAI_STRICT_SCHEMA`, `NEXUS_LLM_MAX_TOKENS`, …).

Validation (Settings): provider must be one of the three; a model without a provider, or
gemini/openrouter without a model, is rejected; base URLs must be `https://`. An unused
provider's key is not required. If a routed provider's key is missing, the app still
starts with a warning naming the variable, and that role's requests fail with
`LLMConfigurationError` before anything is sent. Messages name settings, never values;
keys are `SecretStr` and never logged.

## Limits and caveats

- OpenRouter `:free`: 20 requests/min and 50 requests/day (1,000/day after $10 of
  purchased credits). A full `/execute` run makes several calls; plan small runs.
- Gemini free tier: limits per model are shown in AI Studio; free-tier content may be
  used to improve Google's products.
- Groq free plan: 8,000 tokens/min per model is the binding limit. NEXUS agent calls are
  ~1.5-2.5k input tokens with `max_tokens=4096`; whether Groq counts the requested
  `max_tokens` against the per-minute budget is not documented. gpt-oss models also spend
  output tokens on reasoning.
- Groq's strict validator rejects an `anyOf` with both `integer` and `number`
  (`integer_number_overlap`); the adapter's wire schema keeps only `number` there (same
  values; NEXUS's models unchanged).
- The analyst, the conflict resolver and the specialist share one Gemini model, so they
  share its free-tier per-model limits; the researcher keeps the OpenRouter free quota.
- Each role is real-tested on its own; a full end-to-end run on this routing is not.
