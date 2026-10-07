/**
 * Layout of the plan as a top-to-bottom graph, from the backend's dependency lists only.
 *
 * - Layers are dependency depth: tasks with no unfinished relationship between them share a
 *   layer, so tasks that can run at the same time sit side by side.
 * - Edges are drawn after a transitive reduction: an edge d -> t is omitted when another
 *   dependency of t already depends on d (the verifier depends on every task, but its edge
 *   from a researcher would only cross the analyst). Reachability is unchanged; the full
 *   dependency lists stay in Execution details.
 * - A task with several dependencies receives them through a junction just above it, where
 *   their results meet in shared state.
 * - A replacement task (recovery) is linked to the task it replaces by a separate edge.
 */

import type { TaskState } from "../types/api";

export interface LayoutNode {
  id: string;
  planner: boolean;
  layer: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface LayoutEdge {
  id: string;
  from: string;
  /** A task id, or a junction id. */
  to: string;
  kind: "plan" | "dependency" | "merge" | "replacement";
  path: string;
}

export interface Junction {
  id: string;
  /** The task the merged results flow into. */
  into: string;
  from: string[];
  x: number;
  y: number;
}

export interface GraphLayout {
  width: number;
  height: number;
  compact: boolean;
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  junctions: Junction[];
  /** Reduced dependencies actually drawn, by task id. */
  drawnDependencies: Record<string, string[]>;
}

export const PLANNER_ID = "__planner__";

const curve = (x1: number, y1: number, x2: number, y2: number) => {
  const my = (y1 + y2) / 2;
  return `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`;
};

/** Dependencies that are not implied by another dependency of the same task. */
export function reducedDependencies(tasks: Record<string, TaskState>): Record<string, string[]> {
  const ancestors = new Map<string, Set<string>>();
  const visiting = new Set<string>();
  const ancestorsOf = (id: string): Set<string> => {
    const known = ancestors.get(id);
    if (known) return known;
    if (visiting.has(id)) return new Set(); // cycles are rejected by the backend
    visiting.add(id);
    const out = new Set<string>();
    for (const d of tasks[id]?.dependencies ?? []) {
      if (!(d in tasks)) continue;
      out.add(d);
      ancestorsOf(d).forEach((a) => out.add(a));
    }
    visiting.delete(id);
    ancestors.set(id, out);
    return out;
  };
  const reduced: Record<string, string[]> = {};
  for (const t of Object.values(tasks)) {
    const deps = t.dependencies.filter((d) => d in tasks);
    reduced[t.task_id] = deps.filter((d) => !deps.some((e) => e !== d && ancestorsOf(e).has(d)));
  }
  return reduced;
}

export function layoutGraph(tasks: Record<string, TaskState>, withPlanner: boolean, availableWidth: number): GraphLayout {
  const ordered = Object.values(tasks).sort((a, b) => a.created_at.localeCompare(b.created_at));
  const reduced = reducedDependencies(tasks);
  const compact = availableWidth < 560;

  // Depth = longest dependency chain (identical with full or reduced dependencies).
  const depth = new Map<string, number>();
  const depthOf = (id: string, seen = new Set<string>()): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (seen.has(id)) return 0;
    seen.add(id);
    const deps = reduced[id] ?? [];
    const value = deps.length === 0 ? 0 : 1 + Math.max(...deps.map((d) => depthOf(d, seen)));
    depth.set(id, value);
    return value;
  };
  const layers: TaskState[][] = [];
  for (const t of ordered) (layers[depthOf(t.task_id)] ??= []).push(t);
  const filled = layers.filter(Boolean);

  const GAP = compact ? 8 : 16;
  const VGAP = compact ? 46 : 58;
  const H = compact ? 66 : 78;
  const kmax = Math.max(1, ...filled.map((l) => l.length));
  const w = Math.max(compact ? 108 : 132, Math.min(232, (availableWidth - (kmax - 1) * GAP) / kmax));
  const width = Math.max(availableWidth, kmax * w + (kmax - 1) * GAP);
  const PH = 40;
  const top0 = 2 + (withPlanner ? PH + VGAP : 0);

  const nodes: LayoutNode[] = [];
  const at = new Map<string, LayoutNode>();
  if (withPlanner) {
    const pw = Math.min(Math.max(w, 150), 180, width - 8);
    const planner = { id: PLANNER_ID, planner: true, layer: -1, x: (width - pw) / 2, y: 2, w: pw, h: PH };
    nodes.push(planner);
    at.set(PLANNER_ID, planner);
  }

  filled.forEach((layer, i) => {
    // Order by the mean position of the drawn parents (fewer crossings); a replacement goes
    // right after the task it replaces; ties keep creation order.
    const key = (t: TaskState, index: number) => {
      const parents = (reduced[t.task_id] ?? []).map((d) => at.get(d)).filter((n): n is LayoutNode => Boolean(n));
      if (parents.length > 0) return parents.reduce((s, n) => s + n.x + n.w / 2, 0) / parents.length + index * 1e-3;
      const replaced = t.replaces ? layer.findIndex((x) => x.task_id === t.replaces) : -1;
      return (replaced >= 0 ? replaced + 0.5 : index) * 1e-2;
    };
    const sorted = layer.map((t, index) => ({ t, k: key(t, index) })).sort((a, b) => a.k - b.k).map((x) => x.t);
    const total = sorted.length * w + (sorted.length - 1) * GAP;
    const start = (width - total) / 2;
    sorted.forEach((t, j) => {
      const node = { id: t.task_id, planner: false, layer: i, x: start + j * (w + GAP), y: top0 + i * (H + VGAP), w, h: H };
      nodes.push(node);
      at.set(t.task_id, node);
    });
  });

  const edges: LayoutEdge[] = [];
  const junctions: Junction[] = [];
  const bottom = (n: LayoutNode) => [n.x + n.w / 2, n.y + n.h] as const;
  const topOf = (n: LayoutNode) => [n.x + n.w / 2, n.y] as const;

  for (const t of ordered) {
    const node = at.get(t.task_id)!;
    const deps = reduced[t.task_id] ?? [];
    if (deps.length === 0) {
      if (t.replaces && at.get(t.replaces)) continue; // drawn as a replacement edge below
      if (withPlanner) {
        const p = at.get(PLANNER_ID)!;
        edges.push({ id: `plan>${t.task_id}`, from: PLANNER_ID, to: t.task_id, kind: "plan", path: curve(...bottom(p), ...topOf(node)) });
      }
      continue;
    }
    if (deps.length === 1) {
      const d = at.get(deps[0])!;
      edges.push({ id: `${deps[0]}>${t.task_id}`, from: deps[0], to: t.task_id, kind: "dependency", path: curve(...bottom(d), ...topOf(node)) });
      continue;
    }
    const [jx, ty] = topOf(node);
    const jy = ty - VGAP * 0.42;
    const junction = { id: `join>${t.task_id}`, into: t.task_id, from: deps, x: jx, y: jy };
    junctions.push(junction);
    for (const d of deps) {
      const from = at.get(d)!;
      edges.push({ id: `${d}>${junction.id}`, from: d, to: junction.id, kind: "merge", path: curve(...bottom(from), jx, jy) });
    }
    edges.push({ id: `${junction.id}>${t.task_id}`, from: junction.id, to: t.task_id, kind: "dependency", path: `M ${jx} ${jy} L ${jx} ${ty}` });
  }

  for (const t of ordered) {
    const old = t.replaces ? at.get(t.replaces) : undefined;
    if (!old) continue;
    const node = at.get(t.task_id)!;
    const path =
      old.layer === node.layer
        ? `M ${old.x + old.w} ${old.y + old.h / 2} C ${old.x + old.w + GAP} ${old.y + old.h / 2}, ${node.x - GAP} ${node.y + node.h / 2}, ${node.x} ${node.y + node.h / 2}`
        : curve(...bottom(old), ...topOf(node));
    edges.push({ id: `${t.replaces}~>${t.task_id}`, from: t.replaces!, to: t.task_id, kind: "replacement", path });
  }

  const height = Math.max(...nodes.map((n) => n.y + n.h)) + 4;
  return { width, height, compact, nodes, edges, junctions, drawnDependencies: reduced };
}
