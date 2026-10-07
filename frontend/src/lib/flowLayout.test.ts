import { describe, expect, it } from "vitest";

import { GRAPH_TASKS, task } from "../test/runFixtures";
import { layoutGraph, reducedDependencies } from "./flowLayout";

describe("flowLayout", () => {
  it("draws only dependencies not implied by another one (reachability unchanged)", () => {
    expect(reducedDependencies(GRAPH_TASKS())["verify.objective"]).toEqual(["compare"]); // depends on all three; via compare
    expect(reducedDependencies(GRAPH_TASKS()).compare).toEqual(["fetch_com", "fetch_org"]);
  });

  it("puts independent tasks in one row and merges multiple inputs at a junction", () => {
    const layout = layoutGraph(GRAPH_TASKS(), true, 800);
    const y = Object.fromEntries(layout.nodes.map((n) => [n.id, n.y]));
    expect(y.fetch_com).toBe(y.fetch_org);
    expect(y.compare).toBeGreaterThan(y.fetch_com);
    expect(layout.junctions.map((j) => [j.into, j.from])).toEqual([["compare", ["fetch_com", "fetch_org"]]]);
    expect(layout.edges.filter((e) => e.kind === "plan").map((e) => e.to)).toEqual(["fetch_com", "fetch_org"]);
  });

  it("never invents edges: a sequential plan has no junction and no side-by-side tasks", () => {
    const tasks = { a: task("a", "completed"), b: task("b", "running", { dependencies: ["a"] }), c: task("c", "pending", { dependencies: ["b"] }) };
    const layout = layoutGraph(tasks, false, 800);
    expect(layout.junctions).toEqual([]);
    expect(new Set(layout.nodes.map((n) => n.y)).size).toBe(3);
    expect(layout.edges.map((e) => `${e.from}>${e.to}`)).toEqual(["a>b", "b>c"]);
  });

  it("links a replacement to the task it replaces and stays within a narrow width", () => {
    const tasks = { a: task("a", "failed", { replaced_by: "a2" }), a2: task("a2", "running", { replaces: "a" }), b: task("b", "completed") };
    const layout = layoutGraph(tasks, true, 358);
    expect(layout.compact).toBe(true);
    expect(layout.edges.find((e) => e.kind === "replacement")).toMatchObject({ from: "a", to: "a2" });
    expect(layout.width).toBe(358); // three side by side still fit at 390px
  });
});
