/**
 * Plain-language names for backend identifiers (agent types, tools, check kinds, failure
 * types). Unknown values fall back to a humanized form of the identifier, so new backend
 * values still render.
 */

export function humanize(value: string): string {
  const text = value.replace(/[_.-]+/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

const ROLES: Record<string, string> = {
  planner: "Planner",
  researcher: "Researcher",
  analyst: "Analyst",
  specialist: "Specialist",
  verifier: "Verifier",
  conflict_resolver: "Conflict resolver",
  replanner: "Replanner",
};

/** The colour family of an agent role: planning/reasoning violet, research cyan, checking green. */
export type RoleHue = "violet" | "cyan" | "accent" | "ok" | "amber";

const HUES: Record<string, RoleHue> = {
  planner: "violet",
  replanner: "violet",
  analyst: "violet",
  researcher: "cyan",
  specialist: "accent",
  verifier: "ok",
  conflict_resolver: "amber",
};

export function roleHue(agentType: string | null | undefined): RoleHue {
  return (agentType && HUES[agentType]) || "accent";
}

/** CSS colour for a role hue (inline SVG and custom properties). */
export const HUE_VAR: Record<RoleHue, string> = {
  violet: "var(--violet)",
  cyan: "var(--cyan)",
  accent: "var(--accent)",
  ok: "var(--ok)",
  amber: "var(--amber)",
};

export function roleLabel(agentType: string | null | undefined): string {
  if (!agentType) return "Agent";
  return ROLES[agentType] ?? humanize(agentType);
}

const TOOLS: Record<string, [singular: string, plural: string]> = {
  http_fetch: ["web fetch", "web fetches"],
  web_search: ["web search", "web searches"],
  calculator: ["calculation", "calculations"],
  python_analysis: ["Python analysis", "Python analyses"],
};

export function toolLabel(toolName: string, count = 1): string {
  const known = TOOLS[toolName];
  if (known) return count === 1 ? known[0] : known[1];
  const name = toolName.replace(/[_-]+/g, " ");
  return count === 1 ? `${name} call` : `${name} calls`;
}

/** "2 web fetches", "1 calculation". */
export function countTools(toolName: string, count: number): string {
  return `${count} ${toolLabel(toolName, count)}`;
}

const CHECKS: Record<string, [passed: string, failed: string]> = {
  tasks_completed: ["All planned work finished", "Some planned work did not finish"],
  provenance: ["Every finding traces to a recorded source", "Some findings lack a valid source"],
  conflicts: ["No unresolved conflicting information", "Conflicting information is unresolved"],
  required_fact: ["A required finding is present", "A required finding is missing"],
  required_artifact: ["A required output is present", "A required output is missing"],
  tool_evidence: ["Backed by real tool output", "Not backed by tool output"],
  actions: ["Actions ran only as approved", "An action did not run as approved"],
  artifacts: ["Generated files passed NEXUS's file checks", "Some generated files did not pass NEXUS's file checks"],
};

export function checkLabel(kind: string, passed: boolean, message: string): string {
  const known = CHECKS[kind];
  return known ? known[passed ? 0 : 1] : message;
}

const FAILURES: Record<string, string> = {
  TOOL_FAILURE: "A tool call failed",
  AGENT_FAILURE: "The agent could not complete the task",
  PLANNING_FAILURE: "The task could not run as planned",
  VALIDATION_FAILURE: "The agent's output did not pass validation",
  DEPENDENCY_FAILURE: "A task it depends on failed",
  TIMEOUT: "It ran out of time",
  POLICY_FAILURE: "A safety policy blocked an action",
  VERIFICATION_FAILURE: "Verification found a problem",
};

export function failureLabel(failureType: string | null | undefined): string {
  if (!failureType) return "It failed";
  return FAILURES[failureType] ?? humanize(failureType);
}

/** "example.com" from "https://example.com/path"; non-URLs are returned unchanged. */
export function hostOf(source: string): string {
  try {
    const url = new URL(source);
    return url.host + (url.pathname !== "/" ? url.pathname : "");
  } catch {
    return source;
  }
}
