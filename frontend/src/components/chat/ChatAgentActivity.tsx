import { useEffect, useState } from "react";
import Worker, { type WorkerState } from "../run/Worker";
import { useRunObserver } from "../../hooks/useRunObserver";
import { roleHue, roleLabel } from "../../lib/labels";
import { formatDuration } from "../../lib/format";
import { describeEvent } from "../../lib/events";

interface ChatAgentActivityProps {
  runId: string;
  isRunning: boolean;
  startedAt?: string;
  onFinished?: () => void;
}

export default function ChatAgentActivity({
  runId,
  isRunning,
  startedAt,
  onFinished,
}: ChatAgentActivityProps) {
  const { state, events } = useRunObserver(runId, isRunning);

  useEffect(() => {
    if (state?.status === "completed" || state?.status === "failed") {
      onFinished?.();
    }
  }, [state?.status, onFinished]);

  // Live elapsed duration ticker
  const [elapsedMs, setElapsedMs] = useState(0);
  useEffect(() => {
    if (!isRunning || state?.status === "failed" || state?.status === "completed") return;
    const start = startedAt ? new Date(startedAt).getTime() : Date.now();
    const update = () => setElapsedMs(Math.max(0, Date.now() - start));
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [isRunning, startedAt, state?.status]);

  const tasks = Object.values(state?.tasks || {});
  const runningTasks = tasks.filter((t) => t.status === "running");
  const completedTasks = tasks.filter((t) => t.status === "completed");
  const totalTasks = tasks.length;
  const factCount = Object.keys(state?.facts || {}).length;
  const replanCount = state?.recovery?.replan_count || 0;

  // Determine active task and agent role
  const activeTask =
    runningTasks[0] ||
    tasks.find((t) => t.status === "pending" || t.status === "ready") ||
    null;

  const isVerifying =
    Object.values(state?.verifications || {}).some(
      (v) => v.status === "running"
    ) || activeTask?.agent_type === "verifier";

  const activeAgentType = isVerifying
    ? "verifier"
    : activeTask?.agent_type || "planner";

  const isFailed = state?.status === "failed";
  const isDone = state?.status === "completed";

  // Operator state: "working" | "planning" | "verifying" | "failed" | "done"
  let workerState: WorkerState = "working";
  if (isFailed) {
    workerState = "failed";
  } else if (isDone) {
    workerState = "done";
  } else if (isVerifying) {
    workerState = "verifying";
  } else if (
    activeAgentType === "planner" ||
    activeAgentType === "replanner" ||
    tasks.length === 0
  ) {
    workerState = "planning";
  } else if (runningTasks.length === 0) {
    workerState = "waiting";
  }

  const hue = isFailed ? "amber" : roleHue(activeAgentType);

  // Find recent active tool call
  const toolCalls = Object.values(state?.tool_calls || {});
  const activeToolCall =
    toolCalls.find((tc) => tc.status === "requested") ||
    toolCalls[toolCalls.length - 1];

  let currentActionText = "Analyzing prompt & decomposing plan…";
  if (isFailed) {
    currentActionText = state?.failure_reason || "Run failed to complete objective.";
  } else if (isVerifying) {
    currentActionText = "Verifying facts, claims, and objective completion…";
  } else if (activeToolCall && activeToolCall.status === "requested") {
    const target =
      typeof activeToolCall.arguments?.query === "string"
        ? `"${activeToolCall.arguments.query}"`
        : activeToolCall.tool_name;
    currentActionText = `Executing ${activeToolCall.tool_name} (${target})…`;
  } else if (activeTask) {
    currentActionText = activeTask.title;
  }

  // Calculate progress percent
  const progressPercent =
    totalTasks === 0
      ? 15
      : Math.min(
          95,
          Math.max(
            15,
            Math.round((completedTasks.length / totalTasks) * 85) +
              (runningTasks.length > 0 ? 10 : 0)
          )
        );

  // Latest 2 events
  const recentEvents = [...events].slice(-2).reverse();

  return (
    <div className="rounded-2xl border border-line bg-surface/90 shadow-xs overflow-hidden backdrop-blur-xs max-w-xl transition-all duration-300">
      {/* Header with Compact Isometric Worker Operator */}
      <div className="flex items-center gap-3.5 p-3.5 bg-surface-2/40 border-b border-line/60">
        <div className="relative shrink-0 flex items-center justify-center p-1.5 rounded-xl bg-canvas/70 border border-line/60 shadow-2xs">
          <Worker hue={hue} state={workerState} size={50} />
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="relative flex size-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent opacity-75"></span>
              <span className="relative inline-flex rounded-full size-2 bg-accent"></span>
            </span>
            <span className="text-xs font-semibold text-ink uppercase tracking-wider">
              {roleLabel(activeAgentType)}
            </span>
            <span className="text-[11px] font-mono text-dim ml-auto">
              ⏱ {formatDuration(elapsedMs)}
            </span>
          </div>

          <p className="text-sm font-medium text-ink truncate mt-0.5" title={currentActionText}>
            {currentActionText}
          </p>
        </div>
      </div>

      {/* Metrics Bar */}
      <div className="grid grid-cols-3 divide-x divide-line/60 border-b border-line/60 text-center py-2 bg-canvas/25 text-xs">
        <div>
          <span className="text-[10px] text-dim block uppercase tracking-wider">Tasks</span>
          <span className="font-semibold text-ink font-mono text-xs">
            {completedTasks.length} / {totalTasks > 0 ? totalTasks : "…"}
          </span>
        </div>
        <div>
          <span className="text-[10px] text-dim block uppercase tracking-wider">Facts</span>
          <span className="font-semibold text-ink font-mono text-xs">{factCount}</span>
        </div>
        <div>
          <span className="text-[10px] text-dim block uppercase tracking-wider">Replans</span>
          <span className="font-semibold text-ink font-mono text-xs">{replanCount}</span>
        </div>
      </div>

      {/* Progress Bar */}
      <div className="w-full bg-surface-2 h-1 overflow-hidden">
        <div
          className="bg-accent h-full transition-all duration-500 ease-out"
          style={{ width: `${progressPercent}%` }}
        />
      </div>

      {/* Real-time Event Ticker */}
      {recentEvents.length > 0 && (
        <div className="p-2.5 px-3.5 space-y-1 bg-surface-2/20 font-mono text-[11px]">
          {recentEvents.map((ev) => {
            const desc = describeEvent(ev);
            return (
              <div key={ev.id} className="flex items-center gap-1.5 truncate text-dim">
                <span className="text-accent shrink-0">›</span>
                <span className="truncate">{desc.detail || ev.event_type}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
