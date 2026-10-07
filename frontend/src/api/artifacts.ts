/**
 * Generated artifacts (backend app/api/v1/artifacts.py, prefix /api/v1):
 *   listArtifacts     GET /runs/{id}/artifacts                         -> ArtifactView[]
 *   downloadArtifact  GET /runs/{id}/artifacts/{artifact_id}/download  -> the stored bytes
 *
 * The bytes always come from the backend, which re-checks them against their SHA-256 on
 * every download and serves only artifacts it marked ready. Nothing is rebuilt or zipped in
 * the browser.
 */

import type { ArtifactView } from "../types/api";
import { API_BASE_URL, ApiError, errorFromResponse, isRecord, request } from "./client";

const TYPES = ["file", "project", "archive"];
const STATUSES = ["created", "validated", "rejected", "ready"];

function isArtifact(value: unknown): value is ArtifactView {
  return (
    isRecord(value) &&
    typeof value.artifact_id === "string" &&
    typeof value.name === "string" &&
    TYPES.includes(value.artifact_type as string) &&
    STATUSES.includes(value.status as string) &&
    typeof value.deliverable === "boolean" &&
    Array.isArray(value.files)
  );
}

const isArtifactList = (value: unknown): value is ArtifactView[] => Array.isArray(value) && value.every(isArtifact);

export function listArtifacts(runId: string, signal?: AbortSignal): Promise<ArtifactView[]> {
  return request(`/runs/${encodeURIComponent(runId)}/artifacts`, isArtifactList, { signal });
}

export function artifactDownloadPath(runId: string, artifactId: string): string {
  return `${API_BASE_URL}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}/download`;
}

export interface DownloadedArtifact {
  blob: Blob;
  /** From Content-Disposition when readable, else the artifact's name. */
  filename: string;
}

/** Fetch a ready artifact's bytes. Errors (not ready, missing, corrupted, offline) become ApiError. */
export async function fetchArtifact(runId: string, artifact: ArtifactView): Promise<DownloadedArtifact> {
  let response: Response;
  try {
    response = await fetch(artifactDownloadPath(runId, artifact.artifact_id));
  } catch {
    throw new ApiError("network", "The NEXUS backend could not be reached.");
  }
  if (!response.ok) {
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      body = undefined;
    }
    throw errorFromResponse(response.status, body);
  }
  return { blob: await response.blob(), filename: filenameFrom(response.headers.get("Content-Disposition")) ?? artifact.name };
}

/** Save a ready artifact through the browser's normal download. */
export async function downloadArtifact(runId: string, artifact: ArtifactView): Promise<void> {
  const { blob, filename } = await fetchArtifact(runId, artifact);
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    // Give the browser a moment to start the download before releasing the bytes.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export function filenameFrom(disposition: string | null): string | null {
  if (!disposition) return null;
  const star = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(disposition);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      // fall through to the plain form
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1].trim() : null;
}
