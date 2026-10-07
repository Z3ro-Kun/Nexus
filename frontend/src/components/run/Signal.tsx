import { useEffect, useRef, useState } from "react";

/**
 * A pulse of information travelling along `d` (see .comet in index.css). Callers render it
 * only for a real, active connection and never with reduced or paused motion.
 */
export function Comet({ d, color, dur = 1.9, delay = 0, once = false, width = 2 }: { d: string; color: string; dur?: number; delay?: number; once?: boolean; width?: number }) {
  const timing = { ["--dur" as string]: `${dur}s`, ["--delay" as string]: `${delay}s` };
  const cls = once ? " comet-once" : "";
  return (
    <g aria-hidden="true">
      <path d={d} pathLength={100} className={`comet comet-glow${cls}`} stroke={color} strokeWidth={width * 3.5} style={timing} />
      <path d={d} pathLength={100} className={`comet${cls}`} stroke={color} strokeWidth={width} style={timing} />
    </g>
  );
}

/** Counts how many times `value` has grown since mount, to replay a one-shot effect (a ripple). */
export function useGrowth(value: number): number {
  const previous = useRef(value);
  const [growth, setGrowth] = useState(0);
  useEffect(() => {
    if (value > previous.current) setGrowth((g) => g + 1);
    previous.current = value;
  }, [value]);
  return growth;
}
