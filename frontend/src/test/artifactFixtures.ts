import type { ArtifactView } from "../types/api";

export const SHA = "a3f1".repeat(16);
export const ZIP_SHA = "9c0e".repeat(16);

export function artifact(extra: Partial<ArtifactView> & Pick<ArtifactView, "artifact_id" | "name" | "artifact_type">): ArtifactView {
  return {
    status: "ready",
    deliverable: false,
    verified: false,
    media_type: null,
    size: null,
    sha256: null,
    files: [],
    archive_id: null,
    source_artifact_id: null,
    problems: [],
    task_id: "build",
    agent_id: "specialist",
    tool_call_ids: ["build.t1"],
    input_fact_ids: [],
    download_url: null,
    ...extra,
  };
}

export const BUBBLE_SORT = artifact({
  artifact_id: "build.w1",
  name: "bubble_sort.py",
  artifact_type: "file",
  status: "ready",
  deliverable: true,
  verified: true,
  media_type: "text/x-python",
  size: 1843,
  sha256: SHA,
  files: [{ path: "bubble_sort.py", media_type: "text/x-python", size: 1843, sha256: SHA }],
  download_url: "/api/v1/runs/x/artifacts/build.w1/download",
});

const PROJECT_FILES = [
  { path: "src/main.py", size: 120 },
  { path: "src/utils.py", size: 80 },
  { path: "tests/test_main.py", size: 96 },
  { path: "README.md", size: 64 },
  { path: "requirements.txt", size: 10 },
].map((f) => ({ ...f, media_type: "text/plain", sha256: SHA }));

export const MY_PROJECT = artifact({
  artifact_id: "gen.w1",
  name: "my-project",
  artifact_type: "project",
  status: "validated",
  verified: true,
  task_id: "gen",
  files: PROJECT_FILES,
  archive_id: "gen.w1.zip",
});

export const MY_PROJECT_ZIP = artifact({
  artifact_id: "gen.w1.zip",
  name: "my-project.zip",
  artifact_type: "archive",
  status: "ready",
  deliverable: true,
  verified: true,
  media_type: "application/zip",
  size: 3100,
  sha256: ZIP_SHA,
  task_id: "gen",
  source_artifact_id: "gen.w1",
  download_url: "/api/v1/runs/x/artifacts/gen.w1.zip/download",
});

/** The run state's `workspace_artifacts` entries for a listing (only id and status are read). */
export const inState = (list: ArtifactView[]) => Object.fromEntries(list.map((a) => [a.artifact_id, { artifact_id: a.artifact_id, status: a.status }]));
