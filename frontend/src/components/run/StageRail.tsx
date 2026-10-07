import type { StageState, StageView } from "../../lib/runView";
import { Icon } from "../ui";

const NODE: Record<StageState, string> = {
  upcoming: "border-line-strong bg-canvas text-faint",
  active: "border-accent bg-accent text-accent-ink",
  complete: "border-ok bg-ok text-surface",
  paused: "border-warn bg-canvas text-warn",
  failed: "border-bad bg-bad text-surface",
};

const STATE_TEXT: Record<StageState, string> = {
  upcoming: "not started",
  active: "in progress",
  complete: "complete",
  paused: "paused",
  failed: "failed",
};

/** Plan → Execute → Verify → Done, each stage in the state the backend reports. */
export default function StageRail({ stages }: { stages: StageView[] }) {
  return (
    <ol aria-label="Progress" className="grid grid-cols-4">
      {stages.map((stage, i) => {
        const reached = stage.state !== "upcoming";
        const next = stages[i + 1];
        return (
          <li key={stage.id} data-stage={stage.id} data-state={stage.state} className="relative min-w-0">
            {next && (
              <span
                aria-hidden="true"
                className={`absolute top-[11px] right-0 left-7 h-px transition-colors duration-500 ${next.state !== "upcoming" ? "bg-ok" : "bg-line-strong"}`}
              />
            )}
            <span
              aria-hidden="true"
              className={`relative grid size-6 place-items-center rounded-full border-[1.5px] transition-colors duration-500 ${NODE[stage.state]}`}
            >
              {stage.state === "complete" ? (
                <Icon name="check" className="size-3.5" />
              ) : stage.state === "failed" ? (
                <Icon name="cross" className="size-3.5" />
              ) : stage.state === "paused" ? (
                <Icon name="alert" className="size-3.5" />
              ) : stage.state === "active" ? (
                <span className="size-2 animate-live rounded-full bg-current" />
              ) : null}
            </span>
            <p className={`mt-2.5 pr-2 text-sm font-medium ${reached ? "text-ink" : "text-faint"}`}>
              {stage.label}
              <span className="sr-only">: {STATE_TEXT[stage.state]}</span>
            </p>
            <p className="pr-2 text-xs text-faint">
              {[stage.note, stage.at && `at ${stage.at}`].filter(Boolean).join(", ") || " "}
            </p>
          </li>
        );
      })}
    </ol>
  );
}
