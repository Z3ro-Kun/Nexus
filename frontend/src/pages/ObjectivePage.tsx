import AutonomyStrip from "../components/AutonomyStrip";
import ObjectiveForm from "../components/ObjectiveForm";
import RecentRuns from "../components/RecentRuns";
import { useStartRun } from "../hooks/useStartRun";

export default function ObjectivePage() {
  const { start, creating, error } = useStartRun();

  return (
    <div className="animate-enter">
      <h1 className="max-w-[22ch] text-[2rem] leading-[1.15] font-medium tracking-[-0.02em] text-balance text-ink sm:text-[2.6rem]">
        Give it an objective. It plans, executes, verifies.
      </h1>

      <div className="mt-8">
        <ObjectiveForm busy={creating} onSubmit={(goal) => void start(goal)} />
        {error && (
          <div role="alert" className="mt-4 border-l-2 border-bad pl-4 text-sm">
            <p className="font-medium text-bad">Could not create the run.</p>
            <p className="mt-1 text-dim">{error}</p>
          </div>
        )}
      </div>

      <AutonomyStrip />

      <RecentRuns />
    </div>
  );
}
