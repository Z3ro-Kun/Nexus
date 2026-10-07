"""Conflict resolution: the resolution task and the deterministic verdict on its evidence.

    ConflictCandidate -> resolution_task()   -> TaskCreated(conflict_id=...)
    completed resolver task -> evaluate()     -> ConflictResolved | ConflictUnresolved

The resolution task is an ordinary researcher task, run by the ordinary agent runtime
with its ordinary tools and limits. The agent only *reports facts*; it never declares a
winner. `evaluate` decides, using only recorded facts and their provenance:

- usable evidence = facts of the resolver task with a claim on the conflict's key, that
  are tool-derived, from a source other than the conflicting facts' sources
  (`app.state.conflict_detector.assess_evidence`);
- no usable evidence                   -> ConflictUnresolved (no winner is invented);
- usable evidence that disagrees        -> ConflictUnresolved;
- usable evidence that agrees on a value -> ConflictResolved: the first evidence fact is
  the accepted fact; conflicting facts with that value are listed as corroborated.

No confidence scores, agent priorities, recency or LLM opinions are used.
"""

from app.events.types import ConflictResolved, ConflictUnresolved, TaskCreated
from app.state.conflict_detector import (
    ConflictCandidate,
    assess_evidence,
    canonical_value,
    display_value,
    fact_key,
)
from app.state.models import ConflictState, RunState, TaskStatus

RESOLUTION_AGENT_TYPE = "researcher"
RESOLUTION_TASK_TYPE = "research"


def resolution_task_id(conflict_id: str) -> str:
    return f"resolve_{conflict_id}"


def resolution_task(state: RunState, candidate: ConflictCandidate) -> TaskCreated:
    """A researcher task to find new evidence for the conflict's key. It depends on the
    completed tasks that produced the conflicting facts, so it receives their results."""
    facts = [state.facts[fid] for fid in candidate.fact_ids]
    sources = sorted({f.provenance.source for f in facts if f.provenance and f.provenance.source})
    claims = "; ".join(f"{f.fact_id}: {display_value(f.claim)}" for f in facts if f.claim)
    dependencies = sorted(
        {
            f.task_id
            for f in facts
            if f.task_id in state.tasks and state.tasks[f.task_id].status is TaskStatus.COMPLETED
        }
    )
    key = candidate.fact_key
    return TaskCreated(
        task_id=resolution_task_id(candidate.conflict_id),
        title=f"Resolve conflict: {key.subject} / {key.attribute}"[:200],
        description=(
            f"Determine the current verified {key.attribute} of {key.subject}. "
            f"Conflicting claims ({candidate.conflict_type.value}): {claims}. "
            f"Sources already used: {', '.join(sources) or 'none recorded'}. Use a tool to consult a "
            "different source and report what it says as a tool-derived fact with the same subject "
            "and attribute. Do not choose between the claims without new evidence."
        )[:2000],
        task_type=RESOLUTION_TASK_TYPE,
        agent_type=RESOLUTION_AGENT_TYPE,
        dependencies=dependencies,
        conflict_id=candidate.conflict_id,
    )


def evaluate(state: RunState, conflict: ConflictState, resolver_task_id: str) -> ConflictResolved | ConflictUnresolved:
    assert conflict.fact_key is not None
    assessment = assess_evidence(state, conflict, resolver_task_id)
    on_key = [
        f.fact_id
        for f in sorted(state.facts.values(), key=lambda f: f.sequence)
        if f.task_id == resolver_task_id and f.claim is not None and fact_key(f.claim) == conflict.fact_key
    ]
    excluded = "; ".join(f"{fid}: {why}" for fid, why in assessment.excluded)
    if not assessment.usable:
        return ConflictUnresolved(
            conflict_id=conflict.conflict_id,
            resolver_task_id=resolver_task_id,
            evidence_fact_ids=on_key,
            reason=(
                "no independent tool-derived evidence on "
                f"{conflict.fact_key.subject} / {conflict.fact_key.attribute}"
                + (f" (excluded: {excluded})" if excluded else "")
            )[:1000],
        )
    evidence = [state.facts[fid] for fid in assessment.usable]
    values = {canonical_value(f.claim) for f in evidence if f.claim}
    if len(values) != 1:
        return ConflictUnresolved(
            conflict_id=conflict.conflict_id,
            resolver_task_id=resolver_task_id,
            evidence_fact_ids=on_key,
            reason=(
                "the new evidence disagrees: "
                + "; ".join(f"{f.fact_id} = {display_value(f.claim)}" for f in evidence if f.claim)
            )[:1000],
        )
    [accepted] = values
    corroborated = [fid for fid in conflict.fact_ids if canonical_value(state.facts[fid].claim) == accepted]  # type: ignore[arg-type]
    first = evidence[0]
    assert first.claim is not None and first.provenance is not None
    sources = ", ".join(sorted({f.provenance.source for f in evidence if f.provenance and f.provenance.source}))
    return ConflictResolved(
        conflict_id=conflict.conflict_id,
        resolved_fact_id=first.fact_id,
        evidence_fact_ids=[f.fact_id for f in evidence],
        corroborated_fact_ids=corroborated,
        resolver_task_id=resolver_task_id,
        reason=(
            f"independent tool evidence ({sources}) gives {display_value(first.claim)}"
            + (f"; corroborates {', '.join(corroborated)}" if corroborated else "; matches none of the conflicting facts")
        )[:1000],
    )
