import { useState } from "react";
import MarkdownView from "../ui/MarkdownView";
import ChatAgentActivity from "./ChatAgentActivity";
import type { ConversationTurn } from "../../types/conversations";

interface ChatMessageProps {
  turn: ConversationTurn;
  isLatest?: boolean;
  onRefresh?: () => void;
}

export default function ChatMessage({ turn, isLatest, onRefresh }: ChatMessageProps) {
  const [showInspector, setShowInspector] = useState(false);
  const isRunning =
    turn.status === "created" ||
    turn.status === "running" ||
    turn.status === "executing" ||
    turn.status === "verifying";

  const deliverable = turn.deliverable;
  const summaryText = deliverable?.deliverable_summary || "";
  const facts = deliverable?.facts || [];
  const artifacts = deliverable?.artifacts || [];
  const verification = deliverable?.verification;

  return (
    <div className="space-y-6 py-4">
      {/* User Prompt Message */}
      <div className="flex justify-end gap-3">
        <div className="max-w-[85%] rounded-2xl bg-surface-2 border border-line px-4 py-3 shadow-xs">
          <p className="text-sm text-ink whitespace-pre-wrap">{turn.objective}</p>
          {turn.documents.length > 0 && (
            <div className="mt-2.5 flex flex-wrap gap-1.5 pt-2 border-t border-line/60">
              {turn.documents.map((doc) => (
                <span
                  key={doc.id}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-surface border border-line text-dim font-mono text-[11px]"
                >
                  📄 {doc.filename}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* NEXUS Assistant Response */}
      <div className="flex gap-3.5 items-start">
        {/* Nexus Avatar */}
        <div className="size-8 rounded-full bg-accent/15 border border-accent/30 flex items-center justify-center shrink-0 mt-0.5">
          <svg viewBox="0 0 20 20" className="size-4 text-accent" aria-hidden="true">
            <path
              d="M3 4.5L10 10M3 15.5L10 10M3 10h7m0 0l7 0"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              fill="none"
            />
            <circle cx="3" cy="4.5" r="1.5" fill="currentColor" />
            <circle cx="3" cy="10" r="1.5" fill="currentColor" />
            <circle cx="3" cy="15.5" r="1.5" fill="currentColor" />
            <circle cx="16.5" cy="10" r="2.2" fill="currentColor" />
          </svg>
        </div>

        {/* Response Body */}
        <div className="flex-1 min-w-0 space-y-4">
          {/* Running State with Live Isometric Worker & Agent Activity */}
          {isRunning && (
            <ChatAgentActivity
              runId={turn.run_id}
              isRunning={isRunning}
              startedAt={turn.created_at}
              onFinished={onRefresh}
            />
          )}

          {/* Completed Deliverable */}
          {!isRunning && (
            <div className="space-y-4">
              {/* Deliverable Markdown Rendering */}
              {turn.status === "failed" ? (
                <div className="bg-bad/10 border border-bad/30 rounded-xl p-4 shadow-2xs space-y-2.5">
                  <div className="flex items-center gap-2 text-bad font-medium text-sm">
                    <svg className="size-4 shrink-0" viewBox="0 0 20 20" fill="currentColor">
                      <path
                        fillRule="evenodd"
                        d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z"
                        clipRule="evenodd"
                      />
                    </svg>
                    <span>Run failed to achieve objective</span>
                  </div>
                  <p className="text-xs text-ink/90 leading-relaxed font-mono">
                    {deliverable?.failure_reason || "The run stopped before objective completion."}
                  </p>
                  {summaryText && (
                    <div className="pt-2 border-t border-bad/20">
                      <p className="text-[11px] font-semibold text-dim uppercase tracking-wider mb-1.5">
                        Partial summary before stopping:
                      </p>
                      <MarkdownView content={summaryText} />
                    </div>
                  )}
                </div>
              ) : summaryText ? (
                <div className="bg-surface/50 border border-line/80 rounded-xl p-4 shadow-2xs">
                  <MarkdownView content={summaryText} />
                </div>
              ) : facts.length > 0 ? (
                <div className="bg-surface/50 border border-line/80 rounded-xl p-4 shadow-2xs space-y-2">
                  <h4 className="text-xs font-semibold tracking-wider uppercase text-dim">Verified Findings</h4>
                  <ul className="space-y-1.5">
                    {facts.map((f) => (
                      <li key={f.fact_id} className="text-sm text-ink flex items-start gap-2">
                        <span className="text-accent mt-0.5">•</span>
                        <span>{f.content}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <p className="text-sm text-dim italic">Run completed with status: {turn.status}</p>
              )}

              {/* Verification & Meta Pill */}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                {verification?.passed && (
                  <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    <svg className="size-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M5 13l4 4L19 7" />
                    </svg>
                    Verified Result
                  </span>
                )}

                {facts.length > 0 && (
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs text-dim bg-surface-2 border border-line">
                    {facts.length} facts established
                  </span>
                )}

                {artifacts.length > 0 && (
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs text-accent bg-accent/10 border border-accent/20">
                    📁 {artifacts.length} file deliverables
                  </span>
                )}

                {/* Inspect Button */}
                <button
                  type="button"
                  onClick={() => setShowInspector(!showInspector)}
                  className="ml-auto text-xs text-dim hover:text-ink underline-offset-2 hover:underline transition-colors"
                >
                  {showInspector ? "Hide details" : "Inspect trace"}
                </button>
              </div>

              {/* Collapsible Inspector */}
              {showInspector && (
                <div className="mt-3 p-3 bg-surface-2 border border-line rounded-lg text-xs space-y-2">
                  <div className="flex justify-between items-center text-dim border-b border-line pb-1.5">
                    <span>Run ID: <code className="text-ink font-mono">{turn.run_id}</code></span>
                    <span>Status: <span className="text-ink font-medium">{turn.status}</span></span>
                  </div>
                  {facts.length > 0 && (
                    <div className="space-y-1">
                      <p className="font-semibold text-ink">Facts ({facts.length}):</p>
                      {facts.map((f) => (
                        <p key={f.fact_id} className="text-dim pl-2 border-l border-line">
                          • {f.content}
                        </p>
                      ))}
                    </div>
                  )}
                  <div className="pt-1">
                    <a
                      href={`/runs/${turn.run_id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-accent hover:underline inline-flex items-center gap-1"
                    >
                      Open full execution graph in new tab ↗
                    </a>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
