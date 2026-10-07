import { graphColumns } from "../../../lib/graph";
import { truncate } from "../../../lib/format";
import type { RunState, TaskState } from "../../../types/api";
import { Badge, Empty, Mono, Panel } from "./ui";

/** The real task graph: columns by dependency depth, so parallel tasks stack in one column. */
export default function TaskGraph({ state }: { state: RunState | null }) {
  const tasks = state?.tasks ?? {};
  const columns = graphColumns(tasks);
  const count = Object.keys(tasks).length;

  return (
    <Panel title="Task graph" aside={<Mono className="text-faint">{count} tasks</Mono>}>
      {count === 0 ? (
        <Empty>No tasks yet. The planner creates the task graph when execution starts.</Empty>
      ) : (
        <ol className="flex gap-3 overflow-x-auto pb-1" aria-label="Task columns">
          {columns.map((column, index) => (
            <li key={index} className="flex items-center gap-3">
              {index > 0 && <span className="font-mono text-faint" aria-hidden="true">→</span>}
              <ul className="flex min-w-56 max-w-72 flex-col gap-3">
                {column.map((task) => (
                  <li key={task.task_id}>
                    <TaskCard task={task} state={state!} parallel={column.length > 1} />
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}

function TaskCard({ task, state, parallel }: { task: TaskState; state: RunState; parallel: boolean }) {
  const calls = Object.values(state.tool_calls).filter((c) => c.task_id === task.task_id);
  const facts = Object.values(state.facts).filter((f) => f.task_id === task.task_id).length;
  const artifacts = Object.values(state.artifacts).filter((a) => a.task_id === task.task_id).length;
  const kind = task.verification ? "verification" : task.action ? "action" : task.conflict_id ? "conflict resolution" : null;

  return (
    <article
      aria-label={`Task ${task.task_id}`}
      data-status={task.status}
      className={`rounded border bg-raised px-3 py-2.5 ${task.status === "running" ? "border-accent/60" : task.status === "failed" ? "border-bad/50" : "border-line"}`}
    >
      <div className="flex items-start justify-between gap-2">
        <Mono className="break-all text-ink">{task.task_id}</Mono>
        <Badge value={task.status} />
      </div>
      <p className="mt-1 text-xs text-dim">{truncate(task.title, 90)}</p>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-faint">
        {task.agent_type && <span>agent: {task.agent_type}</span>}
        {kind && <span>{kind}</span>}
        {parallel && <span>parallel</span>}
      </div>
      {task.dependencies.length > 0 && (
        <p className="mt-1 font-mono text-[11px] text-faint">after: {task.dependencies.join(", ")}</p>
      )}
      {(task.replaces || task.replaced_by) && (
        <p className="mt-1 font-mono text-[11px] text-warn">
          {task.replaces ? `replaces ${task.replaces}` : `replaced by ${task.replaced_by}`}
        </p>
      )}
      {(calls.length > 0 || facts > 0 || artifacts > 0) && (
        <p className="mt-1 font-mono text-[11px] text-dim">
          {calls.length} tool calls · {facts} facts · {artifacts} artifacts
        </p>
      )}
      {task.status === "failed" && task.error && <p className="mt-1 text-[11px] text-bad">{truncate(task.error, 140)}</p>}
    </article>
  );
}
