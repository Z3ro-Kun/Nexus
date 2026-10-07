import type { EvidenceRef, FinalResult, RunState, VerificationState } from "../../../types/api";
import { Badge, Empty, Mono, Panel } from "./ui";

/**
 * Verification from the backend's own state: every checkpoint attempt with its checks and
 * semantic verdict. "VERIFIED" comes from the backend's `verified` flag, never from the run
 * merely having completed.
 */
export default function VerificationPanel({ state, result }: { state: RunState | null; result: FinalResult | null }) {
  const attempts = Object.values(state?.verifications ?? {}).sort(
    (a, b) => a.checkpoint_id.localeCompare(b.checkpoint_id) || a.attempt - b.attempt,
  );
  const failed = attempts.some((v) => v.status === "failed" && !v.replaced_by);
  const verdict = result?.verified ? (
    <Badge value="verified" tone="ok" label="Verified" />
  ) : failed ? (
    <Badge value="failed" tone="bad" label="Verification failed" />
  ) : attempts.some((v) => v.status === "running") ? (
    <Badge value="verifying" />
  ) : (
    <Badge value="pending" label="Not verified yet" />
  );

  return (
    <Panel title="Verification record" aside={verdict}>
      {attempts.length === 0 ? (
        <Empty>No verification checkpoint yet.</Empty>
      ) : (
        <ul className="space-y-4">
          {attempts.map((v) => (
            <Attempt key={v.verification_id} v={v} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function Attempt({ v }: { v: VerificationState }) {
  return (
    <li aria-label={`Verification ${v.verification_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Mono className="text-ink">{v.verification_id}</Mono>
        <Mono className="text-faint">attempt {v.attempt}</Mono>
        <Badge value={v.status} />
        {v.replaced_by && <Mono className="text-warn">replaced by {v.replaced_by}</Mono>}
      </div>
      {v.covered_task_ids.length > 0 && (
        <p className="mt-1 font-mono text-[11px] text-faint">covers {v.covered_task_ids.join(", ")}</p>
      )}
      {v.reason && <p className="mt-1.5 text-xs text-bad">{v.reason}</p>}

      {v.checks.length > 0 && (
        <ul className="mt-2 space-y-1" aria-label="Deterministic checks">
          {v.checks.map((c) => (
            <li key={c.check_id} className="flex items-baseline gap-2 text-xs">
              <Mono className={c.passed ? "text-ok" : "text-bad"}>{c.passed ? "PASS" : "FAIL"}</Mono>
              <Mono className="text-dim">{c.check_id}</Mono>
              <span className="text-faint">{c.message}</span>
            </li>
          ))}
        </ul>
      )}

      {v.semantic && (
        <div className="mt-3 rounded border border-line bg-raised px-3 py-2 text-xs" aria-label="Semantic verdict">
          <div className="flex flex-wrap items-center gap-2">
            <Mono className="text-faint">semantic</Mono>
            <Badge value={v.semantic.passed ? "passed" : "failed"} />
            <Mono className="text-faint">
              {v.semantic.provider} / {v.semantic.model}
            </Mono>
          </div>
          <p className="mt-1.5 text-dim">{v.semantic.objective.explanation}</p>
          <Refs refs={v.semantic.objective.evidence} />
          {v.semantic.constraints.map((c, i) => (
            <p key={i} className="mt-1 text-faint">
              <Mono className={c.passed ? "text-ok" : "text-bad"}>{c.passed ? "PASS" : "FAIL"}</Mono> {c.criterion}: {c.explanation}
            </p>
          ))}
          {v.semantic.summary && <p className="mt-1.5 text-faint">{v.semantic.summary}</p>}
        </div>
      )}
      {v.failed_references.length > 0 && <Refs refs={v.failed_references} label="failed references" />}
    </li>
  );
}

function Refs({ refs, label = "evidence" }: { refs: EvidenceRef[]; label?: string }) {
  if (refs.length === 0) return null;
  return (
    <p className="mt-1 font-mono text-[11px] text-faint">
      {label}: {refs.map((r) => `${r.kind}:${r.id}`).join(", ")}
    </p>
  );
}
