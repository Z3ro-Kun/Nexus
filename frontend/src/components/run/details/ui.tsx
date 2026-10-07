import type { ReactNode } from "react";

export type Tone = "neutral" | "info" | "ok" | "warn" | "bad";

const BADGE: Record<Tone, string> = {
  neutral: "border-line text-dim",
  info: "border-accent/40 text-accent",
  ok: "border-ok/40 text-ok",
  warn: "border-warn/40 text-warn",
  bad: "border-bad/40 text-bad",
};

export const TEXT: Record<Tone, string> = {
  neutral: "text-dim",
  info: "text-accent",
  ok: "text-ok",
  warn: "text-warn",
  bad: "text-bad",
};

/** Tone of any backend status/phase value; unknown values are neutral. */
export function toneOf(value: string): Tone {
  switch (value) {
    case "completed":
    case "passed":
    case "succeeded":
    case "resolved":
    case "granted":
    case "accepted":
      return "ok";
    case "running":
    case "executing":
    case "verifying":
    case "requested":
    case "ready":
      return "info";
    case "blocked":
    case "waiting_for_approval":
    case "open":
    case "unresolved":
    case "pending":
      return value === "pending" ? "neutral" : "warn";
    case "failed":
    case "error":
    case "rejected":
    case "cancelled":
      return "bad";
    default:
      return "neutral";
  }
}

export function Badge({ value, tone, label }: { value: string; tone?: Tone; label?: string }) {
  const t = tone ?? toneOf(value);
  return (
    <span className={`inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 font-mono text-[11px] ${BADGE[t]}`}>
      {(value === "running" || value === "executing" || value === "verifying") && (
        <span className="size-1.5 animate-live rounded-full bg-current" aria-hidden="true" />
      )}
      {label ?? value.replaceAll("_", " ")}
    </span>
  );
}

/** A technical panel inside Execution details. */
export function Panel({ title, aside, children, className = "" }: { title: string; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={`min-w-0 border-t border-line pt-4 ${className}`}>
      <header className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {aside}
      </header>
      <div>{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm text-faint">{children}</p>;
}

export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <span translate="no" className={`font-mono text-xs ${className}`}>
      {children}
    </span>
  );
}
