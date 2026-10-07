import { useId, useLayoutEffect, useRef, useState } from "react";

import { HUE_VAR } from "../../lib/labels";
import { layoutGraph, PLANNER_ID, type LayoutEdge, type LayoutNode } from "../../lib/flowLayout";
import { isTerminal, plural, runningTasks, taskView, type TaskView } from "../../lib/runView";
import { usePrefersReducedMotion } from "../../hooks/usePrefersReducedMotion";
import { useMotionPaused } from "../../lib/motion";
import type { NexusEvent, RunState, TaskState } from "../../types/api";
import { Icon, Section } from "../ui";
import { Comet, useGrowth } from "./Signal";

type EdgeState = "idle" | "done" | "flowing" | "failed";

/**
 * The plan as a live system: real dependencies, tasks that can run together side by side,
 * results merging through shared state, recovery as a dashed new path. Data visibly flows
 * (comets) only along edges into a task the backend reports as running; a running task
 * carries a light around its border. The whole graph is drawn in once, layer by layer.
 */
export default function FlowGraph({ state, events }: { state: RunState; events: NexusEvent[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  const paused = useMotionPaused();
  const reduced = usePrefersReducedMotion() || paused;
  const maskId = `reveal${useId().replace(/[^a-zA-Z0-9]/g, "")}`;

  useLayoutEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      const w = Math.round(entry.contentRect.width);
      if (w > 0) setWidth(w);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const tasks = state.tasks;
  if (Object.keys(tasks).length === 0) return null;
  const planned = events.some((e) => e.event_type === "TaskCreated" && e.agent_id === "planner");
  const layout = layoutGraph(tasks, planned, width);
  const views = new Map(Object.values(tasks).map((t) => [t.task_id, taskView(state, t)]));
  const running = runningTasks(state);
  const terminal = isTerminal(state);
  const findings = (ids: string[]) => Object.values(state.facts).filter((f) => f.task_id !== null && ids.includes(f.task_id)).length;

  const edgeState = (edge: LayoutEdge): EdgeState => {
    const target = edge.to.startsWith("join>") ? tasks[edge.to.slice(5)] : tasks[edge.to];
    if (edge.kind === "replacement") return target?.status === "running" ? "flowing" : "done";
    const join = layout.junctions.find((j) => j.id === edge.from);
    const sources = edge.from === PLANNER_ID ? [] : join ? join.from.map((d) => tasks[d]) : [tasks[edge.from]];
    if (!join && sources.some((x) => x?.status === "failed" || x?.status === "cancelled")) return "failed";
    const sourceDone = sources.every((x) => x?.status === "completed" || (x?.status === "failed" && x.replaced_by !== null));
    if (sourceDone && target?.status === "running") return "flowing";
    if (sourceDone && target && target.status !== "pending" && target.status !== "ready" && target.status !== "blocked") return "done";
    return sourceDone && edge.kind !== "plan" ? "done" : "idle";
  };

  const parallelNow = running.length;
  const caption = terminal
    ? null
    : parallelNow > 1
      ? `${parallelNow} tasks running in parallel`
      : parallelNow === 1
        ? "1 task running"
        : null;

  return (
    <Section title={terminal ? "How NEXUS did it" : "Plan"} aside={caption ? <p className="text-sm font-medium text-accent">{caption}</p> : null}>
      <div
        ref={box}
        className="w-full overflow-x-auto overscroll-x-contain"
        {...(layout.width > width ? { tabIndex: 0, role: "region", "aria-label": "Plan graph, scrolls sideways" } : {})}
      >
        <div className="relative" style={{ width: layout.width, height: layout.height }}>
          <svg className="absolute inset-0 overflow-visible" width={layout.width} height={layout.height} aria-hidden="true">
            <defs>
              <mask id={maskId} maskUnits="userSpaceOnUse" x={-20} y={-20} width={layout.width + 40} height={layout.height + 40}>
                <rect className="graph-reveal" x={-20} y={-20} width={layout.width + 40} height={layout.height + 40} fill="white" style={{ ["--reveal" as string]: `${(Math.max(0, ...layout.nodes.map((n) => n.layer)) + 2) * 90 + 420}ms` }} />
              </mask>
            </defs>
            <g mask={`url(#${maskId})`}>
            {layout.edges.map((edge) => {
              const s = edgeState(edge);
              const hue = HUE_VAR[views.get(edge.to.startsWith("join>") ? edge.to.slice(5) : edge.to)?.hue ?? "accent"];
              const stroke =
                edge.kind === "replacement" ? "var(--amber)" : s === "flowing" ? hue : s === "failed" ? "var(--bad)" : "var(--line-strong)";
              return (
                <g key={edge.id}>
                  <path
                    d={edge.path}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={s === "flowing" ? 1.75 : 1.25}
                    strokeDasharray={edge.kind === "replacement" ? "5 4" : s === "idle" ? "2 4" : undefined}
                    opacity={s === "failed" ? 0.5 : 1}
                    className={`flow-edge ${edge.kind === "replacement" && !terminal ? "flow-edge-new" : ""}`}
                  />
                  {s === "flowing" && !reduced && (
                    <>
                      <Comet d={edge.path} color={hue} />
                      <Comet d={edge.path} color={hue} delay={0.95} />
                    </>
                  )}
                  {/* Rerouting after a failure: one pulse along the new path, the first time it is seen. */}
                  {edge.kind === "replacement" && !terminal && !reduced && <Comet d={edge.path} color="var(--amber)" dur={1.4} delay={0.8} once />}
                </g>
              );
            })}
            </g>
          </svg>

          {layout.junctions.map((j) => (
            <Junction key={j.id} x={j.x} y={j.y} findings={findings(j.from)} active={tasks[j.into]?.status === "running"} compact={layout.compact} title={`Results of ${plural(j.from.length, "task")} meet in shared state, where “${tasks[j.into]?.title}” reads them`} />
          ))}

          <ol aria-label="Plan steps">
            {layout.nodes.map((node) =>
              node.planner ? (
                <PlannerNode key={node.id} node={node} count={Object.keys(tasks).length} />
              ) : (
                <TaskNode key={node.id} node={node} view={views.get(node.id)!} task={tasks[node.id]} compact={layout.compact} after={tasks[node.id].dependencies.map((d) => tasks[d]?.title ?? d)} />
              ),
            )}
          </ol>
        </div>
      </div>
      <Legend flowing={!terminal && running.length > 0} recovery={layout.edges.some((e) => e.kind === "replacement")} waiting={Object.values(tasks).some((t) => t.status === "pending" || t.status === "ready")} />
    </Section>
  );
}

/** Where several results meet in shared state; ripples when a new finding lands there. */
function Junction({ x, y, findings, active, compact, title }: { x: number; y: number; findings: number; active: boolean; compact: boolean; title: string }) {
  const landed = useGrowth(findings);
  return (
    <div className="absolute -translate-x-1/2 -translate-y-1/2" style={{ left: x, top: y }} title={title}>
      <span
        className={`relative flex items-center gap-1.5 rounded-full border bg-surface px-2 py-0.5 text-[11px] whitespace-nowrap transition-colors ${
          active ? "border-accent text-accent shadow-[0_0_0_4px_color-mix(in_oklab,var(--accent)_10%,transparent)]" : "border-line-strong text-dim"
        }`}
      >
        <span className="relative grid size-2 place-items-center" aria-hidden="true">
          <span className={`size-1.5 rotate-45 ${active || findings > 0 ? "bg-accent" : "bg-line-strong"}`} />
          {landed > 0 && <span key={landed} className="ripple absolute inset-[-3px] rounded-full border border-accent" />}
        </span>
        {compact ? (findings > 0 ? plural(findings, "finding") : "shared state") : findings > 0 ? `Shared state · ${plural(findings, "finding")}` : "Shared state"}
      </span>
    </div>
  );
}

function PlannerNode({ node, count }: { node: LayoutNode; count: number }) {
  return (
    <li
      className="flow-node absolute flex items-center gap-2 rounded-full border px-3"
      style={{ left: node.x, top: node.y, width: node.w, height: node.h, borderColor: "var(--violet)", color: "var(--violet)", ["--layer" as string]: 0 }}
      aria-label={`Planner: planned ${plural(count, "task")}`}
    >
      <span className="size-2 rotate-45 bg-current" aria-hidden="true" />
      <span className="truncate text-xs font-semibold">Planner</span>
      <span className="ml-auto truncate text-[11px] text-faint">{plural(count, "task")}</span>
    </li>
  );
}

function TaskNode({ node, view, task, compact, after }: { node: LayoutNode; view: TaskView; task: TaskState; compact: boolean; after: string[] }) {
  const hue = HUE_VAR[view.hue];
  const replaced = view.replacedBy !== null;
  const running = view.status === "running";
  const failed = view.status === "failed";
  const waiting = view.status === "pending" || view.status === "ready" || view.status === "blocked";
  const verified = view.kind === "verification" && view.status === "completed";
  return (
    <li
      aria-label={`${view.role}: ${view.title}`}
      data-status={view.status}
      className={`flow-node absolute rounded-lg border bg-surface transition-[border-color,opacity] duration-500 ${
        running ? "shadow-[0_0_0_4px_color-mix(in_oklab,var(--hue)_12%,transparent),0_6px_18px_-8px_var(--hue)]" : ""
      } ${replaced ? "opacity-55" : ""}`}
      style={{
        left: node.x,
        top: node.y,
        width: node.w,
        height: node.h,
        borderColor: running ? `color-mix(in oklab, ${hue} 45%, var(--line))` : failed && !replaced ? "var(--bad)" : verified ? "color-mix(in oklab, var(--ok) 55%, var(--line))" : "var(--line)",
        ["--hue" as string]: hue,
        ["--layer" as string]: node.layer + 1,
      }}
    >
      <span className="absolute inset-y-2 left-0 w-[3px] rounded-r-full" style={{ background: hue, opacity: running ? 1 : view.status === "completed" ? 0.75 : 0.35 }} aria-hidden="true" />
      <div className={`flex h-full flex-col justify-center ${compact ? "gap-0.5 px-2.5" : "gap-1 px-3.5"}`}>
        <p className="flex items-center gap-1.5 text-[11px] leading-none">
          {view.status === "completed" ? (
            <Icon name="check" className="size-3 text-ok" />
          ) : failed ? (
            <Icon name="cross" className="size-3 text-bad" />
          ) : (
            <span
              className={`size-1.5 shrink-0 rounded-full ${running ? "animate-live" : ""}`}
              style={{ background: running ? hue : waiting ? "var(--amber)" : "var(--line-strong)" }}
              aria-hidden="true"
            />
          )}
          <span className="truncate font-semibold" style={{ color: hue }}>
            {view.role}
          </span>
          <span className={`ml-auto shrink-0 ${compact ? "sr-only" : ""} ${running ? "text-accent" : failed ? "text-bad" : waiting ? "text-warn" : "text-faint"}`}>
            {view.statusLabel}
          </span>
        </p>
        <p className={`line-clamp-2 leading-snug text-ink ${compact ? "text-xs" : "text-[13px]"} ${replaced ? "line-through decoration-faint" : ""}`}>{view.title}</p>
        {!compact && (view.toolSummary || view.replaces) && (
          <p className="truncate text-[11px] text-faint">{view.replaces ? `Replaces “${view.replaces}”` : view.toolSummary}</p>
        )}
        <span className="sr-only">{after.length > 0 ? `After ${after.join(", ")}.` : task.replaces ? "" : "Starts from the plan."}</span>
      </div>
    </li>
  );
}

function Legend({ flowing, recovery, waiting }: { flowing: boolean; recovery: boolean; waiting: boolean }) {
  if (!flowing && !recovery && !waiting) return null;
  return (
    <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-faint" aria-hidden="true">
      {flowing && (
        <span className="flex items-center gap-1.5">
          <span className="h-px w-4 bg-accent" />
          data flowing to a running task
        </span>
      )}
      {recovery && (
        <span className="flex items-center gap-1.5">
          <span className="h-0 w-4 border-t border-dashed border-amber" />
          recovery path
        </span>
      )}
      {waiting && (
        <span className="flex items-center gap-1.5">
          <span className="size-1.5 rounded-full bg-amber" />
          waiting
        </span>
      )}
    </p>
  );
}
