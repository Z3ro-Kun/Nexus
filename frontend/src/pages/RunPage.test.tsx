import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App from "../App";
import { IDLE_POLL_INTERVAL_MS, POLL_INTERVAL_MS } from "../hooks/useRunObserver";
import { startExecution } from "../lib/executions";
import { GOAL, RUN_ID, json, stubFetch, type Call, type Handler } from "../test/fetchMock";
import { GRAPH_TASKS, event, result, scriptedRun, state, task, verification } from "../test/runFixtures";
import { BUBBLE_SORT, MY_PROJECT, MY_PROJECT_ZIP, inState } from "../test/artifactFixtures";
import { captureDownloads as captureFiles } from "../test/downloads";
import type { ArtifactView, ConflictState, Fact } from "../types/api";

const HEALTH = { "GET /api/v1/health": () => json({ status: "ok", service: "nexus", version: "0.1.0", environment: "test" }) };
const EXECUTE = `POST /api/v1/runs/${RUN_ID}/execute`;

const E1 = [
  event(1, "RunCreated", { goal: GOAL }),
  event(2, "TaskCreated", { task_id: "fetch_com", agent_type: "researcher", dependencies: [] }, { agent_id: "planner" }),
  event(3, "TaskStarted", { task_id: "fetch_com" }, { task_id: "fetch_com" }),
];
const E2 = [
  ...E1,
  event(4, "ToolCalled", { tool_call_id: "fetch_com.t1", tool_name: "http_fetch", arguments: { url: "https://example.com" } }, { agent_id: "researcher", task_id: "fetch_com" }),
  event(5, "ToolSucceeded", { tool_call_id: "fetch_com.t1", result: { status_code: 200, content_type: "text/html" }, metadata: {} }, { agent_id: "researcher", task_id: "fetch_com" }),
];
const DONE_EVENTS = [
  ...E2,
  event(6, "VerificationPassed", { verification_id: "verify.objective", checks: [{ passed: true }], semantic: { passed: true, model: "openai/gpt-oss-120b" } }),
  event(7, "RunCompleted", { summary: "All 4 tasks completed; verified by verify.objective." }),
];

const FACT: Fact = {
  fact_id: "fetch_com.f1",
  content: "The HTML title of https://example.com is 'Example Domain'.",
  source: "https://example.com",
  agent_id: "researcher",
  task_id: "fetch_com",
  provenance: { kind: "tool_output", tool_name: "http_fetch", tool_call_id: "fetch_com.t1", source: "https://example.com", fake: false },
  claim: { subject: "https://example.com", attribute: "title", value: "Example Domain", unit: null },
  sequence: 6,
};

const CALL = {
  tool_call_id: "fetch_com.t1",
  tool_name: "http_fetch",
  arguments: { url: "https://example.com" },
  status: "succeeded" as const,
  error: null,
  error_type: null,
  metadata: {},
  agent_id: "researcher",
  task_id: "fetch_com",
  sequence: 4,
};

const PASSED = verification("passed", {
  covered_task_ids: ["compare", "fetch_com", "fetch_org"],
  checks: [
    { check_id: "tasks_completed", kind: "tasks_completed", passed: true, message: "all 3 covered tasks completed", references: [] },
    { check_id: "provenance", kind: "provenance", passed: true, message: "2 facts with valid provenance (2 tool-derived)", references: [] },
  ],
  semantic: {
    provider: "groq",
    model: "openai/gpt-oss-120b",
    passed: true,
    objective: { criterion: "objective", passed: true, evidence: [{ kind: "fact", id: "fetch_com.f1" }], explanation: "Both titles were fetched and compared." },
    constraints: [],
    summary: "The objective is satisfied.",
  },
});

const DONE_STATE = state(7, {
  status: "completed",
  tasks: GRAPH_TASKS({ fetch_com: "completed", fetch_org: "completed", compare: "completed", "verify.objective": "completed" }),
  facts: { [FACT.fact_id]: FACT },
  tool_calls: { [CALL.tool_call_id]: CALL },
  verifications: { "verify.objective": PASSED },
  completion_summary: "All 4 tasks completed; verified by verify.objective.",
});

const DONE_RESULT = result("completed", {
  verified: true,
  completion_summary: "All 4 tasks completed; verified by verify.objective.",
  deliverables: [
    {
      task_id: "compare",
      title: "Compare page titles",
      agent_type: "analyst",
      summary: "Both titles are 'Example Domain': identical.",
      artifacts: [{ artifact_id: "compare.a1", name: "titles.json", media_type: "application/json", content: '{"https://example.com":"Example Domain"}', truncated: false }],
    },
  ],
  tool_calls: { total: 1, succeeded: 1, failed: 0, by_tool: { http_fetch: 1 } },
  last_sequence: 7,
});

const RUNNING_STATE = state(5, {
  tasks: GRAPH_TASKS(),
  tool_calls: { [CALL.tool_call_id]: { ...CALL, status: "requested" } },
});

const EXECUTING = { state: state(3, { tasks: GRAPH_TASKS() }), result: result("executing"), events: E1 };
const TOOLING = { state: RUNNING_STATE, result: result("executing"), events: E2 };
const DONE = { state: DONE_STATE, result: DONE_RESULT, events: DONE_EVENTS };

function open(routes: Record<string, Handler>, path = `/runs/${RUN_ID}`) {
  window.history.replaceState(null, "", path);
  const calls = stubFetch({ ...HEALTH, ...routes });
  render(<App />);
  return calls;
}

const eventSequences = () => within(screen.getByRole("list", { name: "Events" })).getAllByRole("listitem").map((li) => li.dataset.sequence);
const reads = (calls: Call[], suffix: string) => calls.filter((c) => c.method === "GET" && c.path.includes(suffix));
const region = (name: string) => screen.findByRole("region", { name });
const runStatus = () => screen.getByRole("status", { name: "Run status" });
const stage = (id: string) => document.querySelector(`[data-stage="${id}"]`);

describe("Run page", () => {
  it("reconstructs a run from the backend on (re-)entry to /runs/:runId, without executing it", async () => {
    const calls = open(scriptedRun([EXECUTING]).routes);

    expect(await screen.findByRole("heading", { level: 1, name: GOAL })).toBeInTheDocument();
    expect(runStatus()).toHaveTextContent("Working");
    expect(screen.getByText("Updating live")).toBeInTheDocument();
    expect(stage("plan")).toHaveAttribute("data-state", "complete");
    expect(stage("execute")).toHaveAttribute("data-state", "active");
    expect(stage("verify")).toHaveAttribute("data-state", "upcoming");
    expect(calls.some((c) => c.path.endsWith("/execute"))).toBe(false); // a refresh never re-executes
  });

  it("wraps a long unbroken URL in the objective instead of widening the page", async () => {
    const goal = "Compare sources: https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1&explaintext=1&format=json&titles=Python_(programming_language)";
    open(scriptedRun([{ state: state(1, { goal }), result: result("created"), events: [event(1, "RunCreated", { goal })] }]).routes);

    // jsdom has no layout: assert the wrapping rule the heading relies on (overflow-wrap: break-word).
    expect(await screen.findByRole("heading", { level: 1, name: goal })).toHaveClass("break-words");
  });

  it("lays out the plan from real dependencies: parallel tasks side by side, merging through shared state", async () => {
    open(scriptedRun([EXECUTING]).routes);

    const steps = await screen.findByRole("list", { name: "Plan steps" });
    const node = (label: string) => within(steps).getByLabelText(label);
    const top = (label: string) => parseFloat(node(label).style.top);
    expect(within(steps).getByLabelText("Planner: planned 4 tasks")).toBeInTheDocument();
    expect(node("Researcher: Title of fetch_com")).toHaveAttribute("data-status", "running");
    expect(node("Researcher: Title of fetch_org")).toHaveAttribute("data-status", "running");
    // Independent tasks share a row; the analyst sits below both; the verifier below it.
    expect(top("Researcher: Title of fetch_com")).toBe(top("Researcher: Title of fetch_org"));
    expect(top("Analyst: Title of compare")).toBeGreaterThan(top("Researcher: Title of fetch_com"));
    expect(top("Verifier: Title of verify.objective")).toBeGreaterThan(top("Analyst: Title of compare"));
    expect(screen.getAllByText("Shared state").length).toBeGreaterThan(0); // where the analyst's two inputs meet
    expect(node("Analyst: Title of compare")).toHaveTextContent("After Title of fetch_com, Title of fetch_org.");
    // Internal ids live in the collapsed Execution details only.
    expect(screen.getByLabelText("Task fetch_com")).not.toBeVisible();
  });

  it("shows one worker per task the backend reports as running, and says when they run in parallel", async () => {
    open(scriptedRun([TOOLING]).routes);

    const now = await region("Now");
    expect(within(now).getByText("2 agents working in parallel")).toBeInTheDocument();
    expect(within(now).getByRole("list", { name: "Running now" }).querySelectorAll(".worker")).toHaveLength(2);
    expect(within(now).getByText("Fetching example.com")).toBeInTheDocument();
    expect(within(now).getByText(/Waits for “Title of fetch_com” and “Title of fetch_org”/)).toBeInTheDocument();
    expect(screen.getByText("2 tasks running in parallel")).toBeInTheDocument();
  });

  it("renders the parallel fixture: A and B running, C completed, the analyst waiting on all three", async () => {
    const tasks = {
      research_a: task("research_a", "running", { title: "Research A" }),
      research_b: task("research_b", "running", { title: "Research B" }),
      research_c: task("research_c", "completed", { title: "Research C" }),
      compare: task("compare", "pending", { title: "Compare", agent_type: "analyst", dependencies: ["research_a", "research_b", "research_c"] }),
    };
    const facts = { "research_c.f1": { ...FACT, fact_id: "research_c.f1", task_id: "research_c" } };
    open(scriptedRun([{ state: state(13, { tasks, facts }), result: result("executing"), events: E1 }]).routes);

    const now = await region("Now");
    expect(within(now).getByRole("list", { name: "Running now" }).querySelectorAll(".worker")).toHaveLength(2); // never C, never a fixed count
    expect(within(now).getByText("2 agents working in parallel")).toBeInTheDocument();
    const steps = screen.getByRole("list", { name: "Plan steps" });
    const top = (label: string) => parseFloat(within(steps).getByLabelText(label).style.top);
    expect(new Set([top("Researcher: Research A"), top("Researcher: Research B"), top("Researcher: Research C")]).size).toBe(1);
    expect(within(steps).getByLabelText("Researcher: Research C")).toHaveAttribute("data-status", "completed");
    expect(within(steps).getByLabelText("Analyst: Compare")).toHaveAttribute("data-status", "pending");
    expect(screen.getByText("Shared state · 1 finding")).toBeInTheDocument(); // C's result is already in shared state
    // Signals travel only on real, active connections: one beam per running agent into shared
    // state, and pulses on the two plan edges into A and B (none into C or the analyst).
    const pulses = (el: HTMLElement) => el.querySelectorAll(".comet:not(.comet-glow)").length;
    expect(pulses(now)).toBe(2);
    expect(within(now).getByRole("list", { name: "Already in shared state" })).toHaveTextContent("Researcher Research C"); // finished, not a worker
    expect(pulses(screen.getByRole("region", { name: "Plan" }))).toBe(4); // two pulses per flowing edge
  });

  it("says 1 agent working, not parallel, when only one task runs", async () => {
    const tasks = GRAPH_TASKS({ fetch_com: "completed", fetch_org: "completed", compare: "running" });
    open(scriptedRun([{ state: state(9, { tasks }), result: result("executing"), events: E1 }]).routes);

    const now = await region("Now");
    expect(within(now).getByText("1 agent working")).toBeInTheDocument();
    expect(screen.queryByText(/in parallel/)).not.toBeInTheDocument();
    expect(within(now).getByRole("list", { name: "Running now" }).querySelectorAll(".worker")).toHaveLength(1);
  });

  it("shows the planner at work while /execute is planning", async () => {
    const run = scriptedRun([{ state: state(1), result: result("created"), events: [event(1, "RunCreated", { goal: GOAL })] }]);
    open({ ...run.routes, [EXECUTE]: () => new Promise(() => undefined) });
    startExecution(RUN_ID);

    const now = await region("Now");
    expect(within(now).getByText("Breaking the objective into tasks")).toBeInTheDocument();
    expect(now.querySelector('.worker[data-state="planning"]')).not.toBeNull();
  });

  it("calls the moment between the last task and RunCompleted Finishing, not Stalled", async () => {
    const tasks = GRAPH_TASKS({ fetch_com: "completed", fetch_org: "completed", compare: "completed", "verify.objective": "completed" });
    open(scriptedRun([{ state: state(28, { tasks, verifications: { "verify.objective": PASSED } }), result: result("blocked"), events: E1 }]).routes);

    await screen.findByRole("heading", { level: 1, name: GOAL });
    expect(runStatus()).toHaveTextContent("Finishing");
    expect(screen.queryByText("NEXUS can't make further progress on this run.")).not.toBeInTheDocument();
  });

  it("lets the viewer pause live motion", async () => {
    open(scriptedRun([TOOLING]).routes);
    const now = await region("Now");
    expect(document.querySelectorAll(".comet").length).toBeGreaterThan(0);

    await userEvent.setup().click(within(now).getByRole("button", { name: "Pause motion" }));

    expect(document.documentElement).toHaveClass("motion-paused");
    expect(document.querySelectorAll(".comet")).toHaveLength(0);
    expect(within(now).getByRole("button", { name: "Resume motion" })).toHaveAttribute("aria-pressed", "true");
  });

  it("draws no moving signals with reduced motion", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"), addEventListener: () => undefined, removeEventListener: () => undefined }));
    open(scriptedRun([TOOLING]).routes);

    await region("Now");
    expect(document.querySelectorAll(".comet")).toHaveLength(0);
    expect(within(await region("Now")).getByText("Shared state")).toBeInTheDocument(); // still readable, just still
  });

  it("polls incrementally with after_sequence, appends new events once and stops at a terminal state", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const run = scriptedRun([EXECUTING, TOOLING, DONE]);
    open(run.routes);
    await screen.findByRole("list", { name: "Plan steps" });

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + 10);
    await waitFor(() => expect(eventSequences()).toEqual(["1", "2", "3", "4", "5"]));
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + 10);
    await waitFor(() => expect(eventSequences()).toEqual(["1", "2", "3", "4", "5", "6", "7"]));

    expect(run.cursors).toEqual([0, 3, 5]); // never the whole history again
    await vi.advanceTimersByTimeAsync(IDLE_POLL_INTERVAL_MS * 3);
    expect(run.polls()).toBe(3); // terminal: polling stopped
    expect(runStatus()).toHaveTextContent("Completed");
    expect(screen.queryByText("Updating live")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Now" })).not.toBeInTheDocument(); // a finished run settles
  });

  it("keeps watching a run that is not executing here, at the slower idle rate", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const run = scriptedRun([{ state: state(1), result: result("created"), events: [event(1, "RunCreated", { goal: GOAL })] }, EXECUTING]);
    open(run.routes);
    await screen.findByRole("heading", { level: 1, name: GOAL });
    expect(runStatus()).toHaveTextContent("Not started");

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + 10);
    expect(run.polls()).toBe(1); // not yet: idle runs are polled less often
    await vi.advanceTimersByTimeAsync(IDLE_POLL_INTERVAL_MS);
    await waitFor(() => expect(runStatus()).toHaveTextContent("Working")); // planned and started elsewhere
  });

  it("never duplicates an event even if the backend repeats one", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const run = scriptedRun([EXECUTING, TOOLING]);
    run.routes[`GET /api/v1/runs/${RUN_ID}/events`] = (_, url) => {
      const after = Number(new URL(url, "http://x").searchParams.get("after_sequence"));
      return json(after === 0 ? E1 : E2.slice(2)); // overlaps: re-sends sequence 3
    };
    open(run.routes);
    await screen.findByRole("list", { name: "Plan steps" });

    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS + 10);
    await waitFor(() => expect(eventSequences()).toEqual(["1", "2", "3", "4", "5"]));
  });

  it("makes the answer the focal point of a completed run, then why to trust it and the evidence", async () => {
    open(scriptedRun([DONE]).routes);

    const answer = await region("Answer");
    expect(within(answer).getByText("Both titles are 'Example Domain': identical.")).toBeInTheDocument();
    expect(within(answer).getByRole("rowheader", { name: "https://example.com" })).toBeInTheDocument(); // flat JSON artifact as a table
    expect(within(answer).getByText(/From “Compare page titles”, analyst/)).toBeInTheDocument();
    expect(within(runStatus().parentElement!).getByText("Verified")).toBeInTheDocument(); // trust signal next to the status

    expect(document.querySelectorAll(".comet, .worker")).toHaveLength(0); // a finished run is still
    const order = ["Answer", "Verification", "Evidence", "How NEXUS did it"].map((name) => screen.getByRole("region", { name }));
    for (let i = 1; i < order.length; i++) expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("renders a passed verification in plain language from the backend's verification state", async () => {
    open(scriptedRun([DONE]).routes);

    const panel = await region("Verification");
    expect(within(panel).getByText("Verified")).toBeInTheDocument();
    expect(within(panel).getByText("All planned work finished")).toBeInTheDocument();
    expect(within(panel).getByText("Every finding traces to a recorded source")).toBeInTheDocument();
    expect(within(panel).getByText("An independent review confirms the objective is met")).toBeInTheDocument();
    expect(within(panel).getByText("Both titles were fetched and compared.")).toBeInTheDocument();
    expect(within(panel).getByText("Reviewed by groq / openai/gpt-oss-120b")).toBeInTheDocument();
    expect(within(panel).queryByText("tasks_completed")).not.toBeInTheDocument(); // ids stay in the details
  });

  it("shows tool evidence with its source, the claim and the recorded HTTP status", async () => {
    open(scriptedRun([DONE]).routes);

    const evidence = await region("Evidence");
    expect(within(evidence).getByRole("link", { name: /example\.com/ })).toHaveAttribute("href", "https://example.com");
    expect(within(evidence).getByText("Example Domain")).toBeInTheDocument();
    expect(within(evidence).getByText(/Web fetch, HTTP 200/)).toBeInTheDocument(); // from the ToolSucceeded event
    expect(within(evidence).getByText("fetch_com.t1")).not.toBeVisible(); // provenance is one click away
  });

  it("renders a failed verification, and does not call a completed-but-unverified run verified", async () => {
    const failed = verification("failed", {
      reason: "1 of 1 checks failed",
      checks: [{ check_id: "provenance", kind: "provenance", passed: false, message: "fact x has no valid provenance", references: [] }],
    });
    open(scriptedRun([{ state: state(9, { status: "completed", verifications: { "verify.objective": failed } }), result: result("completed", { verified: false }), events: E1 }]).routes);

    const panel = await region("Verification");
    expect(within(panel).getByText("Verification failed")).toBeInTheDocument();
    expect(within(panel).queryByText("Verified")).not.toBeInTheDocument();
    expect(within(panel).getByText("Some findings lack a valid source")).toBeInTheDocument();
    expect(within(panel).getByText("fact x has no valid provenance")).not.toBeVisible(); // raw message under Technical details
    expect(runStatus()).toHaveTextContent("Completed without verification");
  });

  it("explains a failed run in plain language, and shows the recovery that actually happened", async () => {
    const failedState = state(12, {
      status: "failed",
      failure_reason: "replan budget exhausted (1 of 1 replans used); task 'fetch_com_retry' failed (TOOL_FAILURE: unavailable)",
      tasks: {
        fetch_com: task("fetch_com", "failed", { error: "tool call fetch_com.t1 (http_fetch) failed [unavailable]", replaced_by: "fetch_com_retry", failure: { failure_type: "TOOL_FAILURE", error_type: "unavailable", tool_call_id: null } }),
        fetch_com_retry: task("fetch_com_retry", "failed", { replaces: "fetch_com", error: "boom", failure: { failure_type: "TOOL_FAILURE", error_type: "unavailable", tool_call_id: null } }),
      },
      recovery: {
        replan_count: 1,
        replan_attempts: 1,
        history: [
          { replan_number: 1, outcome: "accepted", failed_task_id: "fetch_com", failure_type: "TOOL_FAILURE", summary: "Use another source.", new_task_ids: ["fetch_com_retry"], replacement_task_id: "fetch_com_retry", sequence: 8 },
        ],
      },
    });
    open(scriptedRun([{ state: failedState, result: result("failed", { failure_reason: failedState.failure_reason }), events: [...E1, event(9, "RunFailed", { reason: "replan budget exhausted" })] }]).routes);

    const wrong = await region("What went wrong");
    expect(within(wrong).getByText("“Title of fetch_com_retry” failed: a tool call failed.")).toBeInTheDocument();
    expect(within(wrong).getByText("NEXUS changed its plan once before stopping.")).toBeInTheDocument();
    expect(within(wrong).getByText(/replan budget exhausted/)).not.toBeVisible(); // backend wording kept, collapsed
    expect(runStatus()).toHaveTextContent("Failed");
    expect(stage("execute")).toHaveAttribute("data-state", "failed");

    const replan = screen.getByLabelText("Replan 1");
    expect(replan).toHaveTextContent("“Title of fetch_com” failed");
    expect(replan).toHaveTextContent("NEXUS changed the plan");
    expect(replan).toHaveTextContent("Use another source.");
    expect(replan).toHaveTextContent("New approach: “Title of fetch_com_retry”");
    expect(replan).toHaveTextContent("It also failed.");
  });

  it("shows no recovery section when nothing was replanned", async () => {
    open(scriptedRun([DONE]).routes);
    await region("Answer");
    expect(screen.queryByRole("region", { name: "Recovery" })).not.toBeInTheDocument();
  });

  it("names a resolved conflict's accepted value only when the backend chose one", async () => {
    const fact = (id: string, value: number, source: string): Fact => ({
      ...FACT,
      fact_id: id,
      content: `Price is ${value}`,
      provenance: { ...FACT.provenance!, source },
      claim: { subject: "product x", attribute: "price", value, unit: "INR" },
    });
    const conflict = (status: ConflictState["status"], resolved: string | null): ConflictState => ({
      conflict_id: "c1",
      description: null,
      fact_ids: ["a.f1", "b.f1"],
      status,
      resolution: status === "unresolved" ? "no independent tool-derived evidence" : null,
      conflict_type: "value_mismatch",
      fact_key: { subject: "product x", attribute: "price" },
      resolution_task_id: "resolve_c1",
      resolved_fact_id: resolved,
      evidence_ids: resolved ? ["r.f1"] : [],
    });
    const facts = { "a.f1": fact("a.f1", 94999, "https://a.test/x"), "b.f1": fact("b.f1", 99999, "https://b.test/x"), "r.f1": fact("r.f1", 94999, "https://c.test/x") };

    open(scriptedRun([{ state: state(9, { facts, conflicts: { c1: conflict("resolved", "r.f1") } }), result: result("blocked"), events: E1 }]).routes);
    const resolved = await region("Conflicting information");
    expect(within(resolved).getByText("99999 INR")).toBeInTheDocument();
    expect(within(resolved).getByText(/Resolved:/)).toHaveTextContent("Resolved: 94999 INR, backed by c.test/x");
  });

  it("says a conflict remains unresolved instead of picking a side", async () => {
    const facts = {
      "a.f1": { ...FACT, fact_id: "a.f1", claim: { subject: "product x", attribute: "price", value: 1, unit: null } },
      "b.f1": { ...FACT, fact_id: "b.f1", claim: { subject: "product x", attribute: "price", value: 2, unit: null } },
    };
    const conflict: ConflictState = {
      conflict_id: "c1", description: null, fact_ids: ["a.f1", "b.f1"], status: "unresolved", resolution: "no independent evidence",
      conflict_type: "value_mismatch", fact_key: { subject: "product x", attribute: "price" }, resolution_task_id: "r", resolved_fact_id: null, evidence_ids: [],
    };
    open(scriptedRun([{ state: state(9, { facts, conflicts: { c1: conflict } }), result: result("blocked"), events: E1 }]).routes);

    const section = await region("Conflicting information");
    expect(within(section).getByText(/Conflict remains unresolved/)).toBeInTheDocument();
    expect(within(section).queryByText(/Resolved:/)).not.toBeInTheDocument();
  });

  it("keeps execution details collapsed by default and expands them on request", async () => {
    open(scriptedRun([DONE]).routes);
    await region("Answer");

    const summary = screen.getByText("Execution details");
    expect(screen.getByRole("region", { name: "Task graph" })).not.toBeVisible();
    await userEvent.setup().click(summary);

    expect(screen.getByRole("region", { name: "Task graph" })).toBeVisible();
    expect(within(screen.getByLabelText("Task compare")).getByText("after: fetch_com, fetch_org")).toBeVisible();
    expect(within(screen.getByLabelText("Activity fetch_com")).getByText("http_fetch")).toBeVisible();
    expect(within(screen.getByRole("region", { name: "Verification record" })).getByText("tasks_completed")).toBeVisible();
    expect(screen.getByRole("list", { name: "Events" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Shared state" })).toBeVisible();
  });

  it("renders unknown event types safely", async () => {
    const events = [...E1, event(4, "SomethingNew", { anything: { nested: [1, 2] } }), event(5, "Odd", {})];
    open(scriptedRun([{ ...EXECUTING, result: result("blocked"), events }]).routes);

    await screen.findByRole("list", { name: "Plan steps" });
    expect(eventSequences()).toEqual(["1", "2", "3", "4", "5"]);
    expect(screen.getByText("SomethingNew")).toBeInTheDocument();
    expect(screen.getByText('{"anything":{"nested":[1,2]}}')).toBeInTheDocument();
  });

  it("explains a stalled run in plain language, with the backend's blockers collapsed", async () => {
    const tasks = { buy: task("buy", "failed", { title: "Order the laptop", failure: { failure_type: "POLICY_FAILURE", error_type: "denied", tool_call_id: null } }) };
    open(scriptedRun([{ state: state(9, { tasks }), result: result("blocked", { completion_blockers: ["action buy was denied and not replaced"] }), events: E1 }]).routes);

    expect(await screen.findByText("NEXUS can't make further progress on this run.")).toBeInTheDocument();
    expect(screen.getByText("“Order the laptop” failed: a safety policy blocked an action.")).toBeInTheDocument();
    expect(screen.getByText("action buy was denied and not replaced")).not.toBeVisible();
    expect(runStatus()).toHaveTextContent("Stalled");
  });

  it("shows a missing run as not found", async () => {
    open({ [`GET /api/v1/runs/${RUN_ID}/state`]: () => json({ error: { code: "run_not_found", message: `run ${RUN_ID} not found` } }, 404) });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Run not found.");
    expect(alert).toHaveTextContent("Check the link");
  });

  it("keeps the retry behaviour: a failed /execute can be retried from the run page, once at a time", async () => {
    let attempts = 0;
    const run = scriptedRun([{ state: state(1), result: result("created"), events: [event(1, "RunCreated", { goal: GOAL })] }]);
    const calls = open({
      ...run.routes,
      [EXECUTE]: () => (++attempts === 1 ? json({ error: { code: "llm_not_configured", message: "no LLM provider is configured" } }, 503) : new Promise(() => undefined)),
    });
    startExecution(RUN_ID);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The execution request failed.");
    expect(alert).toHaveTextContent("no LLM provider is configured");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Retry execution" }));
    expect(startExecution(RUN_ID)).toBe(false); // already in flight: not issued twice
    await waitFor(() => expect(calls.filter((c) => c.method === "POST" && c.path.endsWith("/execute"))).toHaveLength(2));
    expect(reads(calls, "/events").length).toBeGreaterThan(0);
    expect(runStatus()).toHaveTextContent("Planning"); // /execute in flight before any task exists
  });

  it("remembers an opened run for the recent-runs list", async () => {
    open(scriptedRun([DONE]).routes);
    await region("Answer");
    expect(JSON.parse(window.localStorage.getItem("nexus.recent-runs") ?? "[]")).toEqual([RUN_ID]);
  });
});

describe("Run page: needs clarification", () => {
  const CLARIFY = {
    reason: "underspecified" as const,
    question: "What specific assistance or information are you looking for regarding having two children?",
    missing: ["desired outcome", "type of help required"],
  };
  const CLARIFY_EVENTS = [event(1, "RunCreated", { goal: GOAL }), event(2, "ClarificationRequested", CLARIFY, { agent_id: "planner" })];
  const CLARIFYING = {
    state: state(2, { status: "needs_clarification", clarification: CLARIFY }),
    result: result("needs_clarification", { clarification: CLARIFY }),
    events: CLARIFY_EVENTS,
  };

  it("shows the planner's question, what is missing and why, as its own terminal state", async () => {
    open(scriptedRun([CLARIFYING]).routes);

    const section = await region("Needs clarification");
    expect(runStatus()).toHaveTextContent("Needs clarification");
    expect(within(section).getByText(CLARIFY.question)).toBeInTheDocument();
    const missing = within(section).getByRole("list", { name: "Missing" });
    expect(within(missing).getAllByRole("listitem").map((li) => li.textContent)).toEqual(CLARIFY.missing);
    expect(within(section).getByText("The objective doesn't say what work you want done.")).toBeInTheDocument();
    expect(within(section).getByText(/didn't plan or run anything/)).toBeInTheDocument();
  });

  it("does not look like failure, success, progress, verification or recovery, and offers no fake answer box", async () => {
    open(scriptedRun([CLARIFYING]).routes);
    await region("Needs clarification");

    expect(runStatus()).not.toHaveTextContent(/Failed|Completed|Working|Stalled|Waiting/);
    expect(screen.queryByText("Verified")).not.toBeInTheDocument();
    for (const name of ["Now", "Plan", "Answer", "Verification", "Evidence", "Recovery", "What went wrong"]) {
      expect(screen.queryByRole("region", { name })).not.toBeInTheDocument();
    }
    expect(document.querySelectorAll("[data-stage], .worker, .comet")).toHaveLength(0); // no progress rail, agents or flow
    expect(screen.queryByRole("button", { name: /Execute|Resume|Retry/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument(); // answering is not supported yet
    expect(screen.getByRole("link", { name: /Run another objective/ })).toBeInTheDocument();
  });

  it("is terminal: polling stops after the first load", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const run = scriptedRun([CLARIFYING]);
    open(run.routes);
    await region("Needs clarification");

    await vi.advanceTimersByTimeAsync(IDLE_POLL_INTERVAL_MS * 3);
    expect(run.polls()).toBe(1);
    expect(screen.queryByText("Updating live")).not.toBeInTheDocument();
  });

  it("falls back to the clarification in the run state when the result has none", async () => {
    open(scriptedRun([{ ...CLARIFYING, result: result("needs_clarification") }]).routes);
    expect(within(await region("Needs clarification")).getByText(CLARIFY.question)).toBeInTheDocument();
  });

  it("labels the run in the recent runs list", async () => {
    window.localStorage.setItem("nexus.recent-runs", JSON.stringify([RUN_ID]));
    open(
      { [`GET /api/v1/runs/${RUN_ID}`]: () => json({ id: RUN_ID, goal: GOAL, status: "needs_clarification", last_sequence: 2, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:01Z" }) },
      "/",
    );
    expect(await screen.findByText("Needs clarification")).toBeInTheDocument();
  });
});

describe("Run page: generated artifacts", () => {
  const LIST = `GET /api/v1/runs/${RUN_ID}/artifacts`;
  const DOWNLOAD = (id: string) => `GET /api/v1/runs/${RUN_ID}/artifacts/${id}/download`;

  function withArtifacts(list: ArtifactView[], tick = DONE, extra: Record<string, Handler> = {}) {
    const run = scriptedRun([{ ...tick, state: { ...tick.state, workspace_artifacts: inState(list) } }]);
    return open({ ...run.routes, [LIST]: () => json(list), ...extra });
  }

  const captureDownloads = () => {
    const { saved } = captureFiles();
    return { names: () => saved.map((s) => s.split(" ")[0]) };
  };

  it("renders nothing, and asks for nothing, when the run generated no files", async () => {
    const calls = open(scriptedRun([DONE]).routes);
    await region("Answer");
    expect(screen.queryByRole("region", { name: /Generated artifact/ })).not.toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/artifacts"))).toBe(false);
  });

  it("shows a ready single file with its size, verification and a real Download", async () => {
    const saved = captureDownloads();
    const calls = withArtifacts([BUBBLE_SORT], DONE, {
      [DOWNLOAD("build.w1")]: () => new Response("def bubble_sort(xs):\n    return xs\n", { headers: { "Content-Disposition": 'attachment; filename="bubble_sort.py"' } }),
    });

    const section = await region("Generated artifact");
    expect(within(section).getByRole("heading", { name: "bubble_sort.py" })).toBeInTheDocument();
    expect(within(section).getByText("Python file · 1.8 KB")).toBeInTheDocument();
    expect(within(section).getByText("Artifact verified")).toBeInTheDocument();
    expect(within(section).queryByText(/tested|executed successfully|works/i)).not.toBeInTheDocument(); // never claims the code ran

    await userEvent.setup().click(within(section).getByRole("button", { name: "Download bubble_sort.py" }));
    await waitFor(() => expect(saved.names()).toEqual(["bubble_sort.py"]));
    expect(calls.map((c) => c.path)).toContain(`/api/v1/runs/${RUN_ID}/artifacts/build.w1/download`);
  });

  it("previews the start of a ready text file from the downloaded bytes", async () => {
    withArtifacts([BUBBLE_SORT], DONE, { [DOWNLOAD("build.w1")]: () => new Response("def bubble_sort(xs):\n    return sorted(xs)\n") });
    const section = await region("Generated artifact");
    await userEvent.setup().click(within(section).getByRole("button", { name: "Preview" }));
    expect(await within(section).findByText(/def bubble_sort\(xs\):/)).toBeInTheDocument();
    expect(within(section).getByText("Download for the exact file.")).toBeInTheDocument();
  });

  it("shows a project with its files and downloads the backend's ZIP", async () => {
    const saved = captureDownloads();
    const calls = withArtifacts([MY_PROJECT, MY_PROJECT_ZIP], DONE, {
      [DOWNLOAD("gen.w1.zip")]: () => new Response(new Uint8Array([80, 75, 3, 4]), { headers: { "Content-Disposition": 'attachment; filename="my-project.zip"' } }),
    });

    const section = await region("Generated artifact"); // one deliverable: the project and its ZIP
    expect(within(section).getAllByRole("heading", { level: 3 })).toHaveLength(1);
    expect(within(section).getByRole("heading", { name: "my-project" })).toBeInTheDocument();
    expect(within(section).getByText(/5 files · Python project · 370 B/)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(within(section).getByRole("button", { name: "View files" }));
    const files = within(section).getByRole("list", { name: "Files in my-project" });
    expect(within(files).getAllByRole("listitem").map((li) => li.firstChild?.textContent)).toEqual([
      "README.md",
      "requirements.txt",
      "src/main.py",
      "src/utils.py",
      "tests/test_main.py",
    ]);

    await user.click(within(section).getByRole("button", { name: "Download ZIP my-project.zip" }));
    await waitFor(() => expect(saved.names()).toEqual(["my-project.zip"]));
    expect(calls.map((c) => c.path)).toContain(`/api/v1/runs/${RUN_ID}/artifacts/gen.w1.zip/download`);
  });

  it("offers no download while the artifact is not ready", async () => {
    const pending = { ...BUBBLE_SORT, status: "validated" as const, deliverable: false };
    withArtifacts([pending], TOOLING);
    const section = await region("Generated artifact");
    expect(within(section).getByText("Ready for download once the run is verified")).toBeInTheDocument();
    expect(within(section).queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
    expect(within(section).queryByText("Artifact verified")).not.toBeInTheDocument();
  });

  it("shows a rejected artifact's reason and never offers it", async () => {
    const rejected = {
      ...MY_PROJECT_ZIP,
      status: "rejected" as const,
      deliverable: false,
      problems: ["delivery check: my-project.zip does not match its recorded checksum"],
    };
    withArtifacts([MY_PROJECT, rejected], { ...DONE, state: { ...DONE.state, status: "created" }, result: result("blocked") });
    const section = await region("Generated artifact");
    expect(within(section).getByText("Not delivered")).toBeInTheDocument();
    expect(within(section).getByText(/does not match its recorded checksum/)).toBeInTheDocument();
    expect(within(section).queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
  });

  it("keeps the artifact visible and offers a retry when the download fails", async () => {
    const saved = captureDownloads();
    let attempts = 0;
    withArtifacts([BUBBLE_SORT], DONE, {
      [DOWNLOAD("build.w1")]: () =>
        ++attempts === 1
          ? json({ error: { code: "artifact_corrupted", message: "artifact 'build.w1' does not match its recorded checksum" } }, 500)
          : new Response("ok"),
    });
    const section = await region("Generated artifact");
    const user = userEvent.setup();
    await user.click(within(section).getByRole("button", { name: "Download bubble_sort.py" }));

    expect(await within(section).findByRole("alert")).toHaveTextContent("no longer matches its verified checksum");
    expect(within(section).getByRole("heading", { name: "bubble_sort.py" })).toBeInTheDocument();
    expect(saved.names()).toEqual([]);
    await user.click(within(section).getByRole("button", { name: "Retry download bubble_sort.py" }));
    await waitFor(() => expect(saved.names()).toEqual(["bubble_sort.py"]));
    expect(within(section).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists several deliverables together, after the answer and before verification", async () => {
    withArtifacts([BUBBLE_SORT, MY_PROJECT, MY_PROJECT_ZIP]);
    const section = await region("Generated artifacts");
    expect(within(section).getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["bubble_sort.py", "my-project"]);
    expect(within(section).getByRole("button", { name: "Download bubble_sort.py" })).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Download ZIP my-project.zip" })).toBeInTheDocument();
    const order = ["Answer", "Generated artifacts", "Verification"].map((name) => screen.getByRole("region", { name }));
    for (let i = 1; i < order.length; i++) expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
