import { recovery } from "../../lib/runView";
import type { RunState } from "../../types/api";
import { Section } from "../ui";

const STATUS: Record<string, string> = {
  completed: "succeeded",
  running: "is running",
  failed: "also failed",
  pending: "is waiting to run",
  ready: "is about to run",
  blocked: "is blocked",
  cancelled: "was cancelled",
};

/** Replans the backend recorded: what failed, how NEXUS changed the plan, and how that went. */
export default function RecoverySection({ state }: { state: RunState }) {
  const records = recovery(state);
  if (records.length === 0) return null;

  return (
    <Section title="Recovery" aside={<p className="text-sm text-faint">NEXUS changed its plan {records.filter((r) => r.accepted).length === 1 ? "once" : `${records.filter((r) => r.accepted).length} times`}</p>}>
      <ol className="space-y-6">
        {records.map((r) => (
          <li key={r.number} aria-label={`Replan ${r.number}`}>
            <ol className="relative space-y-3 border-l border-line-strong pl-5">
              <Step tone="bad" title={`“${r.failedTitle}” failed`} body={`${r.failure}.`} />
              {r.accepted ? (
                <>
                  <Step tone="warn" title="NEXUS changed the plan" body={r.strategy} />
                  {r.replacements.map((t) => (
                    <Step
                      key={t.title}
                      tone={t.status === "completed" ? "ok" : t.status === "failed" ? "bad" : "active"}
                      title={t.recheck ? "Verified again" : `New approach: “${t.title}”`}
                      body={t.recheck ? (t.status === "completed" ? "It passed." : t.status === "failed" ? "It did not pass." : `It ${STATUS[t.status] ?? t.status}.`) : `It ${STATUS[t.status] ?? t.status}.`}
                    />
                  ))}
                </>
              ) : (
                <Step tone="bad" title="The new plan was rejected" body={r.strategy} />
              )}
            </ol>
          </li>
        ))}
      </ol>
    </Section>
  );
}

const DOT = { ok: "bg-ok", bad: "bg-bad", warn: "bg-warn", active: "bg-accent" } as const;

/** Steps appear once, in order (original path, issue, new approach); nothing loops. */
function Step({ tone, title, body }: { tone: keyof typeof DOT; title: string; body: string }) {
  return (
    <li className="relative animate-enter [&:nth-child(2)]:[animation-delay:180ms] [&:nth-child(3)]:[animation-delay:360ms] [&:nth-child(4)]:[animation-delay:540ms]">
      <span aria-hidden="true" className={`absolute top-[7px] -left-[24.5px] size-2 rounded-full ring-4 ring-canvas ${DOT[tone]}`} />
      <p className="text-[15px] font-medium text-ink">{title}</p>
      {body && <p className="mt-0.5 max-w-[68ch] text-sm leading-relaxed text-dim">{body}</p>}
    </li>
  );
}
