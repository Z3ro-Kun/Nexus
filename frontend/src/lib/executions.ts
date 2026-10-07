/**
 * The /execute requests this browser tab has issued, by run id.
 *
 * Starting execution (one long POST /runs/{id}/execute) is kept separate from observing
 * it (polling, see useRunObserver): the objective page starts it and navigates away, the run
 * page observes it. At most one request per run is in flight from this tab, so execution is
 * never issued twice by accident. A page refresh forgets in-flight requests (the backend
 * keeps running the run); the run page then only observes, and offers a manual resume.
 */

import { useSyncExternalStore } from "react";

import { executeRun } from "../api/runs";
import type { ExecutionResult } from "../types/api";
import { describeError } from "./errors";

export type Execution =
  | { status: "running"; startedAt: number }
  | { status: "returned"; result: ExecutionResult }
  | { status: "error"; message: string };

const executions = new Map<string, Execution>();
const listeners = new Set<() => void>();

function set(runId: string, execution: Execution) {
  executions.set(runId, execution);
  listeners.forEach((listener) => listener());
}

/** Issue POST /runs/{id}/execute unless one is already in flight. Returns false if it was. */
export function startExecution(runId: string): boolean {
  if (executions.get(runId)?.status === "running") return false;
  set(runId, { status: "running", startedAt: Date.now() });
  executeRun(runId).then(
    (result) => set(runId, { status: "returned", result }),
    (error: unknown) => set(runId, { status: "error", message: describeError(error) }),
  );
  return true;
}

export function getExecution(runId: string): Execution | undefined {
  return executions.get(runId);
}

export function useExecution(runId: string): Execution | undefined {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => executions.get(runId),
  );
}

/** Tests only. */
export function resetExecutions() {
  executions.clear();
  listeners.forEach((listener) => listener());
}
