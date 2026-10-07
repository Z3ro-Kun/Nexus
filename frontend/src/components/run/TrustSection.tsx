import { trust, type Verdict } from "../../lib/runView";
import type { FinalResult, RunState } from "../../types/api";
import { Icon, Mark, Section, StatusDot, TechnicalDetail } from "../ui";

const VERDICT: Record<Verdict, { label: string; className: string }> = {
  verified: { label: "Verified", className: "text-ok" },
  failed: { label: "Verification failed", className: "text-bad" },
  running: { label: "Checking now", className: "text-accent" },
  pending: { label: "Not verified yet", className: "text-faint" },
  unverified: { label: "Not verified", className: "text-warn" },
};

/**
 * Whether the result can be trusted, from the backend's own verification record. "Verified"
 * is shown only when the backend's `verified` flag says so.
 */
export default function TrustSection({ state, result }: { state: RunState; result: FinalResult | null }) {
  const view = trust(state, result);
  const hasRecord = view.items.length > 0 || view.review !== null;
  if (view.verdict === "pending" && !hasRecord) return null;
  const verdict = VERDICT[view.verdict];

  return (
    <Section
      title="Verification"
      aside={
        <p className={`flex items-center gap-1.5 text-sm font-semibold ${verdict.className}`}>
          {view.verdict === "verified" ? <Icon name="check" /> : view.verdict === "failed" ? <Icon name="cross" /> : view.verdict === "running" ? <StatusDot tone="active" live /> : null}
          {verdict.label}
        </p>
      }
    >
      {view.verdict === "unverified" && !hasRecord && <p className="text-sm text-dim">No verification result was recorded for this run.</p>}
      {view.verdict === "running" && !hasRecord && <p className="text-sm text-dim">An independent check of the result against the objective is running.</p>}

      {hasRecord && (
        <ul className="space-y-2.5" aria-label="Checks">
          {view.items.map((item, i) => (
            <li key={i} className="grid grid-cols-[auto_1fr] gap-x-3">
              <Mark passed={item.passed} />
              <div className="min-w-0">
                <p className="text-[15px] text-ink">{item.label}</p>
                {!item.passed && <TechnicalDetail>{item.detail}</TechnicalDetail>}
              </div>
            </li>
          ))}
          {view.review && (
            <li className="grid grid-cols-[auto_1fr] gap-x-3">
              <Mark passed={view.review.passed} />
              <div className="min-w-0">
                <p className="text-[15px] text-ink">{view.review.passed ? "An independent review confirms the objective is met" : "An independent review found the objective not met"}</p>
                {view.review.explanation && <p className="mt-1 max-w-[68ch] text-sm leading-relaxed text-dim">{view.review.explanation}</p>}
                <p className="mt-1 text-xs text-faint">Reviewed by {view.review.reviewer}</p>
              </div>
            </li>
          )}
        </ul>
      )}

      {/* The reason is only news when no failed check or review above already explains it. */}
      {view.verdict === "failed" && view.reason && !view.items.some((i) => !i.passed) && !(view.review && !view.review.passed) && (
        <p className="mt-4 max-w-[68ch] border-l-2 border-bad pl-4 text-sm leading-relaxed text-dim">{view.reason}</p>
      )}

      {view.earlierFailures.length > 0 && (
        <div className="mt-4 max-w-[68ch] text-sm text-dim">
          <p>
            These are the results of attempt {view.earlierFailures.length + 1}. {view.earlierFailures.length === 1 ? "The earlier attempt" : `The ${view.earlierFailures.length} earlier attempts`} did
            not pass.
          </p>
          <TechnicalDetail label="Earlier attempts">
            {view.earlierFailures.map((r, i) => (
              <p key={i}>{r}</p>
            ))}
          </TechnicalDetail>
        </div>
      )}
    </Section>
  );
}
