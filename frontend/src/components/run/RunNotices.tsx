import type { Execution } from "../../lib/executions";
import { finishing, stoppedBecause } from "../../lib/runView";
import type { FinalResult, RunState } from "../../types/api";
import { TechnicalDetail, buttonClass } from "../ui";

interface Props {
  state: RunState;
  result: FinalResult | null;
  execution: Execution | undefined;
  onExecute: () => void;
  onRefresh: () => void;
}

/**
 * Situations that need the user: a failed /execute request, a run that stalled or waits for
 * an approval, or a run that is not executing. Resuming calls /execute again, which the
 * backend treats as "continue this run" (completed work is not repeated).
 */
export default function RunNotices({ state, result, execution, onExecute, onRefresh }: Props) {
  if (!result || finishing(state, result)) return null; // RunCompleted is about to be recorded
  const running = execution?.status === "running";
  const terminal = state.status !== "created";
  const pending = Object.values(state.approvals).filter((a) => a.status === "pending");
  const canExecute = !terminal && !running && result.phase !== "executing" && result.phase !== "verifying";

  const notices: { tone: "bad" | "warn"; title: string; body?: string[]; technical?: string[]; alert: boolean }[] = [];
  if (execution?.status === "error") notices.push({ tone: "bad", title: "The execution request failed.", body: [execution.message], alert: true });
  if (result.phase === "blocked")
    notices.push({
      tone: "warn",
      title: "NEXUS can't make further progress on this run.",
      body: stoppedBecause(state),
      technical: result.completion_blockers,
      alert: false,
    });
  if (result.phase === "waiting_for_approval")
    notices.push({
      tone: "warn",
      title: pending.length === 1 ? "An action is waiting for human approval." : `${pending.length} actions are waiting for human approval.`,
      body: [...pending.map((a) => state.tasks[a.task_id]?.title ?? a.description), "Once it is approved or rejected, resume the run."],
      technical: pending.map((a) => `${a.approval_id}: ${a.description}`),
      alert: false,
    });

  if (notices.length === 0 && !canExecute) return null;

  return (
    <div className="space-y-4">
      {notices.map((n, i) => (
        <div key={i} role={n.alert ? "alert" : "status"} className={`border-l-2 pl-4 ${n.tone === "bad" ? "border-bad" : "border-warn"}`}>
          <p className={`text-[15px] font-medium ${n.tone === "bad" ? "text-bad" : "text-warn"}`}>{n.title}</p>
          {n.body?.map((line) => (
            <p key={line} className="mt-1 max-w-[68ch] text-sm break-words text-dim">
              {line}
            </p>
          ))}
          {n.technical && n.technical.length > 0 && (
            <TechnicalDetail>
              {n.technical.map((line) => (
                <p key={line}>{line}</p>
              ))}
            </TechnicalDetail>
          )}
        </div>
      ))}

      {canExecute && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-3 rounded-lg bg-raised px-4 py-3">
          <p className="mr-auto text-sm text-dim">
            {result.phase === "created" ? "This run is not executing. It has not started, or its execution stopped." : "This run is not executing right now."}
          </p>
          <div className="flex items-center gap-2">
            <button type="button" onClick={onRefresh} className={buttonClass.quiet}>
              Refresh
            </button>
            <button type="button" onClick={onExecute} className={buttonClass.secondary}>
              {execution?.status === "error" ? "Retry execution" : result.phase === "created" ? "Execute run" : "Resume execution"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
