import { useState } from "react";

import { dependentsOf } from "../../../lib/graph";
import { truncate } from "../../../lib/format";
import type { Fact, RunState } from "../../../types/api";
import { Badge, Empty, Mono, Panel } from "./ui";

/**
 * The materialized shared state (GET /state): the facts, artifacts and conflicts that events
 * produced, and which tasks consume them through their dependencies.
 */
export default function SharedStatePanel({ state }: { state: RunState | null }) {
  const [raw, setRaw] = useState(false);
  if (!state) return null;
  const facts = Object.values(state.facts).sort((a, b) => a.sequence - b.sequence);
  const artifacts = Object.values(state.artifacts).sort((a, b) => a.sequence - b.sequence);
  const conflicts = Object.values(state.conflicts);

  return (
    <Panel
      title="Shared state"
      aside={
        <button type="button" onClick={() => setRaw((r) => !r)} className="font-mono text-[11px] text-faint hover:text-ink">
          {raw ? "hide raw" : "raw"}
        </button>
      }
    >
      <p className="mb-4 text-xs text-faint">
        Built from events at sequence {state.last_sequence}. Tasks read their dependencies' results from here, not from each other.
      </p>

      <h3 className="mb-2 font-mono text-[11px] uppercase tracking-wider text-faint">Facts · {facts.length}</h3>
      {facts.length === 0 ? (
        <Empty>No facts yet.</Empty>
      ) : (
        <ul className="space-y-2">
          {facts.map((fact) => (
            <FactRow key={fact.fact_id} fact={fact} consumers={fact.task_id ? dependentsOf(state.tasks, fact.task_id) : []} />
          ))}
        </ul>
      )}

      <h3 className="mt-5 mb-2 font-mono text-[11px] uppercase tracking-wider text-faint">Artifacts · {artifacts.length}</h3>
      {artifacts.length === 0 ? (
        <Empty>No artifacts yet.</Empty>
      ) : (
        <ul className="space-y-2">
          {artifacts.map((a) => (
            <li key={a.artifact_id} className="rounded border border-line bg-raised px-3 py-2">
              <div className="flex flex-wrap items-baseline gap-2">
                <Mono className="text-ink">{a.name}</Mono>
                <Mono className="text-faint">{a.media_type}</Mono>
                <Mono className="text-faint">from {a.task_id ?? "—"}</Mono>
              </div>
              <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-dim">{truncate(a.content, 1200)}</pre>
            </li>
          ))}
        </ul>
      )}

      {conflicts.length > 0 && (
        <>
          <h3 className="mt-5 mb-2 font-mono text-[11px] uppercase tracking-wider text-faint">Conflicts · {conflicts.length}</h3>
          <ul className="space-y-2">
            {conflicts.map((c) => (
              <li key={c.conflict_id} className="rounded border border-warn/40 bg-raised px-3 py-2 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <Mono className="text-ink">{c.conflict_id}</Mono>
                  <Badge value={c.status} />
                  {c.fact_key && <Mono className="text-dim">{c.fact_key.subject} / {c.fact_key.attribute}</Mono>}
                </div>
                <p className="mt-1 font-mono text-[11px] text-faint">facts: {c.fact_ids.join(", ")}</p>
                {c.resolved_fact_id && <p className="mt-1 font-mono text-[11px] text-ok">accepted: {c.resolved_fact_id}</p>}
                {c.resolution && <p className="mt-1 text-dim">{truncate(c.resolution, 200)}</p>}
              </li>
            ))}
          </ul>
        </>
      )}

      {raw && (
        <pre aria-label="Raw state" className="mt-5 max-h-96 overflow-auto rounded border border-line bg-canvas p-3 font-mono text-[11px] text-faint">
          {JSON.stringify(state, null, 2)}
        </pre>
      )}
    </Panel>
  );
}

function FactRow({ fact, consumers }: { fact: Fact; consumers: string[] }) {
  const p = fact.provenance;
  return (
    <li className="rounded border border-line bg-raised px-3 py-2 text-xs">
      <p className="text-ink">{fact.content}</p>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px] text-faint">
        <span>{fact.fact_id}</span>
        {p && <span className={p.kind === "tool_output" ? "text-accent" : ""}>{p.kind}{p.tool_name ? ` · ${p.tool_name}` : ""}{p.tool_call_id ? ` · ${p.tool_call_id}` : ""}</span>}
        {p?.source && <span className="break-all">{p.source}</span>}
        {p?.fake && <span className="text-warn">fake source</span>}
        {fact.claim && (
          <span>
            claim: {fact.claim.subject} / {fact.claim.attribute} = {String(fact.claim.value)}
            {fact.claim.unit ? ` ${fact.claim.unit}` : ""}
          </span>
        )}
        {consumers.length > 0 && <span className="text-dim">→ available to {consumers.join(", ")}</span>}
      </div>
    </li>
  );
}
