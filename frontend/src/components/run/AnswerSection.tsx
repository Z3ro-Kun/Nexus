import { roleLabel } from "../../lib/labels";
import { stoppedBecause } from "../../lib/runView";
import type { FinalResult, ResultArtifact, RunState } from "../../types/api";
import { Section, TechnicalDetail } from "../ui";

/**
 * The run's answer: its deliverables (the final pieces of work, as the backend defines them)
 * once the run has finished. A failed run shows why, then any partial results.
 */
export default function AnswerSection({ state, result }: { state: RunState; result: FinalResult | null }) {
  if (!result || (result.phase !== "completed" && result.phase !== "failed")) return null;
  const failed = result.phase === "failed";
  const reasons = failed ? stoppedBecause(state) : [];
  const delivered = result.deliverables.filter((d) => d.summary || d.artifacts.length > 0);

  return (
    <>
      {failed && (
        <Section title="What went wrong">
          <div className="max-w-[68ch] border-l-2 border-bad pl-4">
            {reasons.length > 0 ? (
              reasons.map((r) => (
                <p key={r} className="text-[15px] leading-relaxed text-ink">
                  {r}
                </p>
              ))
            ) : (
              <p className="text-[15px] leading-relaxed text-ink">{result.failure_reason ?? "The run failed without a recorded reason."}</p>
            )}
            {(result.failure_reason || result.completion_blockers.length > 0) && reasons.length > 0 && (
              <TechnicalDetail>
                {result.failure_reason && <p>{result.failure_reason}</p>}
                {result.completion_blockers.map((b) => (
                  <p key={b}>{b}</p>
                ))}
              </TechnicalDetail>
            )}
          </div>
        </Section>
      )}
      {delivered.length > 0 && (
        <Section
          title={failed ? "Partial results" : "Answer"}
          id="answer"
          className={failed ? "" : "rounded-xl border border-line bg-surface px-4 pt-5 pb-6 sm:-mx-6 sm:px-6"}
        >
          <div className="space-y-8">
            {delivered.map((d) => (
              <article key={d.task_id} className="max-w-[68ch]">
                {d.summary && <p className={`leading-relaxed text-pretty whitespace-pre-wrap text-ink ${failed ? "text-[15px]" : "text-lg"}`}>{d.summary}</p>}
                {d.artifacts.map((a) => (
                  <ArtifactView key={a.artifact_id} artifact={a} />
                ))}
                <p className="mt-3 text-xs text-faint">
                  From “{d.title}”, {roleLabel(d.agent_type).toLowerCase()}
                </p>
              </article>
            ))}
          </div>
        </Section>
      )}
    </>
  );
}

/** A flat JSON object as a table; anything else as text. */
function ArtifactView({ artifact }: { artifact: ResultArtifact }) {
  const rows = flatJson(artifact);
  return (
    <figure className="mt-4">
      {rows ? (
        <table className="w-full border-y border-line text-sm">
          <tbody className="divide-y divide-line">
            {rows.map(([key, value]) => (
              <tr key={key}>
                <th scope="row" className="py-2 pr-4 text-left font-normal break-all text-dim">
                  {key}
                </th>
                <td className="py-2 font-medium break-words text-ink">{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <pre className="max-h-80 overflow-auto rounded-md bg-raised p-3 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap text-dim">{artifact.content}</pre>
      )}
      <figcaption className="mt-1.5 text-xs text-faint">
        {artifact.name}
        {artifact.truncated ? ", shortened" : ""}
      </figcaption>
    </figure>
  );
}

function flatJson(artifact: ResultArtifact): [string, string][] | null {
  if (!artifact.media_type.includes("json") || artifact.truncated) return null;
  try {
    const value: unknown = JSON.parse(artifact.content);
    if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
    const entries = Object.entries(value);
    if (entries.length === 0 || entries.length > 30 || entries.some(([, v]) => typeof v === "object" && v !== null)) return null;
    return entries.map(([k, v]) => [k, String(v)]);
  } catch {
    return null;
  }
}
