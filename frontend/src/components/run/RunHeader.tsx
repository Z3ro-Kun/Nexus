import { useEffect, useState } from "react";

import { elapsed, runStatus, runTotals, plural } from "../../lib/runView";
import type { FinalResult, NexusEvent, RunState } from "../../types/api";
import { Icon, StatusDot, TONE_TEXT } from "../ui";

interface Props {
  state: RunState;
  result: FinalResult | null;
  events: NexusEvent[];
  executing: boolean;
  polling: boolean;
  pollError: string | null;
}

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

/** The objective, and one line that says where the run stands. */
export default function RunHeader({ state, result, events, executing, polling, pollError }: Props) {
  const status = runStatus(state, result, executing);
  const totals = runTotals(state);
  const time = elapsed(events, status.live, useNow(status.live));

  return (
    <header className="animate-enter">
      <p className="text-sm text-faint">Objective</p>
      <h1 className="mt-1.5 max-w-[60ch] text-[1.45rem] leading-snug font-medium tracking-[-0.01em] break-words text-pretty text-ink sm:text-[1.75rem]">{state.goal}</h1>

      <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
        <p role="status" aria-label="Run status" className={`flex items-center gap-2 font-semibold ${TONE_TEXT[status.tone]}`}>
          {status.tone === "ok" ? <Icon name="check" /> : status.tone === "bad" ? <Icon name="cross" /> : <StatusDot tone={status.tone} live={status.live} />}
          {status.label}
        </p>
        {result?.verified && (
          <p className="flex items-center gap-1.5 font-medium text-ok">
            <Icon name="check" className="size-3.5" />
            Verified
          </p>
        )}
        {time && (
          <p className="text-dim">
            <span className="sr-only">Elapsed </span>
            <span className="tabular-nums">{time}</span>
          </p>
        )}
        {totals.tasks > 0 && (
          <p className="text-dim">
            {plural(totals.tasks, "task")}, {plural(totals.agents, "agent")}
          </p>
        )}
        {totals.tools && <p className="text-dim">{totals.tools}</p>}
        <p className="ml-auto text-xs text-faint" aria-live="polite">
          {pollError ? <span className="text-warn">Connection lost, retrying</span> : polling ? "Updating live" : null}
        </p>
      </div>
    </header>
  );
}
