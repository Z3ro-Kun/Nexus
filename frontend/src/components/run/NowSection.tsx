import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

import { usePrefersReducedMotion } from "../../hooks/usePrefersReducedMotion";
import { HUE_VAR } from "../../lib/labels";
import { setMotionPaused, useMotionPaused } from "../../lib/motion";
import { finishing, phaseOf, plural, runningTasks, taskOrder, taskView, type TaskView } from "../../lib/runView";
import type { FinalResult, RunState } from "../../types/api";
import { Icon, Section } from "../ui";
import { Comet, useGrowth } from "./Signal";
import Worker from "./Worker";

/**
 * What is happening at this moment, for active runs only. One worker per task the backend
 * reports as running, never more: two running tasks show two workers side by side, each
 * joined by a beam to the shared state they coordinate through.
 */
export default function NowSection({ state, result, executing }: { state: RunState; result: FinalResult | null; executing: boolean }) {
  const paused = useMotionPaused();
  const reduced = usePrefersReducedMotion() || paused;
  const phase = phaseOf(state, result);
  const views = taskOrder(state).map((t) => taskView(state, t));
  const active = runningTasks(state).map((t) => views.find((v) => v.id === t.task_id)!);
  const next = views.filter((v) => v.status === "ready" || v.status === "pending").slice(0, 3);
  const facts = Object.keys(state.facts).length;
  // Finished work whose findings already sit in shared state: the other side of convergence.
  const contributed = views.filter((v) => v.status === "completed" && v.kind !== "verification" && Object.values(state.facts).some((f) => f.task_id === v.id));
  const withInputs = new Set(runningTasks(state).filter((t) => t.dependencies.length > 0).map((t) => t.task_id));

  if (phase === "created") {
    if (!executing) return null;
    return (
      <Section title="Now">
        <Stage>
          <div className="flex flex-col items-center gap-2 py-2 text-center">
            <Worker hue="violet" state="planning" size={104} />
            <p className="text-[15px] font-semibold" style={{ color: "var(--violet)" }}>
              Planner
            </p>
            <p className="text-sm text-dim">Breaking the objective into tasks</p>
          </div>
        </Stage>
      </Section>
    );
  }

  if (finishing(state, result)) {
    return (
      <Section title="Now">
        <Stage>
          <div className="flex flex-col items-center gap-2 py-2 text-center">
            <Worker hue="ok" state="done" size={96} />
            <p className="text-sm text-dim">All work is verified. Recording the result.</p>
          </div>
        </Stage>
      </Section>
    );
  }

  if (active.length === 0 && next.length === 0) return null;
  const caption = active.length > 1 ? `${active.length} agents working in parallel` : active.length === 1 ? "1 agent working" : null;

  return (
    <Section
      title="Now"
      aside={
        <div className="flex items-center gap-3">
          {caption && <p className="text-sm font-semibold text-accent">{caption}</p>}
          {active.length > 0 && (
            <button type="button" className="rounded-md px-1.5 py-0.5 text-xs text-faint hover:text-ink" aria-pressed={paused} onClick={() => setMotionPaused(!paused)}>
              {paused ? "Resume motion" : "Pause motion"}
            </button>
          )}
        </div>
      }
    >
      {active.length > 0 && (
        <Stage>
          <Coordination active={active} facts={facts} reduced={reduced} withInputs={withInputs} contributed={contributed} />
        </Stage>
      )}

      {next.length > 0 && (
        <ul className="mt-4 space-y-2" aria-label="Up next">
          {next.map((v) => (
            <li key={v.id} className="flex flex-wrap items-baseline gap-x-2 text-sm text-dim">
              <span className="size-1.5 shrink-0 translate-y-[-1px] self-center rounded-full bg-amber" aria-hidden="true" />
              <span className="font-medium text-ink">{v.role}</span>
              <span>{v.kind === "verification" ? "checks the result" : v.title}</span>
              {v.detail && <span className="w-full pl-3.5 text-xs text-faint sm:w-auto sm:pl-0">{v.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/** The panel workers stand on: a dot field, lit where agents are working. */
function Stage({ children }: { children: ReactNode }) {
  return (
    <div className="relative overflow-hidden rounded-xl border border-line bg-surface px-3 pt-5 pb-5 sm:px-6">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 opacity-70 [background-image:radial-gradient(var(--line-strong)_1px,transparent_1px)] [background-size:16px_16px] [mask-image:radial-gradient(ellipse_at_50%_40%,black,transparent_75%)]"
      />
      <div className="relative">{children}</div>
    </div>
  );
}

interface Beam {
  id: string;
  d: string;
  hue: string;
  writes: boolean;
  reads: boolean;
}

/**
 * Agents around the shared state they coordinate through. Every running task is joined to the
 * core by a beam: pulses travel up to it when the task reads inputs other tasks wrote (it has
 * dependencies, or it is the verifier converging the evidence) and down into it from tasks
 * that produce findings. Positions are measured, so beams follow any wrapping.
 */
function Coordination({ active, facts, reduced, withInputs, contributed }: { active: TaskView[]; facts: number; reduced: boolean; withInputs: Set<string>; contributed: TaskView[] }) {
  const scene = useRef<HTMLDivElement>(null);
  const hub = useRef<HTMLDivElement>(null);
  const [beams, setBeams] = useState<Beam[]>([]);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const landed = useGrowth(facts);
  const key = active.map((v) => v.id).join("|");

  useLayoutEffect(() => {
    const root = scene.current;
    const core = hub.current;
    if (!root || !core) return;
    const measure = () => {
      const r = root.getBoundingClientRect();
      const c = core.getBoundingClientRect();
      const ports = Array.from(root.querySelectorAll<HTMLElement>("[data-port]"));
      const hx = c.left + c.width / 2 - r.left;
      const hy = c.top - r.top + 4;
      setSize({ w: r.width, h: r.height });
      setBeams(
        ports.map((port, i) => {
          const p = port.getBoundingClientRect();
          const x = p.left + p.width / 2 - r.left;
          const y = p.top + p.height / 2 - r.top;
          const ex = hx + (i - (ports.length - 1) / 2) * 7;
          const my = (y + hy) / 2;
          const view = active.find((v) => v.id === port.dataset.port)!;
          return { id: view.id, d: `M ${x} ${y} C ${x} ${my}, ${ex} ${my}, ${ex} ${hy}`, hue: HUE_VAR[view.hue], writes: view.kind !== "verification", reads: view.kind === "verification" || withInputs.has(view.id) };
        }),
      );
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return (
    <div ref={scene} className="relative">
      <svg className="pointer-events-none absolute inset-0 overflow-visible" width={size.w} height={size.h} aria-hidden="true">
        {beams.map((b, i) => (
          <g key={b.id}>
            <path d={b.d} fill="none" stroke={b.hue} strokeOpacity="0.35" strokeWidth="1.25" />
            {!reduced && b.writes && <Comet d={b.d} color={b.hue} dur={2.2} delay={i * 0.35} />}
            {!reduced && b.reads && <Comet d={`${reverse(b.d)}`} color="var(--accent)" dur={2.2} delay={1.1 + i * 0.35} />}
          </g>
        ))}
      </svg>

      <ul aria-label="Running now" className="relative flex flex-wrap justify-center gap-x-2 gap-y-8 sm:gap-x-12">
        {active.map((v) => (
          <ActiveWorker key={v.id} view={v} />
        ))}
      </ul>

      <div className="relative mt-12 flex flex-col items-center text-center sm:mt-14">
        <div ref={hub} className="relative grid size-14 place-items-center" aria-hidden="true">
          <span className="absolute -inset-3 rounded-full bg-[radial-gradient(closest-side,color-mix(in_oklab,var(--accent)_16%,transparent),transparent)]" />
          <span className="hub-orbit absolute -inset-1.5 rounded-full border border-dashed border-accent/40" />
          <span className="absolute inset-1.5 rounded-full border border-accent/50 bg-surface" />
          <span className="relative size-3 rotate-45 rounded-[2px] bg-accent" />
          {landed > 0 && <span key={landed} className="ripple absolute inset-1.5 rounded-full border-2 border-accent" />}
        </div>
        <p className="mt-2.5 text-sm font-semibold text-ink">Shared state</p>
        <p className="text-xs text-dim">{facts > 0 ? `${plural(facts, "finding")} so far` : "No findings yet"}</p>
        {contributed.length > 0 && (
          <ul aria-label="Already in shared state" className="mt-3 flex max-w-full flex-wrap justify-center gap-1.5">
            {contributed.slice(0, 4).map((v) => (
              <li key={v.id} className="flex min-w-0 items-center gap-1.5 rounded-full border border-line bg-surface py-0.5 pr-2.5 pl-1.5 text-xs text-dim">
                <Icon name="check" className="size-3 shrink-0 text-ok" />
                <span className="truncate">
                  <span className="font-medium" style={{ color: HUE_VAR[v.hue] }}>
                    {v.role}
                  </span>{" "}
                  {v.title}
                </span>
              </li>
            ))}
            {contributed.length > 4 && <li className="px-1 py-0.5 text-xs text-faint">and {contributed.length - 4} more</li>}
          </ul>
        )}
      </div>
    </div>
  );
}

/** The same cubic, travelled the other way. */
function reverse(d: string): string {
  const n = d.match(/-?\d+(\.\d+)?/g)!.map(Number);
  return `M ${n[6]} ${n[7]} C ${n[4]} ${n[5]}, ${n[2]} ${n[3]}, ${n[0]} ${n[1]}`;
}

function ActiveWorker({ view }: { view: TaskView }) {
  const verifying = view.kind === "verification";
  const hue = HUE_VAR[view.hue];
  return (
    <li className="relative flex w-[6.5rem] min-w-0 flex-col items-center text-center sm:w-44" style={{ ["--hue" as string]: hue }}>
      <span className="agent-glow pointer-events-none absolute -top-4 left-1/2 size-32 -translate-x-1/2 sm:size-44" aria-hidden="true" />
      <Worker hue={view.hue} state={verifying ? "verifying" : "working"} className="relative size-20 sm:size-28" />
      <p className="relative mt-1 text-sm font-semibold" style={{ color: hue }}>
        {view.role}
      </p>
      <p className="relative mt-0.5 line-clamp-2 max-w-[22ch] text-[13px] text-ink sm:text-sm">{verifying ? "Checking the result against the objective" : view.title}</p>
      <p className="relative mt-1 text-xs text-faint">{view.now ?? (view.toolSummary ? `${view.toolSummary} so far` : "Working")}</p>
      {/* Where this agent's beam to shared state starts. */}
      <span data-port={view.id} className="relative mt-2 block size-2 rounded-full border-2 bg-surface" style={{ borderColor: hue }} aria-hidden="true" />
    </li>
  );
}
