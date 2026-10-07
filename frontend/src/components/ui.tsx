import type { ReactNode } from "react";

import type { Tone } from "../lib/runView";

export const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-faint",
  active: "text-accent",
  ok: "text-ok",
  warn: "text-warn",
  bad: "text-bad",
  ask: "text-violet",
};

const TONE_BG: Record<Tone, string> = {
  neutral: "bg-line-strong",
  active: "bg-accent",
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  ask: "bg-violet",
};

type IconName = "check" | "cross" | "alert" | "dot" | "chevron" | "arrow-left" | "plus" | "external" | "download";

const PATHS: Record<IconName, ReactNode> = {
  check: <path d="M3.5 8.5l3 3 6-7" />,
  cross: <path d="M4.5 4.5l7 7m0-7l-7 7" />,
  alert: (
    <>
      <path d="M8 4.5v4.5" />
      <path d="M8 11.5v.01" />
    </>
  ),
  dot: <circle cx="8" cy="8" r="2.5" fill="currentColor" stroke="none" />,
  chevron: <path d="M6 4l4 4-4 4" />,
  "arrow-left": <path d="M12.5 8h-9m4-4l-4 4 4 4" />,
  plus: <path d="M8 3.5v9m-4.5-4.5h9" />,
  external: <path d="M6.5 3.5h-3v9h9v-3m-4-6h4v4m0-4l-6 6" />,
  download: <path d="M8 3v7m-3-3l3 3 3-3M3.5 12.5h9" />,
};

export function Icon({ name, className = "" }: { name: IconName; className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={`size-4 shrink-0 ${className}`} aria-hidden="true">
      {PATHS[name]}
    </svg>
  );
}

/** A small status dot; `live` breathes slowly (and stays still with reduced motion). */
export function StatusDot({ tone, live = false, className = "" }: { tone: Tone; live?: boolean; className?: string }) {
  return <span className={`inline-block size-2 shrink-0 rounded-full ${TONE_BG[tone]} ${live ? "animate-live" : ""} ${className}`} aria-hidden="true" />;
}

/** The mark for a pass/fail line. */
export function Mark({ passed }: { passed: boolean }) {
  return (
    <span className={`mt-0.5 grid size-5 shrink-0 place-items-center rounded-full ${passed ? "bg-ok/12 text-ok" : "bg-bad/12 text-bad"}`}>
      <Icon name={passed ? "check" : "cross"} className="size-3.5" />
    </span>
  );
}

/** A titled section of the run page: whitespace and a heading, not a card. */
export function Section({ title, aside, children, className = "", id }: { title: string; aside?: ReactNode; children: ReactNode; className?: string; id?: string }) {
  const headingId = `${id ?? title.toLowerCase().replace(/\W+/g, "-")}-heading`;
  return (
    <section aria-labelledby={headingId} className={`min-w-0 ${className}`}>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id={headingId} className="text-[15px] font-semibold text-ink">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** The backend's exact wording, collapsed under a plain-language explanation. */
export function TechnicalDetail({ children, label = "Technical details" }: { children: ReactNode; label?: string }) {
  return (
    <details className="group mt-2">
      <summary className="inline-flex items-center gap-1 text-xs text-faint hover:text-ink">
        <Icon name="chevron" className="size-3 transition-transform group-open:rotate-90" />
        {label}
      </summary>
      <div className="mt-1.5 max-w-[80ch] font-mono text-xs leading-relaxed break-words text-dim">{children}</div>
    </details>
  );
}

export const buttonClass = {
  primary:
    "inline-flex items-center justify-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-ink transition-[opacity,transform] hover:opacity-90 active:translate-y-px disabled:cursor-not-allowed disabled:opacity-40",
  secondary:
    "inline-flex items-center justify-center gap-2 rounded-md border border-line-strong bg-surface px-3.5 py-2 text-sm font-medium text-ink transition-colors hover:border-ink/40 disabled:cursor-not-allowed disabled:opacity-40",
  quiet: "inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-sm text-dim transition-colors hover:text-ink",
};
