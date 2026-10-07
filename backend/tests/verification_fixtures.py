"""Helpers for Phase 7 tests: verification checkpoints over deterministic scenarios.

No real LLM, no network. Planner, agents, replanner and the semantic verifier (purpose
"verifier") are scripted with FakeLLMProvider; every price comes from FakeSearchBackend
(results on the reserved fake-search.invalid domain, marked fake).

Integration scenario (the Phase 6 plan):

    research_a  (source A: Product X price) --\\
                                               +--> compare (artifact "recommendation")
    research_b  (source B: Product X price) --/
                                                     verify_objective (checkpoint over all three)

`Log` builds event histories in memory for pure projector / checker tests.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from app.agents.runtime import AgentTaskExecutor
from app.conflicts.manager import ConflictManager
from app.events.base import Event
from app.events.types import (
    ArtifactAdded,
    ArtifactRequirement,
    EventPayload,
    FactAdded,
    FactClaim,
    FactRequirement,
    FailureType,
    Provenance,
    RunCreated,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
    TaskStarted,
    ToolCalled,
    ToolSucceeded,
    ValueConstraint,
    VerificationFailed,
    VerificationPassed,
    VerificationSpec,
    VerificationStarted,
)
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.schemas import LLMRequest
from app.orchestration.scheduler import Scheduler
from app.persistence.database import Database
from app.services.runs import RunService
from app.state.models import RunState
from app.state.projector import project
from app.verification.checkpoint import checkpoint_task
from app.verification.checks import context_refs, run_checks
from app.verification.manager import VerificationManager
from app.verification.semantic import LLMSemanticVerifier
from app.verification.verifier import Verifier, result_for
from tests.conflict_fixtures import COMPARE, PLAN, QA, QB, RA, RB, agent_reply, researcher, resolver_finds
from tests.recovery_fixtures import TIMEOUT, Harness, url
from tests.tool_fixtures import finish

GOAL = "Research several candidate products and compare them."
VERIFY = "verify_objective"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = uuid4()

RECOMMENDATION = {"product": "Product X", "price": 94999, "decision": "buy"}


def recommendation_artifact(content: Mapping[str, Any] | str = RECOMMENDATION, name: str = "recommendation") -> dict[str, str]:
    return {"name": name, "media_type": "application/json",
            "content": content if isinstance(content, str) else json.dumps(content)}


def compare_steps(artifacts: Sequence[dict[str, str]] = (), summary: str = "Compared the candidates.") -> list[dict[str, Any]]:
    return [finish(summary, [], artifacts=list(artifacts))]


def steps(a: Any = 94999, b: Any = 94999, *, compare: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
    return {
        RA: researcher(RA, QA, a),
        RB: researcher(RB, QB, b),
        COMPARE: compare if compare is not None else compare_steps([recommendation_artifact()]),
    }


def spec(
    *,
    price_limit: int | None = 100000,
    tool_derived: bool = True,
    artifact: bool = True,
    tool_evidence: Sequence[str] = (RA, RB),
    semantic: bool = False,
    objective: str = "Find Product X's verified price and a purchase recommendation.",
) -> VerificationSpec:
    constraint = ValueConstraint(operator="le", value=price_limit, unit="INR") if price_limit is not None else None
    return VerificationSpec(
        objective=objective,
        required_facts=[FactRequirement(subject="Product X", attribute="price", tool_derived=tool_derived, constraint=constraint)],
        required_artifacts=[ArtifactRequirement(name="recommendation", media_type="application/json", json_fields=["product", "decision"])]
        if artifact else [],
        tool_evidence_tasks=list(tool_evidence),
        semantic=semantic,
    )


# --- semantic verifier replies ---------------------------------------------------------------


def judgement(verdict: str = "pass", evidence: Sequence[str] = (f"{RA}.f1",), explanation: str = "Supported by the evidence.") -> dict[str, Any]:
    return {"verdict": verdict, "evidence": list(evidence), "explanation": explanation}


def verifier_output(objective: dict[str, Any] | None = None, constraints: Sequence[dict[str, Any]] = (), summary: str = "Checked.") -> dict[str, Any]:
    return {
        "objective": objective or judgement(),
        "constraints": [{**c, "constraint_index": c.get("constraint_index", i)} for i, c in enumerate(constraints)],
        "summary": summary,
    }


def provider(
    agent_steps: Mapping[str, list[dict[str, Any]]] | None = None,
    *,
    verifier: Any = None,
    replanner: Any = None,
    resolvers: Any = None,
    plan: Mapping[str, Any] = PLAN,
    gates: Mapping[str, Any] | None = None,
) -> FakeLLMProvider:
    agents = agent_reply(agent_steps or steps(), resolvers if resolvers is not None else resolver_finds("product x price source c", 96999), gates)
    replies: dict[str, Any] = {
        "planner": FakeReply(data=plan),
        "agent:researcher": agents,
        "agent:analyst": agents,
        "agent:specialist": agents,
    }
    if verifier is not None:
        replies["verifier"] = verifier if callable(verifier) else FakeReply(data=verifier)
    if replanner is not None:
        replies["replanner"] = replanner
    return FakeLLMProvider(replies)


class VerificationHarness(Harness):
    """Planner -> scheduler with recovery, conflicts and verification, over a real database."""

    def __init__(
        self,
        database: Database,
        llm: FakeLLMProvider,
        search: Any,
        *,
        recovery: bool = True,
        max_replans: int = 2,
        semantic: bool = True,
        verification: bool = True,
        verifier_timeout: float = TIMEOUT,
    ) -> None:
        super().__init__(database, llm, search, recovery=recovery, max_replans=max_replans)
        self.conflicts = ConflictManager(database, max_resolution_tasks=5)
        self.verification = (
            VerificationManager(
                database,
                Verifier(LLMSemanticVerifier(llm, max_tokens=4000) if semantic else None),
                timeout_seconds=verifier_timeout,
            )
            if verification else None
        )
        self.scheduler = self.make_scheduler()

    def make_scheduler(self) -> Scheduler:
        return Scheduler(
            self.database, AgentTaskExecutor(self.registry, timeout_seconds=TIMEOUT), self.manager,
            self.conflicts, self.verification,
        )

    async def checkpoint(self, run_id: UUID, spec: VerificationSpec, *, task_id: str = VERIFY,
                         dependencies: Sequence[str] | None = None) -> None:
        async with self.database.session_factory() as session:
            service = RunService(session)
            state = await service.get_state(run_id)
            await service.create_tasks(
                run_id, [checkpoint_task(state, task_id=task_id, spec=spec, dependencies=dependencies)],
                agent_id="verification_api",
            )

    def verifier_calls(self) -> list[LLMRequest]:
        return [r for r in self.llm.requests if r.purpose == "verifier"]


# --- in-memory histories -------------------------------------------------------------------


class Log:
    """Builds a run history with valid tool provenance; each step may carry an envelope
    task id / agent id."""

    def __init__(self, goal: str = GOAL, constraints: Sequence[str] = ()) -> None:
        self.events: list[Event] = []
        self.add(RunCreated(goal=goal, constraints=list(constraints)))

    def add(self, payload: EventPayload, *, task: str | None = None, agent: str | None = None) -> "Log":
        n = len(self.events) + 1
        self.events.append(
            Event(id=uuid4(), run_id=RUN, sequence=n, event_type=payload.event_type,
                  timestamp=BASE + timedelta(seconds=n), agent_id=agent, task_id=task, payload=payload)
        )
        return self

    def state(self) -> RunState:
        return project(self.events)

    def create(self, task_id: str, *deps: str, agent: str = "researcher", task_type: str = "research", **kw: Any) -> "Log":
        return self.add(TaskCreated(task_id=task_id, title=task_id, agent_type=agent, task_type=task_type,
                                    dependencies=list(deps), **kw))

    def start(self, task_id: str) -> "Log":
        return self.add(TaskStarted(task_id=task_id), task=task_id)

    def tool(self, task_id: str, call: str = "t1", query: str | None = None) -> "Log":
        query = query or f"{task_id} source"
        call_id = f"{task_id}.{call}"
        self.add(ToolCalled(tool_call_id=call_id, tool_name="web_search", arguments={"query": query}), task=task_id, agent="researcher")
        return self.add(
            ToolSucceeded(tool_call_id=call_id, result={"query": query, "backend": "fake", "results": [{"url": url(query), "title": "t", "snippet": "s"}]},
                          metadata={"fake": True}),
            task=task_id, agent="researcher",
        )

    def fact(self, fact_id: str, task_id: str, value: Any = None, *, query: str | None = None, call: str = "t1",
             kind: str = "tool_output", subject: str = "Product X", attribute: str = "price", unit: str | None = "INR",
             provenance: Provenance | None = None, content: str | None = None, claim: bool = True) -> "Log":
        query = query or f"{task_id} source"
        if provenance is None:
            provenance = (
                Provenance(kind="tool_output", tool_name="web_search", tool_call_id=f"{task_id}.{call}", source=url(query), fake=True)
                if kind == "tool_output" else Provenance(kind=kind)  # type: ignore[arg-type]
            )
        payload = FactAdded(
            fact_id=fact_id, content=content or f"{subject} {attribute} is {value}", source=provenance.source,
            provenance=provenance,
            claim=FactClaim(subject=subject, attribute=attribute, value=value, unit=unit) if claim and value is not None else None,
        )
        return self.add(payload, task=task_id, agent="researcher")

    def artifact(self, artifact_id: str, task_id: str, name: str = "recommendation",
                 content: Mapping[str, Any] | str = RECOMMENDATION, media_type: str = "application/json") -> "Log":
        return self.add(
            ArtifactAdded(artifact_id=artifact_id, name=name, media_type=media_type,  # type: ignore[arg-type]
                          content=content if isinstance(content, str) else json.dumps(content)),
            task=task_id, agent="analyst",
        )

    def done(self, task_id: str, summary: str = "ok", **kw: Any) -> "Log":
        return self.add(TaskCompleted(task_id=task_id, summary=summary, **kw), task=task_id, agent="researcher")

    def researched(self, task_id: str, value: Any = 94999, *deps: str) -> "Log":
        return self.create(task_id, *deps).start(task_id).tool(task_id).fact(f"{task_id}.f1", task_id, value).done(task_id)

    def standard(self, a: Any = 94999, b: Any = 94999, artifact: Mapping[str, Any] | str | None = RECOMMENDATION) -> "Log":
        """ra, rb (tool-derived prices), compare (recommendation artifact)."""
        self.researched("ra", a).researched("rb", b)
        self.create("compare", "ra", "rb", agent="analyst", task_type="analysis").start("compare")
        if artifact is not None:
            self.artifact("compare.a1", "compare", content=artifact)
        return self.done("compare", summary="Recommend buying Product X.")

    def checkpoint(self, task_id: str = "verify", deps: Sequence[str] = ("ra", "rb", "compare"), spec_: VerificationSpec | None = None,
                   **kw: Any) -> "Log":
        return self.add(TaskCreated(task_id=task_id, title="verify", agent_type="verifier", task_type="verification",
                                    dependencies=list(deps), verification=spec_ or spec(tool_evidence=("ra", "rb")), **kw),
                        agent="verification_api")

    def begin(self, task_id: str = "verify") -> "Log":
        """TaskStarted + a correct VerificationStarted (what the manager records)."""
        self.start(task_id)
        state = self.state()
        refs = context_refs(state, task_id)
        return self.add(
            VerificationStarted(verification_id=task_id, task_id=task_id, based_on_sequence=state.last_sequence,
                                semantic=state.verifications[task_id].spec.semantic, **{k: list(v) for k, v in refs.items()}),
            task=task_id, agent="verifier",
        )

    def conclude(self, task_id: str = "verify", semantic: Any = None) -> "Log":
        """The verdict the manager would record against the current state."""
        result = result_for(task_id, run_checks(self.state(), task_id), semantic)
        if result.passed:
            self.add(VerificationPassed(verification_id=task_id, task_id=task_id, checks=list(result.checks),
                                        semantic=semantic, details=result.summary), task=task_id, agent="verifier")
            return self.add(TaskCompleted(task_id=task_id, summary="verified"), task=task_id, agent="verifier")
        self.add(VerificationFailed(verification_id=task_id, task_id=task_id, checks=list(result.checks),
                                    semantic=semantic, reason=result.reason or "failed"), task=task_id, agent="verifier")
        return self.add(TaskFailed(task_id=task_id, error="verification failed", failure_type=FailureType.VERIFICATION_FAILURE,
                                   error_type="verification_failed"), task=task_id, agent="verifier")


def in_order(*replies: Any) -> Callable[[LLMRequest], FakeReply]:
    calls = {"n": 0}

    def reply(_: LLMRequest) -> FakeReply:
        item = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            return FakeReply(error=item)  # type: ignore[arg-type]
        return item if isinstance(item, FakeReply) else FakeReply(data=item)

    return reply
