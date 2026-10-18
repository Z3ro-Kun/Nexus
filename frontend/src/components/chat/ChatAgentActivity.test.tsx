import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ChatAgentActivity from "./ChatAgentActivity";
import * as observerModule from "../../hooks/useRunObserver";

describe("ChatAgentActivity", () => {
  it("renders active agent, task title, metrics, and isometric operator", () => {
    vi.spyOn(observerModule, "useRunObserver").mockReturnValue({
      state: {
        run_id: "test-run",
        goal: "Find streaming anime",
        constraints: [],
        status: "created",
        tasks: {
          t1: {
            task_id: "t1",
            title: "Fetch streaming anime",
            agent_type: "researcher",
            status: "running",
            dependencies: [],
            evidence: [],
            result_metadata: {},
            created_at: "2026-10-18T12:00:00Z",
          },
          t2: {
            task_id: "t2",
            title: "Verify objective",
            agent_type: "verifier",
            status: "pending",
            dependencies: ["t1"],
            evidence: [],
            result_metadata: {},
            created_at: "2026-10-18T12:00:00Z",
          },
        },
        facts: {
          f1: {
            fact_id: "f1",
            content: "Black Clover streaming on Crunchyroll",
            sequence: 1,
            recorded_at: "2026-10-18T12:01:00Z",
          },
        },
        artifacts: {},
        tool_calls: {
          tc1: {
            tool_call_id: "tc1",
            tool_name: "web_search",
            arguments: { query: "Fall 2026 anime" },
            status: "requested",
            error: null,
            error_type: null,
            metadata: {},
            agent_id: "researcher",
            task_id: "t1",
            sequence: 1,
          },
        },
        conflicts: {},
        verifications: {},
        approvals: {},
        recovery: { replan_count: 0, replan_attempts: 0, history: [] },
        completion_summary: null,
        failure_reason: null,
        last_sequence: 5,
        updated_at: "2026-10-18T12:01:00Z",
      },
      result: null,
      events: [
        {
          id: "e1",
          run_id: "test-run",
          sequence: 1,
          event_type: "ToolCalled",
          timestamp: "2026-10-18T12:00:10Z",
          agent_id: "researcher",
          task_id: "t1",
          payload: {
            tool_name: "web_search",
            arguments: { query: "Fall 2026 anime" },
          },
        },
      ],
      loadError: null,
      notFound: false,
      pollError: null,
      polling: true,
      lastUpdated: Date.now(),
    });

    const { container } = render(
      <ChatAgentActivity
        runId="test-run"
        isRunning={true}
        startedAt="2026-10-18T12:00:00Z"
      />
    );

    // Operator exists
    const worker = container.querySelector(".worker");
    expect(worker).toBeTruthy();
    expect(worker?.getAttribute("data-state")).toBe("working");

    // Agent role is researcher
    expect(screen.getByText("Researcher")).toBeTruthy();

    // Tool call query appears in status
    expect(screen.getByText(/Executing web_search/i)).toBeTruthy();

    // Metrics are rendered
    expect(screen.getByText("0 / 2")).toBeTruthy(); // 0 completed out of 2
    expect(screen.getByText("1")).toBeTruthy(); // 1 fact
  });

  it("switches to verifier mode when verification is running", () => {
    vi.spyOn(observerModule, "useRunObserver").mockReturnValue({
      state: {
        run_id: "test-run-verify",
        goal: "Find streaming anime",
        constraints: [],
        status: "created",
        tasks: {
          t1: {
            task_id: "t1",
            title: "Fetch streaming anime",
            agent_type: "researcher",
            status: "completed",
            dependencies: [],
            evidence: [],
            result_metadata: {},
            created_at: "2026-10-18T12:00:00Z",
          },
        },
        facts: {},
        artifacts: {},
        tool_calls: {},
        conflicts: {},
        verifications: {
          v1: {
            verification_id: "v1",
            checkpoint_id: "cp1",
            attempt: 1,
            spec: { objective: "Find anime", semantic: true },
            dependencies: [],
            status: "running",
            covered_task_ids: ["t1"],
            fact_ids: [],
            artifact_ids: [],
            checks: [],
            semantic: null,
            failed_references: [],
            reason: null,
            replaced_by: null,
          },
        },
        approvals: {},
        recovery: { replan_count: 0, replan_attempts: 0, history: [] },
        completion_summary: null,
        failure_reason: null,
        last_sequence: 5,
        updated_at: "2026-10-18T12:01:00Z",
      },
      result: null,
      events: [],
      loadError: null,
      notFound: false,
      pollError: null,
      polling: true,
      lastUpdated: Date.now(),
    });

    const { container } = render(
      <ChatAgentActivity
        runId="test-run-verify"
        isRunning={true}
        startedAt="2026-10-18T12:00:00Z"
      />
    );

    const worker = container.querySelector(".worker");
    expect(worker?.getAttribute("data-state")).toBe("verifying");
    expect(screen.getByText("Verifier")).toBeTruthy();
    expect(screen.getByText(/Verifying facts, claims/i)).toBeTruthy();
  });
});
