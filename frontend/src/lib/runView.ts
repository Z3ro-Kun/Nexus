/**
 * Plain-language views of a run, derived only from what the backend returned: projected
 * state (GET /state), the result and its phase (GET /result), the event history (GET
 * /events) and whether this tab has an /execute request in flight. Nothing here invents
 * progress, evidence or verdicts; when the backend has not said something, the view says
 * less.
 */

import type {
  Clarification,
  ConflictState,
  Fact,
  FinalResult,
  NexusEvent,
  RunPhase,
  RunState,
  TaskState,
  ToolCallState,
  VerificationState,
} from "../types/api";
import { formatDuration, truncate } from "./format";
import { checkLabel, countTools, failureLabel, hostOf, roleHue, roleLabel, type RoleHue } from "./labels";

/** ask: the run is waiting for the user to say what they want (needs clarification). */
export type Tone = "neutral" | "active" | "ok" | "warn" | "bad" | "ask";

export const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
const isRecord =(v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" && v.length > 0 ? v : null);

export function phaseOf(state: RunState, result: FinalResult | null): RunPhase {
  if (state.status === "completed" || state.status === "failed" || state.status === "needs_clarification") return state.status;
  return result?.phase ?? "created";
}

/** The planner's clarification, when the run stopped before planning. */
export function clarificationOf(state: RunState, result: FinalResult | null): Clarification | null {
  if (phaseOf(state, result) !== "needs_clarification") return null;
  return result?.clarification ?? state.clarification ?? null;
}

export const isTerminal = (state: RunState) => state.status !== "created";

// --- Status ---------------------------------------------------------------------------------

export interface StatusView {
  label: string;
  tone: Tone;
  /** The run is moving on its own (shown as a live indicator). */
  live: boolean;
}

/**
 * All work is done and nothing failed unreplaced, but RunCompleted is not recorded yet. The
 * backend briefly reports this as `blocked` between the last TaskCompleted and RunCompleted.
 */
export function finishing(state: RunState, result: FinalResult | null): boolean {
  const tasks = Object.values(state.tasks);
  return (
    phaseOf(state, result) === "blocked" &&
    tasks.length > 0 &&
    tasks.every((t) => t.status === "completed" || (t.status === "failed" && t.replaced_by !== null)) &&
    latestAttempts(state).every((v) => v.status === "passed")
  );
}

/** Tasks the backend reports as running right now: real concurrency, never inferred. */
export function runningTasks(state: RunState): TaskState[] {
  return taskOrder(state).filter((t) => t.status === "running");
}

export function runStatus(state: RunState, result: FinalResult | null, executing: boolean): StatusView {
  if (finishing(state, result)) return { label: "Finishing", tone: "active", live: true };
  switch (phaseOf(state, result)) {
    case "completed":
      return result?.verified
        ? { label: "Completed", tone: "ok", live: false }
        : { label: "Completed without verification", tone: "warn", live: false };
    case "failed":
      return { label: "Failed", tone: "bad", live: false };
    case "executing":
      return { label: "Working", tone: "active", live: true };
    case "verifying":
      return { label: "Verifying", tone: "active", live: true };
    case "waiting_for_approval":
      return { label: "Waiting for approval", tone: "warn", live: false };
    case "blocked":
      return { label: "Stalled", tone: "warn", live: false };
    case "created":
      return executing ? { label: "Planning", tone: "active", live: true } : { label: "Not started", tone: "neutral", live: false };
    case "needs_clarification":
      return { label: "Needs clarification", tone: "ask", live: false };
  }
}

// --- Stages -----------------------------------------------------------------------------------

export type StageId = "plan" | "execute" | "verify" | "done";
export type StageState = "upcoming" | "active" | "complete" | "paused" | "failed";

export interface StageView {
  id: StageId;
  label: string;
  state: StageState;
  /** Elapsed time from the run's creation to the start of this stage, when recorded. */
  at: string | null;
  note: string | null;
}

function firstAt(events: NexusEvent[], ...types: string[]): number | null {
  const e = events.find((x) => types.includes(x.event_type));
  const t = e ? Date.parse(e.timestamp) : NaN;
  return Number.isNaN(t) ? null : t;
}

function latestAttempts(state: RunState): VerificationState[] {
  return Object.values(state.verifications).filter((v) => !v.replaced_by);
}

export function stages(state: RunState, result: FinalResult | null, events: NexusEvent[], executing: boolean): StageView[] {
  const phase = phaseOf(state, result);
  const start = firstAt(events, "RunCreated");
  // Stage start times, shown only once they are at least a second into the run.
  const offset = (t: number | null) => (start !== null && t !== null && t - start >= 1000 ? formatDuration(t - start) : null);
  const planned = Object.keys(state.tasks).length > 0;
  const verificationStarted = firstAt(events, "VerificationStarted") !== null || Object.values(state.verifications).length > 0;
  const verificationFailed = latestAttempts(state).some((v) => v.status === "failed" || v.status === "error");
  const replans = state.recovery.history.filter((r) => r.outcome === "accepted").length;

  const s: Record<StageId, StageState> = { plan: "upcoming", execute: "upcoming", verify: "upcoming", done: "upcoming" };
  if (planned) s.plan = "complete";
  switch (phase) {
    case "created":
      s.plan = executing ? "active" : "upcoming";
      break;
    case "executing":
      s.execute = "active";
      break;
    case "verifying":
      s.execute = "complete";
      s.verify = "active";
      break;
    case "waiting_for_approval":
      s.execute = "paused";
      break;
    case "blocked":
      if (finishing(state, result)) {
        s.execute = s.verify = "complete";
        s.done = "active";
      } else if (verificationStarted) {
        s.execute = "complete";
        s.verify = "paused";
      } else s.execute = "paused";
      break;
    case "completed":
      s.execute = s.verify = s.done = "complete";
      break;
    case "needs_clarification":
      s.plan = "paused"; // stopped before planning; nothing ran (the page does not show the rail)
      break;
    case "failed":
      if (!planned) s.plan = "failed";
      else if (verificationStarted && verificationFailed) {
        s.execute = "complete";
        s.verify = "failed";
      } else s.execute = "failed";
      break;
  }

  return [
    { id: "plan", label: "Plan", state: s.plan, at: null, note: planned ? plural(Object.keys(state.tasks).length, "task") : null },
    {
      id: "execute",
      label: "Execute",
      state: s.execute,
      at: offset(firstAt(events, "TaskStarted")),
      note: replans > 0 ? (replans === 1 ? "Replanned once" : `Replanned ${replans} times`) : null,
    },
    { id: "verify", label: "Verify", state: s.verify, at: offset(firstAt(events, "VerificationStarted")), note: null },
    {
      id: "done",
      label: phase === "failed" ? "Failed" : "Done",
      state: s.done,
      at: offset(firstAt(events, "RunCompleted", "RunFailed")),
      note: null,
    },
  ];
}

// --- Tasks -----------------------------------------------------------------------------------

export type TaskKind = "work" | "verification" | "action" | "resolution";

export interface ToolCount {
  tool: string;
  count: number;
  failed: number;
}

export interface TaskView {
  id: string;
  title: string;
  role: string;
  hue: RoleHue;
  kind: TaskKind;
  status: TaskState["status"];
  statusLabel: string;
  tone: Tone;
  /** One line: the outcome, what it waits for, or why it failed. */
  detail: string | null;
  /** What it is doing right now, from its latest in-flight tool call. */
  now: string | null;
  tools: ToolCount[];
  toolSummary: string | null;
  replaces: string | null;
  replacedBy: string | null;
}

/** The task that currently stands for `taskId`: itself, or the end of its replacement chain. */
export function standIn(state: RunState, taskId: string): string {
  const seen = new Set<string>();
  let id = taskId;
  while (state.tasks[id]?.replaced_by && !seen.has(id)) {
    seen.add(id);
    id = state.tasks[id].replaced_by!;
  }
  return id;
}

export function taskKind(task: TaskState): TaskKind {
  if (task.verification) return "verification";
  if (task.action) return "action";
  if (task.conflict_id) return "resolution";
  return "work";
}

export function workTasks(state: RunState): TaskState[] {
  return Object.values(state.tasks).filter((t) => taskKind(t) !== "verification");
}

function callsOf(state: RunState, taskId: string): ToolCallState[] {
  return Object.values(state.tool_calls)
    .filter((c) => c.task_id === taskId)
    .sort((a, b) => a.sequence - b.sequence);
}

function toolCounts(calls: ToolCallState[]): ToolCount[] {
  const by = new Map<string, ToolCount>();
  for (const c of calls) {
    const entry = by.get(c.tool_name) ?? { tool: c.tool_name, count: 0, failed: 0 };
    entry.count += 1;
    if (c.status === "failed") entry.failed += 1;
    by.set(c.tool_name, entry);
  }
  return [...by.values()];
}

export function toolSummary(counts: ToolCount[]): string | null {
  if (counts.length === 0) return null;
  return counts.map((c) => countTools(c.tool, c.count) + (c.failed > 0 ? ` (${c.failed} failed)` : "")).join(", ");
}

function target(call: ToolCallState): string | null {
  const a = call.arguments;
  const url = str(a.url);
  if (url) return hostOf(url);
  return str(a.query) ?? str(a.expression);
}

function nowDoing(call: ToolCallState | undefined): string | null {
  if (!call || call.status !== "requested") return null;
  const t = target(call);
  switch (call.tool_name) {
    case "http_fetch":
      return t ? `Fetching ${t}` : "Fetching a page";
    case "web_search":
      return t ? `Searching for “${truncate(t, 60)}”` : "Searching the web";
    case "calculator":
      return "Calculating";
    default:
      return `Using ${call.tool_name.replace(/[_-]+/g, " ")}`;
  }
}

const titleOf = (state: RunState, taskId: string) => state.tasks[taskId]?.title ?? taskId;

function listTitles(titles: string[]): string {
  const quoted = titles.map((t) => `“${t}”`);
  return quoted.length <= 2 ? quoted.join(" and ") : `${quoted.slice(0, -1).join(", ")} and ${quoted[quoted.length - 1]}`;
}

export function taskView(state: RunState, task: TaskState): TaskView {
  const kind = taskKind(task);
  const calls = callsOf(state, task.task_id);
  const counts = toolCounts(calls);
  const verification = state.verifications[task.task_id];
  let statusLabel: string;
  let tone: Tone;
  let detail: string | null = null;

  switch (task.status) {
    case "running":
      statusLabel = kind === "verification" ? "Checking" : "Working";
      tone = "active";
      break;
    case "completed":
      statusLabel = kind === "verification" ? "Passed" : "Done";
      tone = "ok";
      detail = kind === "verification" ? null : task.summary ? truncate(task.summary, 220) : null;
      break;
    case "failed":
      statusLabel = task.replaced_by ? "Failed, replaced" : kind === "verification" && verification?.status === "failed" ? "Did not pass" : "Failed";
      tone = "bad";
      detail = kind === "verification" && verification?.reason ? truncate(verification.reason, 220) : failureLabel(task.failure?.failure_type) + ".";
      break;
    case "cancelled":
      statusLabel = "Cancelled";
      tone = "neutral";
      break;
    case "ready":
      statusLabel = "Up next";
      tone = "neutral";
      break;
    case "blocked":
      statusLabel = "Blocked";
      tone = "warn";
      break;
    case "pending": {
      statusLabel = "Waiting";
      tone = "neutral";
      // A failed dependency with a replacement stands for its replacement (as the scheduler resolves it).
      const open = [...new Set(task.dependencies.map((d) => standIn(state, d)))].filter((d) => state.tasks[d] && state.tasks[d].status !== "completed");
      if (open.length > 0) detail = kind === "verification" ? "Waits for the work to finish" : `Waits for ${listTitles(open.map((d) => titleOf(state, d)))}`;
      break;
    }
  }

  return {
    id: task.task_id,
    title: task.title,
    role: roleLabel(task.agent_type ?? (kind === "verification" ? "verifier" : null)),
    hue: roleHue(task.agent_type ?? (kind === "verification" ? "verifier" : null)),
    kind,
    status: task.status,
    statusLabel,
    tone,
    detail,
    now: task.status === "running" ? nowDoing(calls[calls.length - 1]) : null,
    tools: counts,
    toolSummary: toolSummary(counts),
    replaces: task.replaces ? titleOf(state, task.replaces) : null,
    replacedBy: task.replaced_by ? titleOf(state, task.replaced_by) : null,
  };
}

/** Columns of task views by dependency depth (see graph.ts). */
export function taskOrder(state: RunState): TaskState[] {
  return Object.values(state.tasks).sort((a, b) => a.created_at.localeCompare(b.created_at));
}

// --- Evidence ----------------------------------------------------------------------------------

export interface EvidenceItem {
  factId: string;
  statement: string;
  source: string | null;
  sourceLabel: string | null;
  claim: { attribute: string; value: string } | null;
  toolName: string | null;
  toolCallId: string | null;
  httpStatus: number | null;
  contentType: string | null;
  retrievedAt: string | null;
  fake: boolean;
}

/** Tool results recorded in ToolSucceeded events, by tool call id. */
function toolResults(events: NexusEvent[]): Map<string, { at: string; result: Record<string, unknown> }> {
  const out = new Map<string, { at: string; result: Record<string, unknown> }>();
  for (const e of events) {
    if (e.event_type !== "ToolSucceeded") continue;
    const id = str(e.payload.tool_call_id);
    if (id) out.set(id, { at: e.timestamp, result: isRecord(e.payload.result) ? e.payload.result : {} });
  }
  return out;
}

const claimText = (fact: Fact) =>
  fact.claim ? { attribute: fact.claim.attribute.replace(/_/g, " "), value: `${String(fact.claim.value)}${fact.claim.unit ? ` ${fact.claim.unit}` : ""}` } : null;

/** Facts that came from a tool call (provenance `tool_output`), in the order they were added. */
export function toolEvidence(state: RunState, events: NexusEvent[]): EvidenceItem[] {
  const results = toolResults(events);
  return Object.values(state.facts)
    .filter((f) => f.provenance?.kind === "tool_output")
    .sort((a, b) => a.sequence - b.sequence)
    .map((f) => {
      const p = f.provenance!;
      const source = p.source ?? f.source;
      const recorded = p.tool_call_id ? results.get(p.tool_call_id) : undefined;
      const status = recorded?.result.status_code;
      return {
        factId: f.fact_id,
        statement: f.content,
        source,
        sourceLabel: source ? hostOf(source) : null,
        claim: claimText(f),
        toolName: p.tool_name,
        toolCallId: p.tool_call_id,
        httpStatus: typeof status === "number" ? status : null,
        contentType: str(recorded?.result.content_type),
        retrievedAt: recorded?.at ?? null,
        fake: p.fake,
      };
    });
}

/** Facts agents stated from their task context or knowledge (not directly from a tool). */
export function derivedFacts(state: RunState): Fact[] {
  return Object.values(state.facts)
    .filter((f) => f.provenance?.kind !== "tool_output")
    .sort((a, b) => a.sequence - b.sequence);
}

// --- Verification ------------------------------------------------------------------------------

export type Verdict = "verified" | "failed" | "running" | "pending" | "unverified";

export interface TrustItem {
  label: string;
  passed: boolean;
  detail: string;
}

export interface TrustView {
  verdict: Verdict;
  items: TrustItem[];
  review: { passed: boolean; explanation: string; reviewer: string } | null;
  reason: string | null;
  /** Earlier attempts that failed and were superseded (recovery happened). */
  earlierFailures: string[];
}

export function trust(state: RunState, result: FinalResult | null): TrustView {
  const attempts = Object.values(state.verifications).sort((a, b) => a.attempt - b.attempt);
  const current = attempts.filter((v) => !v.replaced_by);
  const phase = phaseOf(state, result);
  const verdict: Verdict = result?.verified
    ? "verified"
    : current.some((v) => v.status === "failed" || v.status === "error")
      ? "failed"
      : current.some((v) => v.status === "running")
        ? "running"
        : phase === "completed" || phase === "failed"
          ? "unverified"
          : "pending";

  const items: TrustItem[] = [];
  let review: TrustView["review"] = null;
  let reason: string | null = null;
  for (const v of current) {
    for (const c of v.checks) items.push({ label: checkLabel(c.kind, c.passed, c.message), passed: c.passed, detail: c.message });
    if (v.semantic) {
      review = {
        passed: v.semantic.passed,
        explanation: v.semantic.objective.explanation || v.semantic.summary,
        reviewer: `${v.semantic.provider} / ${v.semantic.model}`,
      };
    }
    reason ??= v.reason;
  }
  const earlierFailures = attempts.filter((v) => v.replaced_by && v.reason).map((v) => v.reason!);
  return { verdict, items, review, reason, earlierFailures };
}

// --- Recovery ----------------------------------------------------------------------------------

export interface RecoveryView {
  number: number;
  accepted: boolean;
  failedTitle: string;
  failure: string;
  strategy: string;
  replacements: { title: string; status: TaskState["status"]; recheck: boolean }[];
}

export function recovery(state: RunState): RecoveryView[] {
  return state.recovery.history.map((r) => {
    const ids = r.replacement_task_id ? [r.replacement_task_id, ...r.new_task_ids.filter((id) => id !== r.replacement_task_id)] : r.new_task_ids;
    return {
      number: r.replan_number,
      accepted: r.outcome === "accepted",
      failedTitle: titleOf(state, r.failed_task_id),
      failure: failureLabel(r.failure_type),
      strategy: r.summary,
      // New work first, then the repeated verification (a re-check, not a new approach).
      replacements: ids
        .filter((id) => state.tasks[id])
        .map((id) => ({ title: titleOf(state, id), status: state.tasks[id].status, recheck: taskKind(state.tasks[id]) === "verification" }))
        .sort((a, b) => Number(a.recheck) - Number(b.recheck)),
    };
  });
}

// --- Conflicts ---------------------------------------------------------------------------------

export interface ConflictView {
  id: string;
  topic: string;
  status: ConflictState["status"];
  claims: { value: string; source: string | null }[];
  accepted: { value: string; source: string | null } | null;
  evidence: string[];
  note: string | null;
  /** A resolution task exists for it. */
  investigating: boolean;
}

function claimOf(state: RunState, factId: string) {
  const f = state.facts[factId];
  if (!f) return null;
  const source = f.provenance?.source ?? f.source;
  return { value: f.claim ? `${String(f.claim.value)}${f.claim.unit ? ` ${f.claim.unit}` : ""}` : f.content, source: source ? hostOf(source) : null };
}

export function conflicts(state: RunState): ConflictView[] {
  return Object.values(state.conflicts).map((c) => ({
    id: c.conflict_id,
    topic: c.fact_key ? `${c.fact_key.attribute.replace(/_/g, " ")} of ${hostOf(c.fact_key.subject)}` : (c.description ?? "Facts disagree"),
    status: c.status,
    claims: c.fact_ids.map((id) => claimOf(state, id)).filter((x): x is NonNullable<typeof x> => x !== null),
    // A resolution is shown only when the backend names the accepted fact.
    accepted: c.status === "resolved" && c.resolved_fact_id ? claimOf(state, c.resolved_fact_id) : null,
    evidence: c.evidence_ids.map((id) => claimOf(state, id)?.source).filter((s): s is string => Boolean(s)),
    note: c.resolution,
    investigating: c.resolution_task_id !== null,
  }));
}

// --- Why a run stopped -------------------------------------------------------------------------

/**
 * Plain sentences for why a failed or stalled run stopped: the failed tasks nothing replaced,
 * from state. The backend's own wording (failure_reason, completion_blockers) stays available
 * as technical detail.
 */
export function stoppedBecause(state: RunState): string[] {
  const out: string[] = [];
  for (const task of taskOrder(state)) {
    if (task.status !== "failed" || task.replaced_by) continue;
    if (taskKind(task) === "verification") {
      const v = state.verifications[task.task_id];
      const failedCheck = v?.checks.find((c) => !c.passed);
      const why = failedCheck
        ? checkLabel(failedCheck.kind, false, failedCheck.message)
        : v?.semantic && !v.semantic.passed
          ? "An independent review found the objective not met"
          : null;
      out.push(`The result did not pass verification${why ? `: ${why.charAt(0).toLowerCase()}${why.slice(1)}` : ""}.`);
    } else {
      out.push(`“${task.title}” failed: ${failureLabel(task.failure?.failure_type).charAt(0).toLowerCase()}${failureLabel(task.failure?.failure_type).slice(1)}.`);
    }
  }
  const replans = state.recovery.replan_count;
  if (out.length > 0 && replans > 0) out.push(`NEXUS changed its plan ${replans === 1 ? "once" : `${replans} times`} before stopping.`);
  return out;
}

// --- Totals ------------------------------------------------------------------------------------

export function runTotals(state: RunState) {
  const calls = Object.values(state.tool_calls);
  const agents = new Set(Object.values(state.tasks).map((t) => t.agent_type).filter(Boolean));
  return {
    tasks: Object.keys(state.tasks).length,
    agents: agents.size,
    tools: toolSummary(toolCounts(calls)),
    toolCalls: calls.length,
  };
}

/** Time from RunCreated to `now` while the run is moving, otherwise to its last event. */
export function elapsed(events: NexusEvent[], live: boolean, now: number): string | null {
  const first = events[0];
  if (!first || first.event_type !== "RunCreated" || (!live && events.length < 2)) return null;
  const start = Date.parse(first.timestamp);
  const end = live ? now : Date.parse(events[events.length - 1].timestamp);
  return Number.isNaN(start) || Number.isNaN(end) || end - start < 1000 ? null : formatDuration(end - start);
}
