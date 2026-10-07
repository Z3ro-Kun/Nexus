import { preview, truncate } from "../../../lib/format";
import type { RunState, ToolCallState } from "../../../types/api";
import { Badge, Empty, Mono, Panel } from "./ui";

/** Agent → task → tool calls → evidence, for every task that has started. */
export default function ActivityPanel({ state }: { state: RunState | null }) {
  const started = Object.values(state?.tasks ?? {}).filter((t) => t.started_at !== null);

  return (
    <Panel title="Agents & tools">
      {started.length === 0 ? (
        <Empty>No agent has started yet.</Empty>
      ) : (
        <ul className="space-y-4">
          {started.map((task) => {
            const calls = Object.values(state!.tool_calls)
              .filter((c) => c.task_id === task.task_id)
              .sort((a, b) => a.sequence - b.sequence);
            const facts = Object.values(state!.facts).filter((f) => f.task_id === task.task_id);
            const artifacts = Object.values(state!.artifacts).filter((a) => a.task_id === task.task_id);
            return (
              <li key={task.task_id} aria-label={`Activity ${task.task_id}`} className="border-l border-line pl-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Mono className="text-accent">{task.agent_type ?? "agent"}</Mono>
                  <Mono className="text-faint">→</Mono>
                  <Mono className="text-ink">{task.task_id}</Mono>
                  <Badge value={task.status} />
                </div>
                {calls.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {calls.map((call) => (
                      <ToolCallRow key={call.tool_call_id} call={call} />
                    ))}
                  </ul>
                )}
                {(facts.length > 0 || artifacts.length > 0) && (
                  <p className="mt-1.5 font-mono text-[11px] text-dim">
                    ↳ {facts.length} facts, {artifacts.length} artifacts written to shared state
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

function ToolCallRow({ call }: { call: ToolCallState }) {
  const target = call.arguments.url ?? call.arguments.expression ?? call.arguments.query;
  return (
    <li className="flex flex-wrap items-baseline gap-2 text-xs">
      <Mono className="text-faint">{call.tool_call_id}</Mono>
      <Mono className="text-ink">{call.tool_name}</Mono>
      {target !== undefined && <span className="break-all text-dim">{preview(target, 80)}</span>}
      <Badge value={call.status} />
      {call.metadata.fake === true && <Badge value="fake" tone="warn" />}
      {call.status === "failed" && call.error && (
        <span className="basis-full text-bad">
          {call.error_type ? `[${call.error_type}] ` : ""}
          {truncate(call.error, 160)}
        </span>
      )}
    </li>
  );
}
