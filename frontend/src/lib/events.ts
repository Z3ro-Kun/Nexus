/**
 * One-line descriptions of real NEXUS events. Payload fields are read defensively (they
 * are untyped JSON), and an event type this file does not know still gets a description.
 */

import type { NexusEvent } from "../types/api";
import { preview, truncate } from "./format";

export type EventTone = "neutral" | "info" | "ok" | "warn" | "bad";

export interface EventDescription {
  tone: EventTone;
  /** Lifecycle chatter (dimmed) vs. events that change what the run means. */
  important: boolean;
  detail: string;
}

const str = (value: unknown): string | null => (typeof value === "string" && value.length > 0 ? value : null);
const list = (value: unknown): string[] => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []);
const join = (...parts: (string | null | false | undefined)[]) => parts.filter(Boolean).join(" · ");

export function describeEvent(event: NexusEvent): EventDescription {
  const p = event.payload;
  switch (event.event_type) {
    case "RunCreated":
      return { tone: "info", important: true, detail: truncate(str(p.goal) ?? "", 140) };
    case "TaskCreated":
      return {
        tone: "neutral",
        important: false,
        detail: join(
          str(p.task_id),
          str(p.agent_type),
          list(p.dependencies).length > 0 && `after ${list(p.dependencies).join(", ")}`,
          str(p.replaces) && `replaces ${str(p.replaces)}`,
          p.verification ? "verification checkpoint" : null,
        ),
      };
    case "TaskStarted":
      return { tone: "info", important: false, detail: str(p.task_id) ?? "" };
    case "TaskCompleted":
      return { tone: "ok", important: false, detail: join(str(p.task_id), str(p.summary) && truncate(str(p.summary)!, 120)) };
    case "TaskFailed":
      return { tone: "bad", important: true, detail: join(str(p.task_id), str(p.failure_type), truncate(str(p.error) ?? "", 140)) };
    case "TaskCancelled":
      return { tone: "warn", important: true, detail: join(str(p.task_id), str(p.reason)) };
    case "ToolCalled":
      return { tone: "info", important: false, detail: join(str(p.tool_call_id), str(p.tool_name), toolTarget(p.arguments)) };
    case "ToolSucceeded":
      return { tone: "ok", important: false, detail: join(str(p.tool_call_id), resultHint(p.result)) };
    case "ToolFailed":
      return { tone: "bad", important: true, detail: join(str(p.tool_call_id), str(p.error_type), truncate(str(p.error) ?? "", 140)) };
    case "FactAdded":
      return { tone: "neutral", important: false, detail: join(str(p.fact_id), truncate(str(p.content) ?? "", 120)) };
    case "ArtifactAdded":
      return { tone: "neutral", important: false, detail: join(str(p.artifact_id), str(p.name), str(p.media_type)) };
    case "ConflictDetected":
      return { tone: "warn", important: true, detail: join(str(p.conflict_id), str(p.conflict_type), truncate(str(p.reason) ?? str(p.description) ?? "", 140)) };
    case "ConflictResolved":
      return { tone: "ok", important: true, detail: join(str(p.conflict_id), str(p.resolved_fact_id) && `accepted ${str(p.resolved_fact_id)}`, truncate(str(p.reason) ?? "", 120)) };
    case "ConflictUnresolved":
      return { tone: "warn", important: true, detail: join(str(p.conflict_id), truncate(str(p.reason) ?? "", 140)) };
    case "ReplanTriggered":
      return {
        tone: "warn",
        important: true,
        detail: join(
          str(p.failed_task_id) && `failed ${str(p.failed_task_id)}`,
          str(p.replacement_task_id) && `replacement ${str(p.replacement_task_id)}`,
          truncate(str(p.strategy_summary) ?? str(p.reason) ?? "", 140),
        ),
      };
    case "ReplanRejected":
      return { tone: "bad", important: true, detail: join(str(p.failed_task_id), str(p.stage), truncate(str(p.reason) ?? "", 140)) };
    case "VerificationStarted":
      return { tone: "info", important: true, detail: join(str(p.verification_id), list(p.covered_task_ids).length > 0 && `covers ${list(p.covered_task_ids).join(", ")}`) };
    case "VerificationPassed":
      return { tone: "ok", important: true, detail: join(str(p.verification_id), checksHint(p.checks), semanticHint(p.semantic)) };
    case "VerificationFailed":
      return { tone: "bad", important: true, detail: join(str(p.verification_id), truncate(str(p.reason) ?? "", 140)) };
    case "PolicyEvaluated": {
      const d = typeof p.decision === "object" && p.decision !== null ? (p.decision as Record<string, unknown>) : {};
      return { tone: "info", important: true, detail: join(str(d.task_id), str(d.tool_name), str(d.outcome), truncate(str(d.reason) ?? "", 100)) };
    }
    case "ApprovalRequested":
      return { tone: "warn", important: true, detail: join(str(p.approval_id), truncate(str(p.description) ?? "", 140)) };
    case "ApprovalGranted":
      return { tone: "ok", important: true, detail: join(str(p.approval_id), str(p.actor)) };
    case "ApprovalRejected":
      return { tone: "bad", important: true, detail: join(str(p.approval_id), str(p.actor), str(p.reason)) };
    case "RunCompleted":
      return { tone: "ok", important: true, detail: truncate(str(p.summary) ?? "", 160) };
    case "RunFailed":
      return { tone: "bad", important: true, detail: truncate(str(p.reason) ?? "", 160) };
    case "ClarificationRequested":
      return { tone: "warn", important: true, detail: truncate(str(p.question) ?? "", 160) };
    default:
      return { tone: "neutral", important: false, detail: preview(p, 140) };
  }
}

function toolTarget(args: unknown): string | null {
  if (typeof args !== "object" || args === null) return null;
  const a = args as Record<string, unknown>;
  return str(a.url) ?? str(a.expression) ?? str(a.query) ?? null;
}

function resultHint(result: unknown): string | null {
  if (typeof result !== "object" || result === null) return null;
  const r = result as Record<string, unknown>;
  if (typeof r.status_code === "number") return `HTTP ${r.status_code}`;
  if (r.result !== undefined) return `= ${preview(r.result, 40)}`;
  return null;
}

function checksHint(checks: unknown): string | null {
  if (!Array.isArray(checks) || checks.length === 0) return null;
  const passed = checks.filter((c) => typeof c === "object" && c !== null && (c as Record<string, unknown>).passed === true).length;
  return `${passed}/${checks.length} checks`;
}

function semanticHint(semantic: unknown): string | null {
  if (typeof semantic !== "object" || semantic === null) return null;
  const s = semantic as Record<string, unknown>;
  return join(s.passed === true ? "semantic pass" : "semantic fail", str(s.model));
}
