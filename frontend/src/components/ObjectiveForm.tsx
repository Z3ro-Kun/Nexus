import { useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from "react";

import { uploadDocument } from "../api/documents";
import type { DocumentRead } from "../types/api";
import { buttonClass, Icon } from "./ui";

/** Backend limit for RunCreate.goal. */
export const MAX_GOAL_LENGTH = 10_000;

export const EXAMPLE_OBJECTIVE =
  "Fetch https://example.com and https://example.org, report the HTML page title of each, and state whether the two titles are identical.";

interface Props {
  busy: boolean;
  onSubmit: (goal: string, documentIds?: string[]) => void;
}

export default function ObjectiveForm({ busy, onSubmit }: Props) {
  const [goal, setGoal] = useState("");
  const [attachedDocs, setAttachedDocs] = useState<DocumentRead[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const input = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const trimmed = goal.trim();
  const canSubmit = trimmed.length > 0 && !busy && !uploading;

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (canSubmit) {
      onSubmit(
        trimmed,
        attachedDocs.map((d) => d.id),
      );
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) submit();
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const files = event.target.files;
    if (!files || files.length === 0) return;

    setUploadError(null);
    setUploading(true);

    try {
      for (const file of Array.from(files)) {
        const doc = await uploadDocument(file);
        setAttachedDocs((prev) => [...prev, doc]);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to upload document.";
      setUploadError(msg);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function removeDocument(id: string) {
    setAttachedDocs((prev) => prev.filter((d) => d.id !== id));
  }

  return (
    <form onSubmit={submit}>
      <label htmlFor="objective" className="sr-only">
        Objective
      </label>
      <div className="rounded-lg border border-line-strong bg-surface transition-colors focus-within:border-accent focus-within:ring-3 focus-within:ring-accent/20">
        <textarea
          id="objective"
          ref={input}
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          maxLength={MAX_GOAL_LENGTH}
          rows={4}
          aria-describedby="objective-hint"
          name="objective"
          autoComplete="off"
          placeholder="Describe the outcome you want, with any sources or constraints…"
          className="block min-h-28 w-full resize-y rounded-t-lg bg-transparent px-4 pt-4 pb-2 text-base leading-relaxed text-ink placeholder:text-faint focus:outline-none disabled:opacity-60"
        />

        {attachedDocs.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 px-4 py-2 border-t border-line/40 bg-surface/50">
            {attachedDocs.map((doc) => (
              <span
                key={doc.id}
                className="inline-flex items-center gap-1.5 rounded-md bg-accent/10 px-2.5 py-1 text-xs font-medium text-accent border border-accent/20"
              >
                <span className="truncate max-w-[220px]" title={doc.filename}>
                  {doc.filename}
                </span>
                <span className="text-faint text-[10px]">
                  ({Math.round(doc.size_bytes / 1024) || 1} KB)
                </span>
                <button
                  type="button"
                  onClick={() => removeDocument(doc.id)}
                  className="hover:text-bad focus:outline-none ml-0.5"
                  title="Remove document"
                >
                  <Icon name="cross" className="size-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        {uploadError && (
          <p className="px-4 py-1.5 text-xs text-bad bg-bad/5 border-t border-bad/20">
            {uploadError}
          </p>
        )}

        <div className="flex flex-wrap items-center justify-between gap-3 px-3 pt-1 pb-3 sm:px-4">
          <div className="flex items-center gap-2">
            <input
              type="file"
              ref={fileInputRef}
              onChange={handleFileChange}
              disabled={busy || uploading}
              multiple
              accept=".pdf,.txt,.md,.markdown,.json,.csv,.py,.ts,.js,.html,.css"
              className="hidden"
              id="document-upload"
            />
            <label
              htmlFor="document-upload"
              className={`${buttonClass.quiet} cursor-pointer text-xs`}
              title="Attach documents (PDF, TXT, MD, CSV, JSON)"
            >
              <Icon name="plus" className="size-3.5" />
              <span>{uploading ? "Uploading…" : "Attach document"}</span>
            </label>
            <p id="objective-hint" className="text-xs text-faint ml-1">
              {goal.length > MAX_GOAL_LENGTH * 0.9 ? (
                `${goal.length.toLocaleString()} of ${MAX_GOAL_LENGTH.toLocaleString()} characters`
              ) : (
                <>
                  <kbd className="font-sans">Ctrl</kbd> + <kbd className="font-sans">Enter</kbd> to start
                </>
              )}
            </p>
          </div>

          <div className="flex items-center gap-2">
            {goal.length === 0 && !busy && (
              <button
                type="button"
                className={buttonClass.quiet}
                onClick={() => {
                  setGoal(EXAMPLE_OBJECTIVE);
                  input.current?.focus();
                }}
              >
                Use an example
              </button>
            )}
            <button type="submit" disabled={!canSubmit} className={buttonClass.primary}>
              {busy ? "Starting run…" : "Start run"}
            </button>
          </div>
        </div>
      </div>
    </form>
  );
}
