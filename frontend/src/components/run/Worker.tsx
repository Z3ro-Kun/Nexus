import { HUE_VAR, type RoleHue } from "../../lib/labels";

export type WorkerState = "working" | "waiting" | "done" | "failed" | "planning" | "verifying";

/**
 * A small isometric operator: a block with a sensor band on a platform, working on a
 * floating unit of work. Pure SVG + CSS (see .worker rules in index.css); every animation
 * reflects a backend state, and all of them stop with reduced motion.
 */
export default function Worker({ hue, state, size = 96, className = "" }: { hue: RoleHue; state: WorkerState; size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 120 120" width={className ? undefined : size} height={className ? undefined : size} className={`worker ${className}`} data-state={state} style={{ color: HUE_VAR[hue] }} aria-hidden="true">
      <polygon points="26,96 60,79 94,96 60,113" fill="currentColor" fillOpacity="0.08" stroke="currentColor" strokeOpacity="0.28" />
      <polygon className="w-ring" points="26,96 60,79 94,96 60,113" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <g className="w-body">
        <polygon points="40,50 60,40 80,50 60,60" fill="currentColor" fillOpacity="0.3" />
        <polygon points="40,50 60,60 60,86 40,76" fill="currentColor" fillOpacity="0.78" />
        <polygon points="60,60 80,50 80,76 60,86" fill="currentColor" fillOpacity="0.56" />
        <polygon points="40,50 60,40 80,50 60,60" fill="none" stroke="currentColor" strokeOpacity="0.9" strokeWidth="1" />
        <polygon className="w-visor" points="40,60 60,70 60,74 40,64" />
        <polygon className="w-visor" points="60,70 80,60 80,64 60,74" />
        <polygon points="55,50 60,47.5 65,50 60,52.5" fill="var(--surface)" fillOpacity="0.85" />
      </g>
      {state === "working" && (
        <>
          <line className="w-beam" x1="70" y1="47" x2="94" y2="30" stroke="currentColor" strokeWidth="1.2" strokeDasharray="2 3" />
          <g className="w-item">
            <polygon points="86,26 97,20.5 108,26 97,31.5" fill="currentColor" fillOpacity="0.45" stroke="currentColor" strokeWidth="1" />
            <polygon points="86,26 97,31.5 97,35 86,29.5" fill="currentColor" fillOpacity="0.7" />
            <polygon points="97,31.5 108,26 108,29.5 97,35" fill="currentColor" fillOpacity="0.55" />
          </g>
        </>
      )}
      {state === "planning" &&
        [0, 1, 2].map((i) => (
          <polygon key={i} className={`w-piece w-piece-${i}`} points="52,24 60,20 68,24 60,28" fill="currentColor" fillOpacity={0.4 + i * 0.2} stroke="currentColor" strokeWidth="0.8" />
        ))}
      {state === "verifying" && (
        <>
          {[0, 1, 2].map((i) => (
            <polygon key={i} className={`w-evidence w-evidence-${i}`} points="55,27 60,24.5 65,27 60,29.5" fill="currentColor" fillOpacity="0.7" />
          ))}
          <path className="w-check" d="M53 25 l5 4 l9 -9" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
        </>
      )}
    </svg>
  );
}
