/** GET /health (backend app/api/v1/health.py). */

import { isRecord, request } from "./client";

export interface Health {
  status: "ok";
  service: string;
  version: string;
  environment: string;
}

function isHealth(value: unknown): value is Health {
  return isRecord(value) && value.status === "ok" && typeof value.service === "string" && typeof value.version === "string";
}

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return request("/health", isHealth, { signal });
}
