import { useEffect, useState } from "react";

import { ApiError } from "../api/client";
import { getRun } from "../api/runs";
import { formatRelative, truncate } from "../lib/format";
import { forgetRun, recentRunIds } from "../lib/history";
import { linkTo, runPath } from "../lib/router";
import type { Run } from "../types/api";
import { StatusDot } from "./ui";

const STATUS = {
  created: { label: "Not finished", tone: "neutral" },
  completed: { label: "Completed", tone: "ok" },
  failed: { label: "Failed", tone: "bad" },
  needs_clarification: { label: "Needs clarification", tone: "ask" },
} as const;

/**
 * Runs opened in this browser (the backend cannot list runs). Each entry is loaded from
 * GET /runs/{id}; runs the backend no longer knows are dropped from the list.
 */
export default function RecentRuns() {
  const [runs, setRuns] = useState<Run[] | null>(null);

  useEffect(() => {
    const ids = recentRunIds();
    if (ids.length === 0) {
      setRuns([]);
      return;
    }
    const controller = new AbortController();
    Promise.allSettled(ids.map((id) => getRun(id, controller.signal))).then((settled) => {
      if (controller.signal.aborted) return;
      const found: Run[] = [];
      settled.forEach((s, i) => {
        if (s.status === "fulfilled") found.push(s.value);
        else if (s.reason instanceof ApiError && s.reason.code === "run_not_found") forgetRun(ids[i]);
      });
      setRuns(found.sort((a, b) => b.updated_at.localeCompare(a.updated_at)));
    });
    return () => controller.abort();
  }, []);

  if (!runs || runs.length === 0) return null;

  return (
    <section aria-labelledby="recent-heading" className="mt-16">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h2 id="recent-heading" className="text-[15px] font-semibold text-ink">
          Recent runs
        </h2>
        <p className="text-xs text-faint">Opened in this browser</p>
      </div>
      <ul className="divide-y divide-line border-y border-line">
        {runs.map((run) => {
          const status = STATUS[run.status];
          return (
            <li key={run.id}>
              <a
                {...linkTo(runPath(run.id))}
                className="group grid grid-cols-[1fr_auto] items-baseline gap-x-6 gap-y-1 px-1 py-3.5 transition-colors hover:bg-surface sm:grid-cols-[1fr_9rem_6rem]"
              >
                <span className="min-w-0 text-sm text-ink group-hover:text-accent">{truncate(run.goal, 140)}</span>
                <span className="col-start-1 row-start-2 flex items-center gap-2 text-xs text-dim sm:col-start-2 sm:row-start-1">
                  <StatusDot tone={status.tone} />
                  {status.label}
                </span>
                <span className="col-start-2 row-start-1 text-right text-xs text-faint sm:col-start-3">
                  <time dateTime={run.updated_at}>{formatRelative(run.updated_at)}</time>
                </span>
              </a>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
