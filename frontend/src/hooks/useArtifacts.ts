import { useCallback, useEffect, useState } from "react";

import { listArtifacts } from "../api/artifacts";
import { ApiError } from "../api/client";
import type { ArtifactView, RunState } from "../types/api";

/**
 * The run's generated artifacts from GET /runs/{id}/artifacts. Fetched only when the run
 * state says there are some, and again whenever one of them changes status (created ->
 * validated -> ready / rejected), so a run without artifacts costs no request.
 */
export function useArtifacts(runId: string, state: RunState) {
  const signature = Object.values(state.workspace_artifacts ?? {})
    .map((a) => `${a.artifact_id}:${a.status}`)
    .sort()
    .join("|");
  const [artifacts, setArtifacts] = useState<ArtifactView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!signature) {
      setArtifacts([]);
      return;
    }
    const controller = new AbortController();
    listArtifacts(runId, controller.signal)
      .then((list) => {
        setArtifacts(list);
        setError(null);
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof ApiError ? e.message : "Could not load the generated artifacts.");
      });
    return () => controller.abort();
  }, [runId, signature, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { artifacts, error, expected: signature !== "", retry };
}
