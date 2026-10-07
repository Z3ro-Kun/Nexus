import { conflicts } from "../../lib/runView";
import type { RunState } from "../../types/api";
import { Section, TechnicalDetail } from "../ui";

/** Facts that disagreed, and what the backend decided. A winner is shown only if it names one. */
export default function ConflictSection({ state }: { state: RunState }) {
  const views = conflicts(state);
  if (views.length === 0) return null;

  return (
    <Section title="Conflicting information">
      <ul className="space-y-6">
        {views.map((c) => (
          <li key={c.id} aria-label={`Conflict ${c.id}`} className="max-w-[68ch]">
            <p className="text-[15px] text-ink">
              Sources disagree about the <span className="font-medium">{c.topic}</span>.
            </p>
            <ul className="mt-3 divide-y divide-line border-y border-line text-sm">
              {c.claims.map((claim, i) => (
                <li key={i} className="flex flex-wrap items-baseline justify-between gap-x-4 py-2">
                  <span className="text-dim">{claim.source ?? "Unattributed"}</span>
                  <span className="font-medium text-ink">{claim.value}</span>
                </li>
              ))}
            </ul>
            {c.status === "resolved" && c.accepted ? (
              <p className="mt-3 text-sm text-ok">
                Resolved: <span className="font-medium">{c.accepted.value}</span>
                {c.evidence.length > 0 && <span className="text-dim">, backed by {c.evidence.join(", ")}</span>}
              </p>
            ) : c.status === "resolved" ? (
              <p className="mt-3 text-sm text-dim">Marked resolved, without a single accepted value.</p>
            ) : c.status === "unresolved" ? (
              <p className="mt-3 text-sm text-warn">Conflict remains unresolved. No independent evidence settled it, so NEXUS did not pick a side.</p>
            ) : c.investigating ? (
              <p className="mt-3 text-sm text-accent">NEXUS is investigating the disagreement.</p>
            ) : (
              <p className="mt-3 text-sm text-dim">Not resolved yet.</p>
            )}
            {c.note && <TechnicalDetail>{c.note}</TechnicalDetail>}
          </li>
        ))}
      </ul>
    </Section>
  );
}
