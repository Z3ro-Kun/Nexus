import { useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { buttonClass } from "./ui";

/** Backend limit for RunCreate.goal. */
export const MAX_GOAL_LENGTH = 10_000;

export const EXAMPLE_OBJECTIVE =
  "Fetch https://example.com and https://example.org, report the HTML page title of each, and state whether the two titles are identical.";

interface Props {
  busy: boolean;
  onSubmit: (goal: string) => void;
}

export default function ObjectiveForm({ busy, onSubmit }: Props) {
  const [goal, setGoal] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const trimmed = goal.trim();
  const canSubmit = trimmed.length > 0 && !busy;

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (canSubmit) onSubmit(trimmed);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) submit();
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
        <div className="flex flex-wrap items-center justify-between gap-3 px-3 pt-1 pb-3 sm:px-4">
          <p id="objective-hint" className="text-xs text-faint">
            {goal.length > MAX_GOAL_LENGTH * 0.9 ? (
              `${goal.length.toLocaleString()} of ${MAX_GOAL_LENGTH.toLocaleString()} characters`
            ) : (
              <>
                <kbd className="font-sans">Ctrl</kbd> + <kbd className="font-sans">Enter</kbd> to start
              </>
            )}
          </p>
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
