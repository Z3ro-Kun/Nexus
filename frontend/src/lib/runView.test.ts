import { describe, expect, it } from "vitest";

import { GRAPH_TASKS, event, result, state, task, verification } from "../test/runFixtures";
import { conflicts, elapsed, runStatus, stages, stoppedBecause, taskView, trust } from "./runView";

const states = (s: ReturnType<typeof stages>) => Object.fromEntries(s.map((x) => [x.id, x.state]));

describe("runView", () => {
  it("reports Verified only from the backend's verified flag", () => {
    const passed = verification("passed");
    const s = state(9, { status: "completed", verifications: { "verify.objective": passed } });
    expect(trust(s, result("completed", { verified: false })).verdict).toBe("unverified");
    expect(trust(s, result("completed", { verified: true })).verdict).toBe("verified");
    expect(runStatus(s, result("completed", { verified: false }), false).label).toBe("Completed without verification");
  });

  it("maps the backend phase onto stages without inventing progress", () => {
    const planning = state(1);
    expect(states(stages(planning, result("created"), [], true))).toEqual({ plan: "active", execute: "upcoming", verify: "upcoming", done: "upcoming" });
    expect(states(stages(planning, result("created"), [], false)).plan).toBe("upcoming"); // nothing is executing
    const s = state(5, { tasks: GRAPH_TASKS() });
    expect(states(stages(s, result("verifying"), [], false))).toEqual({ plan: "complete", execute: "complete", verify: "active", done: "upcoming" });
    expect(states(stages(s, result("waiting_for_approval"), [], false)).execute).toBe("paused");
    const failedVerification = state(9, { status: "failed", tasks: GRAPH_TASKS(), verifications: { "verify.objective": verification("failed") } });
    expect(states(stages(failedVerification, result("failed"), [], false))).toEqual({ plan: "complete", execute: "complete", verify: "failed", done: "upcoming" });
  });

  it("describes waiting tasks by what they wait for, by title", () => {
    const s = state(3, { tasks: GRAPH_TASKS({ fetch_com: "completed" }) });
    expect(taskView(s, s.tasks.compare).detail).toBe("Waits for “Title of fetch_org”");
    expect(taskView(s, s.tasks.compare).role).toBe("Analyst");
  });

  it("names the replacement, not the failed task, as what a dependent waits for", () => {
    const base = GRAPH_TASKS({ fetch_com: "failed", fetch_org: "completed" });
    const tasks = {
      ...base,
      fetch_com: { ...base.fetch_com, replaced_by: "fetch_com2" },
      fetch_com2: task("fetch_com2", "running", { replaces: "fetch_com" }),
    };
    const s = state(9, { tasks });
    expect(taskView(s, s.tasks.compare).detail).toBe("Waits for “Title of fetch_com2”"); // as the scheduler resolves it
  });

  it("explains why a run stopped from failed tasks that nothing replaced", () => {
    const s = state(9, {
      tasks: {
        a: task("a", "failed", { replaced_by: "b", failure: { failure_type: "TOOL_FAILURE", error_type: null, tool_call_id: null } }),
        b: task("b", "failed", { replaces: "a", failure: { failure_type: "TIMEOUT", error_type: null, tool_call_id: null } }),
      },
      recovery: { replan_count: 1, replan_attempts: 1, history: [] },
    });
    expect(stoppedBecause(s)).toEqual(["“Title of b” failed: it ran out of time.", "NEXUS changed its plan once before stopping."]);
  });

  it("never names a conflict winner unless the backend recorded one", () => {
    const base = { conflict_id: "c", description: null, fact_ids: [], resolution: "x", conflict_type: null, fact_key: null, resolution_task_id: null, evidence_ids: [] };
    const s = state(9, {
      conflicts: {
        c: { ...base, status: "resolved", resolved_fact_id: null },
        d: { ...base, conflict_id: "d", status: "unresolved", resolved_fact_id: null },
      },
    });
    expect(conflicts(s).map((c) => c.accepted)).toEqual([null, null]);
  });

  it("counts elapsed time only while the run moves, and hides sub-second values", () => {
    const events = [event(1, "RunCreated"), event(30, "RunCompleted")];
    expect(elapsed(events, false, Date.now())).toBe("29s");
    expect(elapsed([event(1, "RunCreated")], false, Date.now())).toBeNull(); // idle, nothing happened yet
    expect(elapsed([event(1, "RunCreated"), event(1, "TaskCreated")], false, Date.now())).toBeNull();
  });
});

describe("runStatus for each phase", () => {
  const view = (phase: Parameters<typeof result>[0], status: "created" | "completed" | "failed" | "needs_clarification" = "created", executing = false) =>
    runStatus(state(5, { status, tasks: { a: task("a", "running") } }), result(phase, { verified: phase === "completed" }), executing);

  it("keeps the existing labels and tones", () => {
    expect(view("executing")).toEqual({ label: "Working", tone: "active", live: true });
    expect(view("completed", "completed")).toEqual({ label: "Completed", tone: "ok", live: false });
    expect(view("failed", "failed")).toEqual({ label: "Failed", tone: "bad", live: false });
    expect(view("waiting_for_approval")).toEqual({ label: "Waiting for approval", tone: "warn", live: false });
    expect(view("blocked")).toEqual({ label: "Stalled", tone: "warn", live: false });
    expect(view("verifying")).toEqual({ label: "Verifying", tone: "active", live: true });
  });

  it("gives needs_clarification its own, non-live, non-failure status", () => {
    expect(view("needs_clarification", "needs_clarification")).toEqual({ label: "Needs clarification", tone: "ask", live: false });
  });
});
