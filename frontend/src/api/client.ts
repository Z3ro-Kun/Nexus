/**
 * HTTP client for the NEXUS REST API. Every failure becomes an ApiError with a kind the
 * UI can explain; components never see raw fetch errors or response bodies.
 */

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/+$/, "");

export type ApiErrorKind =
  | "network" // the backend could not be reached
  | "http" // the backend answered with an error status
  | "malformed"; // a 2xx response that is not the expected JSON shape

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  /** NEXUS error code (e.g. "run_not_found"), or "validation_error" for FastAPI 422s. */
  readonly code: string | null;

  constructor(kind: ApiErrorKind, message: string, options: { status?: number | null; code?: string | null } = {}) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = options.status ?? null;
    this.code = options.code ?? null;
  }
}

type Guard<T> = (value: unknown) => value is T;

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
}

export async function request<T>(path: string, guard: Guard<T>, options: RequestOptions = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: options.method ?? "GET",
      headers: options.body === undefined ? undefined : { "Content-Type": "application/json" },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("network", "The NEXUS backend could not be reached.");
  }

  const body = await readJson(response);
  if (!response.ok) throw errorFromResponse(response.status, body);
  if (body === undefined || !guard(body)) {
    throw new ApiError("malformed", "The backend returned an unexpected response.", { status: response.status });
  }
  return body;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

export function errorFromResponse(status: number, body: unknown): ApiError {
  // NEXUS errors: {"error": {"code": "...", "message": "..."}}
  if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") {
    const code = typeof body.error.code === "string" ? body.error.code : null;
    return new ApiError("http", body.error.message, { status, code });
  }
  // FastAPI request validation: {"detail": [{"loc": [...], "msg": "..."}]} or {"detail": "..."}
  if (isRecord(body) && "detail" in body) {
    const detail = body.detail;
    const message = Array.isArray(detail)
      ? detail.map((d) => (isRecord(d) && typeof d.msg === "string" ? d.msg : null)).filter(Boolean).join("; ")
      : typeof detail === "string"
        ? detail
        : "";
    return new ApiError("http", message || `Request failed (HTTP ${status}).`, {
      status,
      code: status === 422 ? "validation_error" : null,
    });
  }
  return new ApiError("http", `Request failed (HTTP ${status}).`, { status });
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
