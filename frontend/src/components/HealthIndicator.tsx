import { useEffect, useState } from "react";

import { getHealth, type Health } from "../api/health";
import { StatusDot } from "./ui";

type State = { kind: "checking" } | { kind: "online"; health: Health } | { kind: "offline" };

/** Backend reachability, checked once on load (GET /health). */
export default function HealthIndicator() {
  const [state, setState] = useState<State>({ kind: "checking" });

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal)
      .then((health) => setState({ kind: "online", health }))
      .catch(() => {
        if (!controller.signal.aborted) setState({ kind: "offline" });
      });
    return () => controller.abort();
  }, []);

  const [tone, label, title] =
    state.kind === "online"
      ? (["ok", "Connected", `API v${state.health.version} · ${state.health.environment}`] as const)
      : state.kind === "offline"
        ? (["bad", "API unreachable", "The NEXUS backend did not answer GET /health"] as const)
        : (["neutral", "Connecting…", undefined] as const);

  return (
    <span className="flex items-center gap-2 text-xs text-dim" role="status" aria-label="Backend status" title={title}>
      <StatusDot tone={tone} />
      {label}
    </span>
  );
}
