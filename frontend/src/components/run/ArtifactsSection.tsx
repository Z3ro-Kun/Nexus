import { useState } from "react";

import { downloadArtifact, fetchArtifact } from "../../api/artifacts";
import { ApiError } from "../../api/client";
import { useArtifacts } from "../../hooks/useArtifacts";
import { isTerminal, plural } from "../../lib/runView";
import type { ArtifactView, RunState } from "../../types/api";
import { Icon, Section, StatusDot, TechnicalDetail, buttonClass } from "../ui";

const PREVIEW_MAX_BYTES = 200_000;
const PREVIEW_LINES = 60;

const FILE_KINDS: Record<string, string> = {
  "text/x-python": "Python file",
  "text/markdown": "Markdown file",
  "application/json": "JSON file",
  "text/csv": "CSV file",
  "text/tab-separated-values": "TSV file",
  "application/toml": "TOML file",
  "application/yaml": "YAML file",
  "text/javascript": "JavaScript file",
  "text/html": "HTML file",
  "text/css": "CSS file",
  "text/x-shellscript": "Shell script",
  "application/sql": "SQL file",
  "application/xml": "XML file",
  "image/svg+xml": "SVG file",
};

export function formatBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1_000_000) return `${(bytes / 1000).toFixed(bytes < 10_000 ? 1 : 0)} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

function projectKind(files: ArtifactView["files"]): string {
  const paths = files.map((f) => f.path.toLowerCase());
  const has = (name: string) => paths.some((p) => p === name || p.endsWith(`/${name}`));
  if (has("package.json")) return "JavaScript project";
  if (has("requirements.txt") || has("pyproject.toml") || paths.filter((p) => p.endsWith(".py")).length * 2 > paths.length) return "Python project";
  return "Project";
}

/** A deliverable as the user thinks of it: a file, or a project with its ZIP. */
interface Item {
  key: string;
  main: ArtifactView;
  download: ArtifactView | null; // what the Download button fetches (the file, or the project's archive)
}

function items(list: ArtifactView[]): Item[] {
  const byId = new Map(list.map((a) => [a.artifact_id, a]));
  return list
    .filter((a) => a.artifact_type !== "archive" || !a.source_artifact_id || !byId.has(a.source_artifact_id))
    .map((a) => ({
      key: a.artifact_id,
      main: a,
      download: a.artifact_type === "project" ? (byId.get(a.archive_id ?? "") ?? null) : a,
    }));
}

/**
 * Files NEXUS generated for this run, with their real download. Shown under the answer;
 * nothing renders when the run generated no files. "Verified" here means NEXUS's artifact
 * checks (paths, exclusions, secrets, checksums) and the run's verification passed; the
 * generated code itself is never executed.
 */
export default function ArtifactsSection({ runId, state }: { runId: string; state: RunState }) {
  const { artifacts, error, expected, retry } = useArtifacts(runId, state);
  const list = items(artifacts);
  if (!expected || (list.length === 0 && !error)) return null;

  return (
    <Section title={list.length === 1 ? "Generated artifact" : "Generated artifacts"} id="artifacts">
      {error && list.length === 0 ? (
        <p role="alert" className="flex flex-wrap items-center gap-x-3 text-sm text-bad">
          {error}
          <button type="button" className={buttonClass.quiet} onClick={retry}>
            Try again
          </button>
        </p>
      ) : (
        <ul className="divide-y divide-line border-y border-line">
          {list.map((item) => (
            <ArtifactRow key={item.key} runId={runId} item={item} terminal={isTerminal(state)} />
          ))}
        </ul>
      )}
    </Section>
  );
}

function downloadError(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case "artifact_missing":
        return "The file is no longer stored on the NEXUS server.";
      case "artifact_corrupted":
        return "The stored file no longer matches its verified checksum, so NEXUS will not deliver it.";
      case "artifact_not_ready":
        return "This artifact is not ready for download.";
      case "artifact_not_found":
      case "run_not_found":
        return "NEXUS no longer has this artifact.";
      case "artifacts_disabled":
        return "Artifact downloads are turned off on this NEXUS server.";
    }
    return e.kind === "network" ? "The NEXUS backend could not be reached." : e.message;
  }
  return "The download failed.";
}

function ArtifactRow({ runId, item, terminal }: { runId: string; item: Item; terminal: boolean }) {
  const { main, download } = item;
  const project = main.artifact_type === "project";
  const ready = download?.deliverable === true;
  const rejected = main.status === "rejected" || download?.status === "rejected";
  const problems = [...main.problems, ...(download && download !== main ? download.problems : [])];
  const size = project ? main.files.reduce((n, f) => n + f.size, 0) : (main.size ?? main.files[0]?.size ?? 0);
  const kind = project ? `${plural(main.files.length, "file")} · ${projectKind(main.files)}` : FILE_KINDS[main.media_type ?? ""] ?? "Text file";
  const label = project ? `Download ZIP` : "Download";
  const headingId = `artifact-${main.artifact_id.replace(/[^A-Za-z0-9_-]/g, "-")}`;

  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [open, setOpen] = useState<"files" | "preview" | null>(null);
  const [preview, setPreview] = useState<{ lines: string[]; total: number } | null>(null);

  const save = async () => {
    if (!download) return;
    setBusy(true);
    setFailure(null);
    try {
      await downloadArtifact(runId, download);
    } catch (e) {
      setFailure(downloadError(e));
    } finally {
      setBusy(false);
    }
  };

  const togglePreview = async () => {
    if (open === "preview") return setOpen(null);
    setOpen("preview");
    if (preview || !download) return;
    try {
      const text = await (await fetchArtifact(runId, download)).blob.text();
      const lines = text.split("\n");
      setPreview({ lines: lines.slice(0, PREVIEW_LINES), total: lines.length });
    } catch (e) {
      setOpen(null);
      setFailure(downloadError(e));
    }
  };

  const canPreview = !project && ready && size <= PREVIEW_MAX_BYTES;

  return (
    <li aria-labelledby={headingId} className="py-4">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h3 id={headingId} className="font-mono text-[15px] font-medium break-all text-ink">
            {main.name}
          </h3>
          <p className="mt-0.5 text-sm text-dim">
            {kind} · {formatBytes(size)}
            {project && download?.size ? <span className="text-faint"> · ZIP {formatBytes(download.size)}</span> : null}
          </p>
          <p className="mt-1.5 flex items-center gap-1.5 text-sm">
            {ready ? (
              <span className="flex items-center gap-1.5 font-medium text-ok">
                <Icon name="check" className="size-3.5" />
                Artifact verified
              </span>
            ) : rejected ? (
              <span className="flex items-center gap-1.5 font-medium text-bad">
                <Icon name="cross" className="size-3.5" />
                Not delivered
              </span>
            ) : (
              <span className="flex items-center gap-2 text-dim">
                <StatusDot tone={terminal ? "neutral" : "warn"} />
                {terminal ? "Not delivered: the run did not finish verification" : "Ready for download once the run is verified"}
              </span>
            )}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {project && (
            <button type="button" className={buttonClass.quiet} aria-expanded={open === "files"} onClick={() => setOpen(open === "files" ? null : "files")}>
              {open === "files" ? "Hide files" : "View files"}
            </button>
          )}
          {canPreview && (
            <button type="button" className={buttonClass.quiet} aria-expanded={open === "preview"} onClick={() => void togglePreview()}>
              {open === "preview" ? "Hide preview" : "Preview"}
            </button>
          )}
          {ready && (
            <button type="button" className={buttonClass.secondary} disabled={busy} onClick={() => void save()} aria-label={busy ? undefined : `${failure ? "Retry download" : label} ${download?.name ?? main.name}`}>
              <Icon name="download" className="size-3.5" />
              {busy ? "Downloading…" : failure ? "Retry download" : label}
            </button>
          )}
        </div>
      </div>

      {failure && (
        <p role="alert" className="mt-2 text-sm text-bad">
          {failure}
        </p>
      )}
      {rejected && problems.length > 0 && <p className="mt-2 max-w-[68ch] text-sm break-words text-dim">{problems[0]}</p>}

      {open === "files" && (
        <ul aria-label={`Files in ${main.name}`} className="mt-3 space-y-1 rounded-md bg-raised px-3 py-2.5 font-mono text-xs">
          {[...main.files]
            .sort((a, b) => a.path.localeCompare(b.path))
            .map((f) => (
              <li key={f.path} className="flex justify-between gap-4">
                <span className="min-w-0 break-all text-ink">{f.path}</span>
                <span className="shrink-0 text-faint">{formatBytes(f.size)}</span>
              </li>
            ))}
        </ul>
      )}

      {open === "preview" && (
        <figure className="mt-3">
          <pre className="max-h-80 overflow-auto rounded-md bg-raised px-3 py-2.5 font-mono text-xs leading-relaxed text-ink">
            {preview ? preview.lines.join("\n") : "Loading preview…"}
          </pre>
          {preview && (
            <figcaption className="mt-1 text-xs text-faint">
              {preview.total > PREVIEW_LINES ? `First ${PREVIEW_LINES} of ${preview.total} lines. ` : ""}Download for the exact file.
            </figcaption>
          )}
        </figure>
      )}

      {ready && (
        <TechnicalDetail label="Integrity">
          <p>
            SHA-256 <span className="break-all">{download?.sha256}</span>
          </p>
          <p>Checked by NEXUS before delivery: safe paths, no excluded or secret files, matching checksum. The code was not run.</p>
        </TechnicalDetail>
      )}
    </li>
  );
}
