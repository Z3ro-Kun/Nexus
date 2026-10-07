/**
 * Types for the NEXUS REST API, matching the backend schemas. Only the fields the
 * frontend uses are modelled; responses may carry more.
 *
 * Sources: app/schemas/runs.py (RunCreate, RunRead), app/models/run.py (RunStatus),
 * app/state/models.py (RunState and its parts), app/events/base.py + app/events/types.py
 * (Event, payload types), app/orchestration/result.py (RunPhase, FinalResult),
 * app/orchestration/orchestrator.py (OrchestrationResult), app/core/exceptions.py (errors).
 */

/** Persisted run status (RunStatus). needs_clarification is terminal: nothing was planned or run. */
export type RunStatus = "created" | "completed" | "failed" | "needs_clarification";

/** Where a run is in its lifecycle (RunPhase), derived from its state by the backend. */
export type RunPhase =
  | "created"
  | "executing"
  | "waiting_for_approval"
  | "verifying"
  | "blocked"
  | "completed"
  | "failed"
  | "needs_clarification";

/** Why the planner did not plan, and what to ask (ClarificationRequested / ClarificationResult). */
export interface Clarification {
  reason: "underspecified" | "not_a_request";
  question: string;
  missing: string[];
}

/** Task status in projected state (TaskStatus). pending/ready/blocked are derived from dependencies. */
export type TaskStatus = "pending" | "ready" | "running" | "completed" | "failed" | "blocked" | "cancelled";

/** POST /runs request body (RunCreate). `goal`: 1 to 10,000 characters. */
export interface RunCreate {
  goal: string;
  constraints?: string[];
}

/** POST /runs and GET /runs/{id} response (RunRead). */
export interface Run {
  id: string;
  goal: string;
  status: RunStatus;
  last_sequence: number;
  created_at: string;
  updated_at: string;
}

// --- GET /runs/{id}/state (RunState) ------------------------------------------------------

export interface FactClaim {
  subject: string;
  attribute: string;
  value: number | string;
  unit: string | null;
}

export interface Provenance {
  kind: "tool_output" | "task_context" | "model_knowledge" | string;
  tool_name: string | null;
  tool_call_id: string | null;
  source: string | null;
  fake: boolean;
}

export interface TaskFailure {
  failure_type: string;
  error_type: string | null;
  tool_call_id: string | null;
}

export interface TaskState {
  task_id: string;
  title: string;
  description: string | null;
  task_type: string | null;
  agent_type: string | null;
  dependencies: string[];
  status: TaskStatus;
  summary: string | null;
  error: string | null;
  failure: TaskFailure | null;
  replaces: string | null;
  replaced_by: string | null;
  conflict_id: string | null;
  /** Set for a verification checkpoint (VerificationSpec); only its presence is used here. */
  verification: unknown | null;
  /** Set for an action task (ActionSpec). */
  action: { tool_name: string; intent: string } | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface Fact {
  fact_id: string;
  content: string;
  source: string | null;
  agent_id: string | null;
  task_id: string | null;
  provenance: Provenance | null;
  claim: FactClaim | null;
  sequence: number;
}

export interface Artifact {
  artifact_id: string;
  name: string;
  media_type: string;
  content: string;
  agent_id: string | null;
  task_id: string | null;
  sequence: number;
}

/**
 * A generated file, project or archive (GET /runs/{id}/artifacts, ArtifactView). Bytes are
 * only downloadable when `deliverable` (ready: verified, then re-checked by checksum).
 */
export interface ArtifactView {
  artifact_id: string;
  name: string;
  artifact_type: "file" | "project" | "archive";
  status: "created" | "validated" | "rejected" | "ready";
  deliverable: boolean;
  verified: boolean;
  media_type: string | null;
  size: number | null;
  sha256: string | null;
  files: { path: string; media_type: string; size: number; sha256: string }[];
  archive_id: string | null;
  source_artifact_id: string | null;
  problems: string[];
  task_id: string | null;
  agent_id: string | null;
  tool_call_ids: string[];
  input_fact_ids: string[];
  download_url: string | null;
}

export type ToolCallStatus = "requested" | "succeeded" | "failed";

export interface ToolCallState {
  tool_call_id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  status: ToolCallStatus;
  error: string | null;
  error_type: string | null;
  metadata: Record<string, unknown>;
  agent_id: string | null;
  task_id: string | null;
  sequence: number;
}

export interface ConflictState {
  conflict_id: string;
  description: string | null;
  fact_ids: string[];
  status: "open" | "resolved" | "unresolved";
  resolution: string | null;
  conflict_type: string | null;
  fact_key: { subject: string; attribute: string } | null;
  resolution_task_id: string | null;
  resolved_fact_id: string | null;
  evidence_ids: string[];
}

export interface RecoveryRecord {
  replan_number: number;
  outcome: "accepted" | "rejected";
  failed_task_id: string;
  failure_type: string | null;
  summary: string;
  new_task_ids: string[];
  replacement_task_id: string | null;
  sequence: number;
}

export interface EvidenceRef {
  kind: "task" | "fact" | "artifact" | "tool_call" | "conflict";
  id: string;
}

export interface VerificationCheck {
  check_id: string;
  kind: string;
  passed: boolean;
  message: string;
  references: EvidenceRef[];
}

export interface SemanticJudgement {
  criterion: string;
  passed: boolean;
  evidence: EvidenceRef[];
  explanation: string;
}

export interface SemanticVerification {
  provider: string;
  model: string;
  passed: boolean;
  objective: SemanticJudgement;
  constraints: SemanticJudgement[];
  summary: string;
}

export type VerificationStatus = "pending" | "blocked" | "running" | "passed" | "failed" | "error" | "cancelled";

export interface VerificationState {
  verification_id: string;
  checkpoint_id: string;
  attempt: number;
  spec: { objective: string; semantic: boolean };
  dependencies: string[];
  status: VerificationStatus;
  covered_task_ids: string[];
  fact_ids: string[];
  artifact_ids: string[];
  checks: VerificationCheck[];
  semantic: SemanticVerification | null;
  failed_references: EvidenceRef[];
  reason: string | null;
  replaced_by: string | null;
}

export interface ApprovalState {
  approval_id: string;
  task_id: string;
  description: string;
  status: "pending" | "granted" | "rejected";
  actor: string | null;
  decision_reason: string | null;
}

export interface RunState {
  run_id: string;
  goal: string;
  constraints: string[];
  status: RunStatus;
  tasks: Record<string, TaskState>;
  facts: Record<string, Fact>;
  artifacts: Record<string, Artifact>;
  tool_calls: Record<string, ToolCallState>;
  conflicts: Record<string, ConflictState>;
  /** Set when the run is needs_clarification. Absent from older backends. */
  clarification?: Clarification | null;
  /** Generated files by artifact id; only presence and status are read here (details: listArtifacts). */
  workspace_artifacts?: Record<string, { artifact_id: string; status: string }>;
  recovery: { replan_count: number; replan_attempts: number; history: RecoveryRecord[] };
  verifications: Record<string, VerificationState>;
  approvals: Record<string, ApprovalState>;
  completion_summary: string | null;
  failure_reason: string | null;
  last_sequence: number;
  updated_at: string;
}

// --- GET /runs/{id}/events (Event) ----------------------------------------------------------

/** A stored event. `event_type` is a string so event types added later still render. */
export interface NexusEvent {
  id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  timestamp: string;
  agent_id: string | null;
  task_id: string | null;
  payload: Record<string, unknown>;
}

// --- GET /runs/{id}/result (FinalResult) ------------------------------------------------------

export interface TaskSummary {
  task_id: string;
  title: string;
  agent_type: string | null;
  task_type: string | null;
  status: TaskStatus;
  replaces: string | null;
  replaced_by: string | null;
  conflict_id: string | null;
  is_action: boolean;
  is_checkpoint: boolean;
}

export interface ResultArtifact {
  artifact_id: string;
  name: string;
  media_type: string;
  content: string;
  truncated: boolean;
}

export interface Deliverable {
  task_id: string;
  title: string;
  agent_type: string | null;
  summary: string | null;
  artifacts: ResultArtifact[];
}

export interface SupportingFact {
  fact_id: string;
  task_id: string | null;
  content: string;
  claim: FactClaim | null;
  provenance_kind: string | null;
  source: string | null;
  tool_name: string | null;
  tool_call_id: string | null;
  fake: boolean;
}

export interface FinalResult {
  run_id: string;
  objective: string;
  constraints: string[];
  phase: RunPhase;
  verified: boolean;
  completion_summary: string | null;
  failure_reason: string | null;
  completion_blockers: string[];
  /** Set when phase is needs_clarification. Absent from older backends. */
  clarification?: Clarification | null;
  deliverables: Deliverable[];
  supporting_facts: SupportingFact[];
  tasks: TaskSummary[];
  tool_calls: { total?: number; succeeded?: number; failed?: number; by_tool?: Record<string, number> };
  last_sequence: number;
}

/** POST /runs/{id}/execute response (OrchestrationResult). */
export interface ExecutionResult {
  run_id: string;
  phase: RunPhase;
  planned: boolean;
  passes: number;
  started: string[];
  completed: string[];
  failed: string[];
  replanned: string[];
  approvals_pending: string[];
  result: FinalResult;
}

/** Error body of NEXUS errors: {"error": {"code", "message"}}. */
export interface NexusErrorBody {
  error: { code: string; message: string };
}
