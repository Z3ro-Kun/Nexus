import { ApiError } from "../api/client";

/** A user-facing explanation of a failed API call. Never includes stack traces. */
export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. Please try again.";
  switch (error.kind) {
    case "network":
      return "Can't reach the NEXUS backend. Check that the API server is running and try again.";
    case "malformed":
      return "The backend returned a response NEXUS doesn't understand. The frontend and backend versions may not match.";
    case "http":
      if (error.status === 422) return `The request was rejected: ${error.message}`;
      if (error.status === 404) return `Not found: ${error.message}`;
      if (error.status !== null && error.status >= 500) return `The backend reported an error: ${error.message}`;
      return error.message;
  }
}
