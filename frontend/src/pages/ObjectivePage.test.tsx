import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import App from "../App";
import { EXAMPLE_OBJECTIVE } from "../components/ObjectiveForm";
import { recentRunIds, rememberRun } from "../lib/history";
import { GOAL, RUN, RUN_ID, deferred, json, stubFetch, type Call, type Handler } from "../test/fetchMock";
import { GRAPH_TASKS, event, result, state } from "../test/runFixtures";

const HEALTH = { "GET /api/v1/health": () => json({ status: "ok", service: "nexus", version: "0.1.0", environment: "test" }) };
const EXECUTE = `POST /api/v1/runs/${RUN_ID}/execute`;
const RUN_READS: Record<string, Handler> = {
  [`GET /api/v1/runs/${RUN_ID}/state`]: () => json(state(6, { tasks: GRAPH_TASKS() })),
  [`GET /api/v1/runs/${RUN_ID}/result`]: () => json(result("executing")),
  [`GET /api/v1/runs/${RUN_ID}/events`]: () => json([event(1, "RunCreated", { goal: GOAL })]),
};

function setup(routes: Record<string, Handler>) {
  const calls = stubFetch({ ...HEALTH, ...routes });
  const user = userEvent.setup();
  render(<App />);
  return { calls, user };
}

async function submit(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Objective"), GOAL);
  await user.click(screen.getByRole("button", { name: "Start run" }));
}

const posts = (calls: Call[], path: string) => calls.filter((c) => c.method === "POST" && c.path === path);

describe("Objective page", () => {
  it("renders the shell and the objective input", async () => {
    setup({});

    expect(screen.getByText("NEXUS")).toBeInTheDocument();
    expect(screen.getByLabelText("Objective")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start run" })).toBeDisabled(); // empty objective
    expect(await screen.findByText("Connected")).toBeInTheDocument();
  });

  it("creates the run, issues execute once and opens /runs/:runId", async () => {
    const { calls, user } = setup({
      "POST /api/v1/runs": () => json(RUN, 201),
      [EXECUTE]: () => new Promise(() => undefined), // long-running /execute: still in flight
      ...RUN_READS,
    });

    await submit(user);

    await waitFor(() => expect(window.location.pathname).toBe(`/runs/${RUN_ID}`));
    expect(posts(calls, "/api/v1/runs")).toEqual([{ method: "POST", path: "/api/v1/runs", body: { goal: GOAL } }]);
    expect(posts(calls, `/api/v1/runs/${RUN_ID}/execute`)).toHaveLength(1);
    expect(await screen.findByRole("list", { name: "Plan steps" })).toBeInTheDocument(); // run page loaded from the backend
  });

  it("shows a loading state while the run is being created", async () => {
    const create = deferred<Response>();
    const { user } = setup({ "POST /api/v1/runs": () => create.promise });

    await submit(user);

    expect(screen.getByRole("button", { name: "Starting run…" })).toBeDisabled();
    expect(screen.getByLabelText("Objective")).toBeDisabled();
    create.resolve(json({ error: { code: "x", message: "stop" } }, 400));
    await screen.findByRole("alert");
  });

  it("does not submit twice while a request is in flight", async () => {
    const create = deferred<Response>();
    const { calls, user } = setup({ "POST /api/v1/runs": () => create.promise });

    await submit(user);
    await user.click(screen.getByRole("button", { name: "Starting run…" }));
    await user.keyboard("{Enter}");

    expect(posts(calls, "/api/v1/runs")).toHaveLength(1);
    create.resolve(json({ error: { code: "x", message: "stop" } }, 400));
    await screen.findByRole("alert");
  });

  it("fills the objective with an example on request", async () => {
    const { user } = setup({});

    await user.click(screen.getByRole("button", { name: "Use an example" }));

    expect(screen.getByLabelText("Objective")).toHaveValue(EXAMPLE_OBJECTIVE);
    expect(screen.getByRole("button", { name: "Start run" })).toBeEnabled();
  });

  it("starts the run with Ctrl+Enter", async () => {
    const { calls, user } = setup({ "POST /api/v1/runs": () => json(RUN, 201), [EXECUTE]: () => new Promise(() => undefined), ...RUN_READS });

    await user.type(screen.getByLabelText("Objective"), GOAL);
    await user.keyboard("{Control>}{Enter}{/Control}");

    await waitFor(() => expect(window.location.pathname).toBe(`/runs/${RUN_ID}`));
    expect(posts(calls, "/api/v1/runs")).toHaveLength(1);
  });

  it("lists recent runs from this browser by objective and status, loaded from the backend", async () => {
    const OTHER = "11111111-2222-3333-4444-555555555555";
    const GONE = "99999999-2222-3333-4444-555555555555";
    rememberRun(GONE);
    rememberRun(OTHER);
    rememberRun(RUN_ID);
    setup({
      [`GET /api/v1/runs/${RUN_ID}`]: () => json({ ...RUN, status: "completed", updated_at: new Date().toISOString() }),
      [`GET /api/v1/runs/${OTHER}`]: () => json({ ...RUN, id: OTHER, goal: "Compare two prices.", status: "failed", updated_at: "2026-10-01T10:00:00Z" }),
      [`GET /api/v1/runs/${GONE}`]: () => json({ error: { code: "run_not_found", message: "gone" } }, 404),
    });

    const section = await screen.findByRole("region", { name: "Recent runs" });
    const rows = within(section).getAllByRole("link");
    expect(rows.map((r) => r.getAttribute("href"))).toEqual([`/runs/${RUN_ID}`, `/runs/${OTHER}`]); // newest first
    expect(rows[0]).toHaveTextContent(GOAL);
    expect(rows[0]).toHaveTextContent("Completed");
    expect(rows[1]).toHaveTextContent("Failed");
    expect(recentRunIds()).not.toContain(GONE); // the backend no longer knows it
  });

  it("records a created run in the recent-runs history", async () => {
    const { user } = setup({ "POST /api/v1/runs": () => json(RUN, 201), [EXECUTE]: () => new Promise(() => undefined), ...RUN_READS });

    await submit(user);

    await waitFor(() => expect(recentRunIds()).toEqual([RUN_ID]));
  });

  it("surfaces an unreachable backend when creating the run", async () => {
    const { calls, user } = setup({ "POST /api/v1/runs": () => Promise.reject(new TypeError("Failed to fetch")) });

    await submit(user);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not create the run.");
    expect(alert).toHaveTextContent("Can't reach the NEXUS backend");
    expect(window.location.pathname).toBe("/");
    expect(calls.some((c) => c.path.endsWith("/execute"))).toBe(false);
  });
});
