/**
 * Run operations. Endpoints (backend app/api/v1/runs.py, prefix /api/v1):
 *   createRun   POST /runs                          RunCreate          -> 201 RunRead
 *   getRun      GET  /runs/{id}                                        -> RunRead
 *   executeRun  POST /runs/{id}/execute             (no body: agents)  -> OrchestrationResult
 *   getState    GET  /runs/{id}/state                                  -> RunState
 *   getEvents   GET  /runs/{id}/events?after_sequence=N                -> Event[] (sequence > N)
 *   getResult   GET  /runs/{id}/result                                 -> FinalResult (incl. phase)
 * /execute is synchronous: it returns when the run completes, fails, waits for an approval
 * or cannot progress (it may take minutes). Calling it again resumes the same run.
 */

import type { ExecutionResult, FinalResult, NexusEvent, Run, RunCreate, RunPhase, RunState, RunStatus } from "../types/api";
import { isRecord, request } from "./client";

const RUN_STATUSES: readonly RunStatus[] = ["created", "completed", "failed", "needs_clarification"];
const RUN_PHASES: readonly RunPhase[] = [
  "created",
  "executing",
  "waiting_for_approval",
  "verifying",
  "blocked",
  "completed",
  "failed",
  "needs_clarification",
];

export function isRun(value: unknown): value is Run {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.goal === "string" &&
    RUN_STATUSES.includes(value.status as RunStatus) &&
    typeof value.last_sequence === "number"
  );
}

export function isFinalResult(value: unknown): value is FinalResult {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    RUN_PHASES.includes(value.phase as RunPhase) &&
    typeof value.verified === "boolean" &&
    Array.isArray(value.deliverables) &&
    Array.isArray(value.supporting_facts) &&
    Array.isArray(value.tasks)
  );
}

export function isExecutionResult(value: unknown): value is ExecutionResult {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    RUN_PHASES.includes(value.phase as RunPhase) &&
    Array.isArray(value.started) &&
    Array.isArray(value.completed) &&
    Array.isArray(value.failed) &&
    Array.isArray(value.approvals_pending) &&
    isRecord(value.result) &&
    typeof value.result.verified === "boolean"
  );
}

export function isRunState(value: unknown): value is RunState {
  return (
    isRecord(value) &&
    typeof value.run_id === "string" &&
    RUN_STATUSES.includes(value.status as RunStatus) &&
    typeof value.last_sequence === "number" &&
    isRecord(value.tasks) &&
    isRecord(value.facts) &&
    isRecord(value.artifacts) &&
    isRecord(value.tool_calls) &&
    isRecord(value.conflicts) &&
    isRecord(value.verifications) &&
    isRecord(value.recovery)
  );
}

function isEvent(value: unknown): value is NexusEvent {
  return (
    isRecord(value) &&
    typeof value.sequence === "number" &&
    typeof value.event_type === "string" &&
    typeof value.timestamp === "string" &&
    isRecord(value.payload)
  );
}

function isEventList(value: unknown): value is NexusEvent[] {
  return Array.isArray(value) && value.every(isEvent);
}

const runPath = (runId: string) => `/runs/${encodeURIComponent(runId)}`;

export function createRun(body: RunCreate, signal?: AbortSignal): Promise<Run> {
  return request("/runs", isRun, { method: "POST", body, signal });
}

export function getRun(runId: string, signal?: AbortSignal): Promise<Run> {
  return request(runPath(runId), isRun, { signal });
}

export function executeRun(runId: string, signal?: AbortSignal): Promise<ExecutionResult> {
  return request(`${runPath(runId)}/execute`, isExecutionResult, { method: "POST", signal });
}

export function getState(runId: string, signal?: AbortSignal): Promise<RunState> {
  return request(`${runPath(runId)}/state`, isRunState, { signal });
}

/** Events with sequence > `afterSequence`, in order. */
export function getEvents(runId: string, afterSequence: number, signal?: AbortSignal): Promise<NexusEvent[]> {
  return request(`${runPath(runId)}/events?after_sequence=${afterSequence}`, isEventList, { signal });
}

export function getResult(runId: string, signal?: AbortSignal): Promise<FinalResult> {
  return request(`${runPath(runId)}/result`, isFinalResult, { signal });
}
