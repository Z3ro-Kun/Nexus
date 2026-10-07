import { useCallback, useRef, useState } from "react";

import { createRun } from "../api/runs";
import { describeError } from "../lib/errors";
import { startExecution } from "../lib/executions";
import { rememberRun } from "../lib/history";
import { navigate, runPath } from "../lib/router";

/**
 * Start Run: POST /runs, then issue POST /runs/{id}/execute (once) and open the run page,
 * which observes the execution. One submission at a time.
 */
export function useStartRun() {
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);

  const start = useCallback(async (goal: string) => {
    if (busy.current) return; // no duplicate submission while a request is in flight
    busy.current = true;
    setCreating(true);
    setError(null);
    try {
      const run = await createRun({ goal });
      rememberRun(run.id);
      startExecution(run.id);
      navigate(runPath(run.id));
    } catch (e) {
      setError(describeError(e));
    } finally {
      busy.current = false;
      setCreating(false);
    }
  }, []);

  return { start, creating, error };
}
