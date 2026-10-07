/**
 * Test-only builders for backend responses (RunState, Event, FinalResult), shaped like the
 * real API. Used with stubFetch to simulate a run that progresses between polls.
 */

import type { FinalResult, NexusEvent, RunPhase, RunState, TaskState, VerificationState } from "../types/api";
import { GOAL, RUN_ID, json, type Handler } from "./fetchMock";

const T0 = Date.parse("2026-10-04T11:27:37Z");
const at = (s: number) => new Date(T0 + s * 1000).toISOString();

export function event(sequence: number, event_type: string, payload: Record<string, unknown> = {}, extra: Partial<NexusEvent> = {}): NexusEvent {
  return { id: `e${sequence}`, run_id: RUN_ID, sequence, event_type, timestamp: at(sequence), agent_id: null, task_id: null, payload, ...extra };
}

export function task(task_id: string, status: TaskState["status"], extra: Partial<TaskState> = {}): TaskState {
  return {
    task_id,
    title: `Title of ${task_id}`,
    description: null,
    task_type: "research",
    agent_type: "researcher",
    dependencies: [],
    status,
    summary: null,
    error: null,
    failure: null,
    replaces: null,
    replaced_by: null,
    conflict_id: null,
    verification: null,
    action: null,
    created_at: at(2),
    started_at: status === "pending" || status === "ready" || status === "blocked" ? null : at(3),
    completed_at: null,
    ...extra,
  };
}

export function verification(status: VerificationState["status"], extra: Partial<VerificationState> = {}): VerificationState {
  return {
    verification_id: "verify.objective",
    checkpoint_id: "verify.objective",
    attempt: 1,
    spec: { objective: GOAL, semantic: true },
    dependencies: ["fetch_com", "fetch_org", "compare"],
    status,
    covered_task_ids: [],
    fact_ids: [],
    artifact_ids: [],
    checks: [],
    semantic: null,
    failed_references: [],
    reason: null,
    replaced_by: null,
    ...extra,
  };
}

export function state(last_sequence: number, extra: Partial<RunState> = {}): RunState {
  return {
    run_id: RUN_ID,
    goal: GOAL,
    constraints: [],
    status: "created",
    tasks: {},
    facts: {},
    artifacts: {},
    tool_calls: {},
    conflicts: {},
    recovery: { replan_count: 0, replan_attempts: 0, history: [] },
    verifications: {},
    approvals: {},
    completion_summary: null,
    failure_reason: null,
    last_sequence,
    updated_at: at(last_sequence),
    ...extra,
  };
}

export function result(phase: RunPhase, extra: Partial<FinalResult> = {}): FinalResult {
  return {
    run_id: RUN_ID,
    objective: GOAL,
    constraints: [],
    phase,
    verified: false,
    completion_summary: null,
    failure_reason: null,
    completion_blockers: [],
    deliverables: [],
    supporting_facts: [],
    tasks: [],
    tool_calls: { total: 0, succeeded: 0, failed: 0, by_tool: {} },
    last_sequence: 0,
    ...extra,
  };
}

/** The three-task graph of a real run: fetch_com ∥ fetch_org → compare → verify.objective. */
export const GRAPH_TASKS = (statuses: Partial<Record<string, TaskState["status"]>> = {}): Record<string, TaskState> => ({
  fetch_com: task("fetch_com", statuses.fetch_com ?? "running"),
  fetch_org: task("fetch_org", statuses.fetch_org ?? "running"),
  compare: task("compare", statuses.compare ?? "pending", { agent_type: "analyst", task_type: "analysis", dependencies: ["fetch_com", "fetch_org"] }),
  "verify.objective": task("verify.objective", statuses["verify.objective"] ?? "pending", {
    agent_type: "verifier",
    task_type: "verification",
    dependencies: ["fetch_com", "fetch_org", "compare"],
    verification: { objective: GOAL },
  }),
});

/**
 * A backend whose run advances one "tick" per poll. `ticks[i]` is the state, result and the
 * full event history at poll i (the last tick repeats). The events route honours
 * after_sequence exactly as the backend does, and records the cursors it was asked for.
 */
export function scriptedRun(ticks: { state: RunState; result: FinalResult; events: NexusEvent[] }[]) {
  let polls = -1;
  const cursors: number[] = [];
  const current = () => ticks[Math.max(0, Math.min(polls, ticks.length - 1))];
  const routes: Record<string, Handler> = {
    [`GET /api/v1/runs/${RUN_ID}/state`]: () => {
      polls += 1;
      return json(current().state);
    },
    [`GET /api/v1/runs/${RUN_ID}/result`]: () => json(current().result),
    [`GET /api/v1/runs/${RUN_ID}/events`]: (_, url) => {
      const after = Number(new URL(url, "http://x").searchParams.get("after_sequence"));
      cursors.push(after);
      return json(current().events.filter((e) => e.sequence > after));
    },
  };
  return { routes, cursors, polls: () => polls + 1 };
}
