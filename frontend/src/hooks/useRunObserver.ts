import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import { getEvents, getResult, getState } from "../api/runs";
import { describeError } from "../lib/errors";
import { finishing } from "../lib/runView";
import type { FinalResult, NexusEvent, RunPhase, RunState } from "../types/api";

export const POLL_INTERVAL_MS = 2000;
/** Slower polling while nothing is executing (planning elsewhere, waiting for an approval, stalled). */
export const IDLE_POLL_INTERVAL_MS = 6000;

/** Phases in which the run progresses without anyone acting on it. */
const PROGRESSING: ReadonlySet<RunPhase> = new Set(["executing", "verifying"]);

export interface RunObservation {
  state: RunState | null;
  result: FinalResult | null;
  events: NexusEvent[];
  /** The first load failed (nothing to show); `notFound` when the backend says the run does not exist. */
  loadError: string | null;
  notFound: boolean;
  /** The latest poll failed; earlier data is still shown and polling continues. */
  pollError: string | null;
  /** Polling at the fast rate: the run is moving. */
  polling: boolean;
  lastUpdated: number | null;
}

const EMPTY: RunObservation = {
  state: null,
  result: null,
  events: [],
  loadError: null,
  notFound: false,
  pollError: null,
  polling: false,
  lastUpdated: null,
};

/**
 * Reconstructs a run from the backend (GET /state, GET /result for the phase, and the
 * event history), then polls while the run is active: every POLL_INTERVAL_MS it fetches the
 * state, the result and only the events after the last one received (after_sequence).
 *
 * Polling continues until the run is completed or failed: every POLL_INTERVAL_MS while an
 * /execute request from this tab is in flight or the backend reports a progressing phase,
 * otherwise every IDLE_POLL_INTERVAL_MS (the run may be planning in another request, or wait
 * for an approval decided elsewhere). `refresh()` polls at once. Requests never overlap.
 */
export function useRunObserver(runId: string, executing: boolean) {
  const [observation, setObservation] = useState<RunObservation>(EMPTY);
  const cursor = useRef(0);
  const inFlight = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const alive = useRef(true);
  const latest = useRef({ executing, observation });
  latest.current = { executing, observation };

  /** Delay until the next poll, or null to stop (terminal). */
  const nextPoll = useCallback((state: RunState | null, result: FinalResult | null): number | null => {
    if (state && state.status !== "created") return null; // completed or failed: terminal
    if (latest.current.executing || (result !== null && PROGRESSING.has(result.phase))) return POLL_INTERVAL_MS;
    if (state && finishing(state, result)) return POLL_INTERVAL_MS; // RunCompleted is about to be recorded
    return IDLE_POLL_INTERVAL_MS;
  }, []);

  const poll = useCallback(async () => {
    if (inFlight.current || !alive.current) return;
    inFlight.current = true;
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    try {
      const [state, events, result] = await Promise.all([
        getState(runId),
        getEvents(runId, cursor.current),
        getResult(runId),
      ]);
      if (!alive.current) return;
      const fresh = events.filter((e) => e.sequence > cursor.current);
      if (fresh.length > 0) cursor.current = Math.max(...fresh.map((e) => e.sequence));
      const delay = nextPoll(state, result);
      setObservation((o) => ({
        ...o,
        state,
        result,
        events: fresh.length > 0 ? [...o.events, ...fresh] : o.events,
        loadError: null,
        notFound: false,
        pollError: null,
        polling: delay === POLL_INTERVAL_MS,
        lastUpdated: Date.now(),
      }));
      if (delay !== null) timer.current = setTimeout(() => void poll(), delay);
    } catch (error) {
      if (!alive.current) return;
      const notFound = error instanceof ApiError && error.code === "run_not_found";
      const hasData = latest.current.observation.state !== null;
      const delay = notFound ? null : hasData ? nextPoll(latest.current.observation.state, latest.current.observation.result) : POLL_INTERVAL_MS;
      setObservation((o) =>
        o.state === null
          ? { ...o, loadError: describeError(error), notFound, polling: delay !== null }
          : { ...o, pollError: describeError(error), polling: delay !== null },
      );
      if (delay !== null) timer.current = setTimeout(() => void poll(), delay);
    } finally {
      inFlight.current = false;
    }
  }, [runId, nextPoll]);

  // Load (or reload, for another run) and start polling.
  useEffect(() => {
    alive.current = true;
    cursor.current = 0;
    setObservation(EMPTY);
    void poll();
    return () => {
      alive.current = false;
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
    };
  }, [poll]);

  // An /execute request started or returned: poll now (catch the start, or the final state).
  useEffect(() => {
    if (latest.current.observation.state !== null || executing) void poll();
  }, [executing, poll]);

  return { ...observation, refresh: poll };
}
