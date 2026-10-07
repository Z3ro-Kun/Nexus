import { vi } from "vitest";

/**
 * Test-only fetch stub: routes "METHOD /path" to a handler and records every call. A route
 * without a query string also matches requests with one (the handler gets the full URL).
 */
export type Handler = (init: RequestInit | undefined, url: string) => Response | Promise<Response>;

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export interface Call {
  method: string;
  path: string;
  body: unknown;
}

export function stubFetch(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const handler = routes[`${method} ${path}`] ?? routes[`${method} ${path.split("?")[0]}`];
    if (!handler) return json({ error: { code: "not_stubbed", message: `${method} ${path}` } }, 500);
    return handler(init, path);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

/** A promise you resolve later, to hold a request "in flight". */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

export const RUN_ID = "2e1740b4-2159-4765-bf56-2b22d55c9293";
export const GOAL = "Fetch https://example.com and report its title.";

export const RUN = {
  id: RUN_ID,
  goal: GOAL,
  status: "created",
  last_sequence: 1,
  created_at: "2026-10-04T11:27:37Z",
  updated_at: "2026-10-04T11:27:37Z",
};
