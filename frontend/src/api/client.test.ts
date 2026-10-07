import { describe, expect, it, vi } from "vitest";

import { describeError } from "../lib/errors";
import { RUN, json, stubFetch } from "../test/fetchMock";
import { ApiError } from "./client";
import { createRun, executeRun } from "./runs";

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error("expected the request to fail");
}

describe("API client", () => {
  it("posts the objective to /api/v1/runs and returns the run", async () => {
    const calls = stubFetch({ "POST /api/v1/runs": () => json(RUN, 201) });

    await expect(createRun({ goal: "g" })).resolves.toEqual(RUN);
    expect(calls).toEqual([{ method: "POST", path: "/api/v1/runs", body: { goal: "g" } }]);
  });

  it("reads NEXUS error bodies", async () => {
    stubFetch({
      [`POST /api/v1/runs/${RUN.id}/execute`]: () =>
        json({ error: { code: "plan_rejected", message: "schema: tasks: Field required" } }, 422),
    });

    const error = await failure(executeRun(RUN.id));
    expect([error.kind, error.status, error.code, error.message]).toEqual(["http", 422, "plan_rejected", "schema: tasks: Field required"]);
    expect(describeError(error)).toBe("The request was rejected: schema: tasks: Field required");
  });

  it("reads FastAPI validation errors", async () => {
    stubFetch({ "POST /api/v1/runs": () => json({ detail: [{ loc: ["body", "goal"], msg: "String should have at least 1 character" }] }, 422) });

    const error = await failure(createRun({ goal: "" }));
    expect([error.code, error.message]).toEqual(["validation_error", "String should have at least 1 character"]);
  });

  it("reports an unreachable backend as a network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = await failure(createRun({ goal: "g" }));
    expect(error.kind).toBe("network");
    expect(describeError(error)).toMatch(/Can't reach the NEXUS backend/);
  });

  it.each([
    ["non-JSON body", () => new Response("<html>proxy error</html>", { status: 200 })],
    ["unexpected shape", () => json({ id: 42 }, 201)],
  ])("rejects a malformed response (%s)", async (_, handler) => {
    stubFetch({ "POST /api/v1/runs": handler });

    const error = await failure(createRun({ goal: "g" }));
    expect(error.kind).toBe("malformed");
    expect(describeError(error)).not.toMatch(/at |Error:/); // no stack traces or raw errors
  });

  it("explains a 5xx without exposing internals", async () => {
    stubFetch({ "POST /api/v1/runs": () => new Response("Internal Server Error", { status: 500 }) });

    const error = await failure(createRun({ goal: "g" }));
    expect([error.kind, error.status]).toEqual(["http", 500]);
    expect(describeError(error)).toBe("The backend reported an error: Request failed (HTTP 500).");
  });
});
