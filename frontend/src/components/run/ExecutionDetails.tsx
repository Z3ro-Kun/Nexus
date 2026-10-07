import type { FinalResult, NexusEvent, RunState } from "../../types/api";
import { plural } from "../../lib/runView";
import { Icon } from "../ui";
import ActivityPanel from "./details/ActivityPanel";
import EventStream from "./details/EventStream";
import SharedStatePanel from "./details/SharedStatePanel";
import TaskGraph from "./details/TaskGraph";
import { Badge, Mono, Panel } from "./details/ui";
import VerificationPanel from "./details/VerificationPanel";

/**
 * The machinery, for anyone who wants it: the task graph with ids, every tool call, the
 * verification record, the raw event stream and the shared state. Collapsed by default.
 */
export default function ExecutionDetails({ runId, state, result, events }: { runId: string; state: RunState; result: FinalResult | null; events: NexusEvent[] }) {
  const approvals = Object.values(state.approvals);

  return (
    <details className="group border-t border-line pt-5">
      <summary className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 rounded-md">
        <span className="inline-flex items-center gap-2 text-[15px] font-semibold text-ink">
          <Icon name="chevron" className="transition-transform group-open:rotate-90" />
          Execution details
        </span>
        <span className="text-sm text-faint">
          {plural(state.last_sequence, "event")}, {plural(Object.keys(state.tasks).length, "task")}, {plural(Object.keys(state.tool_calls).length, "tool call")}
        </span>
      </summary>

      <div className="mt-6 space-y-8">
        <p className="text-xs text-faint">
          Run <Mono className="text-dim">{runId}</Mono>
          {result && (
            <>
              {" "}
              in phase <Mono className="text-dim">{result.phase}</Mono>, status <Mono className="text-dim">{state.status}</Mono>
            </>
          )}
        </p>
        <TaskGraph state={state} />
        <ActivityPanel state={state} />
        <VerificationPanel state={state} result={result} />
        {approvals.length > 0 && (
          <Panel title="Approvals">
            <ul className="space-y-2">
              {approvals.map((a) => (
                <li key={a.approval_id} className="flex flex-wrap items-baseline gap-2 text-xs">
                  <Mono className="text-dim">{a.approval_id}</Mono>
                  <Badge value={a.status} />
                  <span className="text-dim">{a.description}</span>
                  {a.actor && <span className="text-faint">by {a.actor}</span>}
                </li>
              ))}
            </ul>
          </Panel>
        )}
        <EventStream events={events} />
        <SharedStatePanel state={state} />
      </div>
    </details>
  );
}
