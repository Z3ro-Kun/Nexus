/**
 * The ids of runs opened in this browser, newest first. The backend has no endpoint that
 * lists runs, so this is only an index: everything shown about a run is fetched from the
 * backend (GET /runs/{id}). Storage can be unavailable (private mode, blocked site data);
 * then the list is simply empty.
 */

const KEY = "nexus.recent-runs";
const MAX = 12;

export function recentRunIds(): string[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string").slice(0, MAX) : [];
  } catch {
    return [];
  }
}

function save(ids: string[]) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(ids.slice(0, MAX)));
  } catch {
    // Storage unavailable: history is a convenience only.
  }
}

export function rememberRun(runId: string) {
  save([runId, ...recentRunIds().filter((id) => id !== runId)]);
}

export function forgetRun(runId: string) {
  save(recentRunIds().filter((id) => id !== runId));
}
