import { usePrefersReducedMotion } from "../hooks/usePrefersReducedMotion";

const LANES = [26, 60, 94];
const PLAN = { x: 40, y: 60 };
const AGENT = 184;
const TOOL = 272;
const HUB = { x: 412, y: 60 };
const VERIFY = { x: 590, y: 60 };

const toAgent = (y: number) => `M ${PLAN.x + 14} ${PLAN.y} C 112 ${PLAN.y}, 112 ${y}, ${AGENT - 16} ${y}`;
const toTool = (y: number) => `L ${TOOL - 8} ${y}`;
const toHub = (y: number) => `L ${TOOL + 8} ${y} C ${(TOOL + HUB.x) / 2} ${y}, ${(TOOL + HUB.x) / 2} ${HUB.y}, ${HUB.x - 20} ${HUB.y}`;
const lane = (y: number) => `M ${AGENT + 16} ${y} ${toTool(y)}`;
const route = (y: number) => `${toAgent(y)} L ${AGENT + 16} ${y} ${toTool(y)} ${toHub(y)}`;
const toVerify = `M ${HUB.x + 20} ${HUB.y} L ${VERIFY.x - 16} ${VERIFY.y}`;

/**
 * How a run works, shown once: the plan fans out to agents working at the same time, each
 * using tools; their findings meet in shared state, and verification checks what converged
 * there. One pass of pulses on mount, then it rests; static with reduced motion.
 * Illustrative only: it shows no run data.
 */
export default function AutonomyStrip() {
  const reduced = usePrefersReducedMotion();

  return (
    <figure className="mt-12" aria-labelledby="how-caption">
      <svg viewBox="0 0 640 120" className="h-auto w-full overflow-visible" role="img" aria-label="The planner splits an objective across agents working in parallel; each uses tools; their findings meet in shared state, where verification checks them.">
        <defs>
          <radialGradient id="strip-hub-glow">
            <stop offset="0" stopColor="var(--accent)" stopOpacity="0.2" />
            <stop offset="1" stopColor="var(--accent)" stopOpacity="0" />
          </radialGradient>
        </defs>
        <g fill="none" strokeWidth="1.25">
          {LANES.map((y) => (
            <g key={y}>
              <path d={toAgent(y)} stroke="var(--violet)" strokeOpacity="0.45" />
              <path d={lane(y)} stroke="var(--cyan)" strokeOpacity="0.45" />
              <path d={`M ${TOOL + 8} ${y} ${toHub(y).slice(toHub(y).indexOf("C"))}`} stroke="var(--accent)" strokeOpacity="0.4" />
            </g>
          ))}
          <path d={toVerify} stroke="var(--ok)" strokeOpacity="0.55" />
        </g>

        <rect x={PLAN.x - 10} y={PLAN.y - 10} width="20" height="20" rx="3" fill="var(--violet)" transform={`rotate(45 ${PLAN.x} ${PLAN.y})`} />
        {LANES.map((y) => (
          <g key={y} transform={`translate(${AGENT} ${y})`} color="var(--cyan)">
            <polygon points="-12,-3 0,-9 12,-3 0,3" fill="currentColor" fillOpacity="0.35" />
            <polygon points="-12,-3 0,3 0,12 -12,6" fill="currentColor" fillOpacity="0.85" />
            <polygon points="0,3 12,-3 12,6 0,12" fill="currentColor" fillOpacity="0.6" />
          </g>
        ))}
        {LANES.map((y) => (
          <rect key={y} x={TOOL - 7} y={y - 7} width="14" height="14" rx="3" fill="var(--surface)" stroke="var(--cyan)" strokeOpacity="0.7" strokeWidth="1.25" />
        ))}

        <circle cx={HUB.x} cy={HUB.y} r="38" fill="url(#strip-hub-glow)" />
        <circle cx={HUB.x} cy={HUB.y} r="21" fill="none" stroke="var(--accent)" strokeOpacity="0.4" strokeDasharray="3 3" />
        <circle cx={HUB.x} cy={HUB.y} r="15" fill="var(--surface)" stroke="var(--accent)" strokeOpacity="0.6" />
        <rect x={HUB.x - 5} y={HUB.y - 5} width="10" height="10" rx="1.5" fill="var(--accent)" transform={`rotate(45 ${HUB.x} ${HUB.y})`} />

        <g transform={`translate(${VERIFY.x} ${VERIFY.y})`}>
          <circle r="15" fill="var(--ok)" fillOpacity="0.14" stroke="var(--ok)" strokeWidth="1.5" />
          <path d="M-6 0 l4 4 l8 -9" fill="none" stroke="var(--ok)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
        </g>

        {!reduced && (
          <g aria-hidden="true">
            {LANES.map((y, i) => (
              <path key={y} d={route(y)} pathLength={100} className="comet comet-once" stroke="var(--accent)" strokeWidth="2.5" style={{ ["--dur" as string]: "2.4s", ["--delay" as string]: `${0.3 + i * 0.18}s` }} />
            ))}
            <path d={toVerify} pathLength={100} className="comet comet-once" stroke="var(--ok)" strokeWidth="2.5" style={{ ["--dur" as string]: "0.9s", ["--delay" as string]: "2.6s" }} />
            <circle cx={HUB.x} cy={HUB.y} r="15" fill="none" stroke="var(--accent)" strokeWidth="2" className="ripple" style={{ transformBox: "fill-box", transformOrigin: "center", animationDelay: "2.35s" }} />
          </g>
        )}
      </svg>
      <figcaption id="how-caption" className="mt-3 grid grid-cols-4 gap-2 text-xs sm:text-sm">
        <span className="font-medium" style={{ color: "var(--violet)" }}>
          Plan
          <span className="mt-0.5 hidden font-normal text-dim sm:block">Split into tasks</span>
        </span>
        <span className="text-center font-medium" style={{ color: "var(--cyan)" }}>
          Agents
          <span className="mt-0.5 hidden font-normal text-dim sm:block">Work in parallel, with tools</span>
        </span>
        <span className="text-center font-medium text-accent">
          Shared state
          <span className="mt-0.5 hidden font-normal text-dim sm:block">Findings meet here</span>
        </span>
        <span className="text-right font-medium text-ok">
          Verify
          <span className="mt-0.5 hidden font-normal text-dim sm:block">Checked before you see it</span>
        </span>
      </figcaption>
    </figure>
  );
}
