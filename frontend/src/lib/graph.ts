import type { TaskState } from "../types/api";

/**
 * Columns of the task graph from the backend's dependency lists only (no inferred edges).
 * A task's column is 1 + the deepest column among its dependencies, so independent tasks
 * share a column (they can run in parallel) and a chain becomes consecutive columns.
 */
export function graphColumns(tasks: Record<string, TaskState>): TaskState[][] {
  const depth = new Map<string, number>();
  const visiting = new Set<string>();

  function columnOf(taskId: string): number {
    const known = depth.get(taskId);
    if (known !== undefined) return known;
    if (visiting.has(taskId)) return 0; // cycles are rejected by the backend; never loop here
    visiting.add(taskId);
    const deps = (tasks[taskId]?.dependencies ?? []).filter((d) => d in tasks);
    const value = deps.length === 0 ? 0 : 1 + Math.max(...deps.map(columnOf));
    visiting.delete(taskId);
    depth.set(taskId, value);
    return value;
  }

  const columns: TaskState[][] = [];
  for (const task of Object.values(tasks).sort((a, b) => a.created_at.localeCompare(b.created_at))) {
    const column = columnOf(task.task_id);
    (columns[column] ??= []).push(task);
  }
  return columns.filter(Boolean);
}

/** Tasks that list `taskId` as a dependency (they consume its results). */
export function dependentsOf(tasks: Record<string, TaskState>, taskId: string): string[] {
  return Object.values(tasks)
    .filter((t) => t.dependencies.includes(taskId))
    .map((t) => t.task_id);
}
