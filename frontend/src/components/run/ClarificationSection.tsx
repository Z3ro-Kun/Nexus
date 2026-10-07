import type { Clarification } from "../../types/api";
import { Section } from "../ui";

const REASON: Record<Clarification["reason"], string> = {
  underspecified: "The objective doesn't say what work you want done.",
  not_a_request: "This doesn't look like a request NEXUS can act on.",
};

/**
 * A run the planner stopped before planning, because the objective could not be executed
 * as stated. Nothing was planned or run; answering here is not supported yet, so the way
 * forward is a new run with a more specific objective.
 */
export default function ClarificationSection({ clarification }: { clarification: Clarification }) {
  return (
    <Section title="Needs clarification" id="clarification" className="rounded-xl border border-line bg-surface px-4 pt-5 pb-6 sm:-mx-6 sm:px-6">
      <div className="max-w-[68ch] border-l-2 border-violet pl-4">
        <p className="text-lg leading-relaxed text-pretty text-ink">{clarification.question}</p>
        <p className="mt-2 text-sm text-dim">{REASON[clarification.reason] ?? clarification.reason}</p>
      </div>
      {clarification.missing.length > 0 && (
        <div className="mt-6">
          <h3 id="missing-heading" className="text-sm font-medium text-ink">
            Missing
          </h3>
          <ul aria-labelledby="missing-heading" className="mt-2 space-y-1.5">
            {clarification.missing.map((m) => (
              <li key={m} className="flex items-baseline gap-2.5 text-[15px] text-ink">
                <span className="size-1.5 shrink-0 translate-y-[-2px] self-center rounded-full bg-violet" aria-hidden="true" />
                {m}
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="mt-6 text-sm text-dim">
        NEXUS didn't plan or run anything for this objective. To continue, run a new objective that says what you want done.
      </p>
    </Section>
  );
}
