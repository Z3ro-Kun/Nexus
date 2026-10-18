export interface AttachedDocument {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
}

export interface ConversationTurnDeliverable {
  goal: string;
  status: string;
  failure_reason?: string | null;
  facts: Array<{ fact_id: string; content: string; source: string | null }>;
  artifacts: Array<{ artifact_id: string; name: string; media_type: string; content: string }>;
  deliverable_summary: string | null;
  verification: { passed: boolean; details: Record<string, unknown> } | null;
}

export interface ConversationTurn {
  run_id: string;
  parent_run_id: string | null;
  objective: string;
  status: string;
  created_at: string;
  updated_at: string;
  deliverable: ConversationTurnDeliverable | null;
  documents: AttachedDocument[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  turn_count: number;
  last_status: string | null;
}

export interface ConversationDetail {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  turns: ConversationTurn[];
}
