import { afterEach, describe, expect, it, vi } from "vitest";

import { BUBBLE_SORT, MY_PROJECT_ZIP } from "../test/artifactFixtures";
import { captureDownloads } from "../test/downloads";
import { RUN_ID, json, stubFetch } from "../test/fetchMock";
import { downloadArtifact, fetchArtifact, filenameFrom, listArtifacts } from "./artifacts";
import { ApiError } from "./client";

const LIST = `GET /api/v1/runs/${RUN_ID}/artifacts`;
const download = (id: string) => `GET /api/v1/runs/${RUN_ID}/artifacts/${id}/download`;

afterEach(() => vi.restoreAllMocks());

describe("artifact API", () => {
  it("lists a run's artifacts from GET /runs/{id}/artifacts", async () => {
    const calls = stubFetch({ [LIST]: () => json([BUBBLE_SORT]) });
    await expect(listArtifacts(RUN_ID)).resolves.toEqual([BUBBLE_SORT]);
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([LIST]);
  });

  it("rejects a listing that is not artifact metadata", async () => {
    stubFetch({ [LIST]: () => json([{ name: "x" }]) });
    await expect(listArtifacts(RUN_ID)).rejects.toMatchObject({ kind: "malformed" });
  });

  it("downloads the stored bytes from the real download endpoint, with the backend's filename", async () => {
    const calls = stubFetch({
      [download("gen.w1.zip")]: () =>
        new Response(new Uint8Array([80, 75, 3, 4]), { headers: { "Content-Type": "application/zip", "Content-Disposition": 'attachment; filename="my-project.zip"' } }),
    });
    const { blob, filename } = await fetchArtifact(RUN_ID, MY_PROJECT_ZIP);
    expect(calls.map((c) => c.path)).toEqual([`/api/v1/runs/${RUN_ID}/artifacts/gen.w1.zip/download`]);
    expect(filename).toBe("my-project.zip");
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(new Uint8Array([80, 75, 3, 4]));
  });

  it("turns NEXUS download errors into ApiErrors with their code", async () => {
    stubFetch({ [download("build.w1")]: () => json({ error: { code: "artifact_corrupted", message: "checksum differs" } }, 500) });
    const error = await fetchArtifact(RUN_ID, BUBBLE_SORT).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ kind: "http", status: 500, code: "artifact_corrupted" });
  });

  it("saves through a temporary object URL and a link named like the file", async () => {
    stubFetch({ [download("build.w1")]: () => new Response("print(1)\n", { headers: { "Content-Disposition": 'attachment; filename="bubble_sort.py"' } }) });
    const { saved, created } = captureDownloads();

    await downloadArtifact(RUN_ID, BUBBLE_SORT);
    expect(created).toHaveBeenCalledOnce();
    expect(saved).toEqual(["bubble_sort.py blob:nexus/1"]);
  });

  it("reads filenames from Content-Disposition", () => {
    expect(filenameFrom('attachment; filename="my-project.zip"')).toBe("my-project.zip");
    expect(filenameFrom("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.md")).toBe("résumé.md");
    expect(filenameFrom(null)).toBeNull();
  });
});
