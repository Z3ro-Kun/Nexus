import { useEffect } from "react";

import AnswerSection from "../components/run/AnswerSection";
import ArtifactsSection from "../components/run/ArtifactsSection";
import ClarificationSection from "../components/run/ClarificationSection";
import ConflictSection from "../components/run/ConflictSection";
import EvidenceSection from "../components/run/EvidenceSection";
import ExecutionDetails from "../components/run/ExecutionDetails";
import NowSection from "../components/run/NowSection";
import FlowGraph from "../components/run/FlowGraph";
import RecoverySection from "../components/run/RecoverySection";
import RunHeader from "../components/run/RunHeader";
import RunNotices from "../components/run/RunNotices";
import StageRail from "../components/run/StageRail";
import TrustSection from "../components/run/TrustSection";
import { Icon, buttonClass } from "../components/ui";
import { useRunObserver } from "../hooks/useRunObserver";
import { startExecution, useExecution } from "../lib/executions";
import { rememberRun } from "../lib/history";
import { linkTo } from "../lib/router";
import { clarificationOf, isTerminal, stages } from "../lib/runView";

/**
 * One run, reconstructed from the backend (works after a refresh). Reads top to bottom as
 * objective, progress, what is happening, the answer, why it can be trusted, the evidence,
 * and finally the execution details for anyone who wants the machinery.
 */
export default function RunPage({ runId }: { runId: string }) {
  const execution = useExecution(runId);
  const executing = execution?.status === "running";
  const observed = useRunObserver(runId, executing);
  const { state, result, events } = observed;
  const loaded = state !== null;

  useEffect(() => {
    if (loaded) rememberRun(runId);
  }, [loaded, runId]);

  if (!state) {
    return (
      <div className="space-y-6">
        <TopBar />
        {observed.loadError ? (
          <div role="alert" className="border-l-2 border-bad pl-4">
            <p className="text-[15px] font-medium text-bad">{observed.notFound ? "Run not found." : "Could not load the run."}</p>
            <p className="mt-1 text-sm text-dim">
              {observed.notFound ? "Check the link, or start a new objective from the home page." : `${observed.loadError} NEXUS keeps retrying.`}
            </p>
          </div>
        ) : (
          <p className="text-sm text-dim" role="status">
            Loading the run…
          </p>
        )}
      </div>
    );
  }

  const terminal = isTerminal(state);
  const plan = <FlowGraph state={state} events={events} />;
  const clarification = clarificationOf(state, result);

  if (clarification || state.status === "needs_clarification") {
    // Stopped before planning: no progress, plan, answer, verification or recovery to show.
    return (
      <div className="space-y-12">
        <div className="space-y-8">
          <TopBar terminal />
          <RunHeader state={state} result={result} events={events} executing={false} polling={observed.polling} pollError={observed.pollError} />
        </div>
        {clarification && <ClarificationSection clarification={clarification} />}
        <ExecutionDetails runId={runId} state={state} result={result} events={events} />
      </div>
    );
  }

  return (
    <div className="space-y-12">
      <div className="space-y-8">
        <TopBar terminal={terminal} />
        <RunHeader
          state={state}
          result={result}
          events={events}
          executing={executing}
          polling={observed.polling}
          pollError={observed.pollError}
        />
        <StageRail stages={stages(state, result, events, executing)} />
        <RunNotices state={state} result={result} execution={execution} onExecute={() => startExecution(runId)} onRefresh={() => void observed.refresh()} />
      </div>

      {!terminal && <NowSection state={state} result={result} executing={executing} />}
      {!terminal && plan}
      <AnswerSection state={state} result={result} />
      <ArtifactsSection runId={runId} state={state} />
      <TrustSection state={state} result={result} />
      <EvidenceSection state={state} events={events} />
      <ConflictSection state={state} />
      <RecoverySection state={state} />
      {terminal && plan}
      <ExecutionDetails runId={runId} state={state} result={result} events={events} />
    </div>
  );
}

function TopBar({ terminal = false }: { terminal?: boolean }) {
  return (
    <nav aria-label="Run" className="flex items-center justify-between gap-4">
      <a {...linkTo("/")} className={buttonClass.quiet + " -ml-2"}>
        <Icon name="arrow-left" className="size-3.5" />
        All runs
      </a>
      {terminal && (
        <a {...linkTo("/")} className={buttonClass.secondary}>
          <Icon name="plus" className="size-3.5" />
          Run another objective
        </a>
      )}
    </nav>
  );
}
