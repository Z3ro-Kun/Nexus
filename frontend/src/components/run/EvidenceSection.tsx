import { formatTime } from "../../lib/format";
import { toolLabel } from "../../lib/labels";
import { derivedFacts, plural, toolEvidence, type EvidenceItem } from "../../lib/runView";
import type { NexusEvent, RunState } from "../../types/api";
import { Icon, Section } from "../ui";

/** Findings that came straight from tool calls, with where they came from. */
export default function EvidenceSection({ state, events }: { state: RunState; events: NexusEvent[] }) {
  const items = toolEvidence(state, events);
  const derived = derivedFacts(state);
  if (items.length === 0 && derived.length === 0) return null;
  const sources = new Set(items.map((i) => i.source).filter(Boolean)).size;

  return (
    <Section title="Evidence" aside={sources > 0 ? <p className="text-sm text-faint">From {plural(sources, "source")}</p> : null}>
      {items.length > 0 ? (
        <ul className="divide-y divide-line border-y border-line" aria-label="Tool evidence">
          {items.map((item) => (
            <EvidenceRow key={item.factId} item={item} />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-dim">No finding has come from a tool call yet.</p>
      )}

      {derived.length > 0 && (
        <details className="group mt-4">
          <summary className="inline-flex items-center gap-1.5 text-sm text-dim hover:text-ink">
            <Icon name="chevron" className="size-3.5 transition-transform group-open:rotate-90" />
            {plural(derived.length, "statement")} made by agents from their task context
          </summary>
          <ul className="mt-3 space-y-2 pl-5">
            {derived.map((f) => (
              <li key={f.fact_id} className="max-w-[68ch] text-sm text-dim">
                {f.content}
              </li>
            ))}
          </ul>
        </details>
      )}
    </Section>
  );
}

function EvidenceRow({ item }: { item: EvidenceItem }) {
  const isUrl = item.source !== null && /^https?:\/\//.test(item.source);
  return (
    <li className="py-3.5">
      <div className="grid gap-x-6 gap-y-1 sm:grid-cols-[minmax(0,14rem)_1fr]">
        <p className="min-w-0 text-sm font-medium break-all text-ink">
          {isUrl ? (
            <a href={item.source!} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-accent">
              {item.sourceLabel}
              <Icon name="external" className="size-3 text-faint" />
              <span className="sr-only">(opens in a new tab)</span>
            </a>
          ) : (
            (item.sourceLabel ?? "Unnamed source")
          )}
        </p>
        <div className="min-w-0">
          {item.claim ? (
            <p className="text-[15px] text-ink">
              <span className="text-dim">{item.claim.attribute}: </span>
              <span className="font-medium">{item.claim.value}</span>
            </p>
          ) : (
            <p className="text-[15px] text-ink">{item.statement}</p>
          )}
          <p className="mt-0.5 text-xs text-faint">
            {[
              item.toolName && `${capitalize(toolLabel(item.toolName))}`,
              item.httpStatus !== null && `HTTP ${item.httpStatus}`,
              item.retrievedAt && `at ${formatTime(item.retrievedAt)}`,
            ]
              .filter(Boolean)
              .join(", ")}
          </p>
          {item.fake && <p className="mt-1 text-xs font-medium text-warn">From a simulated tool, not a live source</p>}
          <details className="group mt-1">
            <summary className="inline-flex items-center gap-1 text-xs text-faint hover:text-ink">
              <Icon name="chevron" className="size-3 transition-transform group-open:rotate-90" />
              Source record
            </summary>
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
              <dt className="text-faint">Statement</dt>
              <dd className="text-dim">{item.statement}</dd>
              {item.source && (
                <>
                  <dt className="text-faint">Source</dt>
                  <dd className="break-all text-dim">{item.source}</dd>
                </>
              )}
              {item.contentType && (
                <>
                  <dt className="text-faint">Content type</dt>
                  <dd className="text-dim">{item.contentType}</dd>
                </>
              )}
              <dt className="text-faint">Tool call</dt>
              <dd className="font-mono text-dim">{item.toolCallId ?? "not recorded"}</dd>
              <dt className="text-faint">Fact</dt>
              <dd className="font-mono text-dim">{item.factId}</dd>
            </dl>
          </details>
        </div>
      </div>
    </li>
  );
}

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
