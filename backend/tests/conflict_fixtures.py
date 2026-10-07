"""Helpers for Phase 6 tests: a deterministic conflicting-facts scenario.

No real LLM, no network. Agents are scripted with FakeLLMProvider; every price comes from
FakeSearchBackend (results on the reserved fake-search.invalid domain, marked fake).

Scenario ("Research several candidate products and compare them."):

    research_a  (source A: Product X price = 94999 INR) --\\
                                                           +--> compare
    research_b  (source B: Product X price = 99999 INR) --/

The two prices conflict; a resolution task (resolve_conflict_<hash>) consults source C.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.agents.runtime import AgentTaskExecutor
from app.conflicts.manager import ConflictManager
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.schemas import LLMRequest
from app.orchestration.scheduler import Scheduler
from app.persistence.database import Database
from tests.agent_fixtures import planned
from tests.recovery_fixtures import TIMEOUT, Harness, url
from tests.tool_fixtures import call_tool, finish, turn_of

RA, RB, COMPARE = "research_a", "research_b", "compare"
QA, QB, QC, QD = "product x price source a", "product x price source b", "product x price source c", "product x price source d"

PLAN: dict[str, Any] = {
    "tasks": [
        planned(RA, description="Find Product X's price using source A (web search)."),
        planned(RB, description="Find Product X's price using source B (web search)."),
        planned(COMPARE, agent_type="analyst", task_type="analysis", dependencies=[RA, RB],
                description="Compare the candidate products and recommend one."),
    ]
}


def claim(value: Any, subject: str = "Product X", attribute: str = "price", unit: str | None = "INR") -> dict[str, Any]:
    return {"subject": subject, "attribute": attribute, "value": value, "unit": unit}


def claimed_fact(task_id: str, query: str, value: Any, *, call: int = 1, basis: str = "tool_output", **claim_kw: Any) -> dict[str, Any]:
    subject = claim_kw.get("subject", "Product X")
    attribute = claim_kw.get("attribute", "price")
    return {
        "content": f"{subject} {attribute} is {value} per {query}.",
        "basis": basis,
        "tool_call_id": f"{task_id}.t{call}" if basis == "tool_output" else None,
        "source_url": url(query) if basis == "tool_output" else None,
        "claim": claim(value, **claim_kw),
    }


def search_then_report(task_id: str, query: str, *facts: dict[str, Any]) -> list[dict[str, Any]]:
    return [call_tool("web_search", query=query, max_results=1), finish(f"searched {query}", list(facts))]


def researcher(task_id: str, query: str, value: Any, **claim_kw: Any) -> list[dict[str, Any]]:
    return search_then_report(task_id, query, claimed_fact(task_id, query, value, **claim_kw))


COMPARE_STEPS = [finish("Compared the candidates.", [])]

STEPS: dict[str, list[dict[str, Any]]] = {
    RA: researcher(RA, QA, 94999),
    RB: researcher(RB, QB, 99999),
    COMPARE: COMPARE_STEPS,
}

Resolver = Callable[[str], list[dict[str, Any]]]


def resolver_finds(query: str, value: Any, **claim_kw: Any) -> Resolver:
    """A resolution agent that searches `query` and reports `value` from it."""
    return lambda task_id: researcher(task_id, query, value, **claim_kw)


def agent_reply(steps: Mapping[str, list[dict[str, Any]]], resolvers: Mapping[str, Resolver] | Resolver | None, gates: Mapping[str, Any] | None = None) -> Callable[[LLMRequest], FakeReply]:
    """Replies by task id. Tasks not in `steps` (resolution tasks and their replacements,
    whose ids are derived at run time) use `resolvers` (by id prefix, or one for all)."""

    def reply(request: LLMRequest) -> FakeReply:
        task_id = request.metadata["task_id"]
        if task_id in steps:
            script = steps[task_id]
        elif callable(resolvers):
            script = resolvers(task_id)
        else:
            [script_for] = [r for prefix, r in (resolvers or {}).items() if task_id.startswith(prefix)]
            script = script_for(task_id)
        turn = turn_of(request)
        gate = (gates or {}).get(task_id) if turn == 0 else None
        return FakeReply(data=script[min(turn, len(script) - 1)], wait_for=gate)

    return reply


def provider(
    steps: Mapping[str, list[dict[str, Any]]] = STEPS,
    resolvers: Mapping[str, Resolver] | Resolver | None = None,
    *,
    plan: Mapping[str, Any] = PLAN,
    replanner: Any = None,
    gates: Mapping[str, Any] | None = None,
) -> FakeLLMProvider:
    agents = agent_reply(steps, resolvers if resolvers is not None else resolver_finds(QC, 96999), gates)
    replies: dict[str, Any] = {"planner": FakeReply(data=plan), "agent:researcher": agents, "agent:analyst": agents}
    if replanner is not None:
        replies["replanner"] = replanner
    return FakeLLMProvider(replies)


class ConflictHarness(Harness):
    """The Phase 5 harness plus a ConflictManager (recovery optional)."""

    def __init__(self, database: Database, llm: FakeLLMProvider, search: Any, *, max_resolutions: int = 5, recovery: bool = False, max_replans: int = 2) -> None:
        super().__init__(database, llm, search, recovery=recovery, max_replans=max_replans)
        self.conflicts = ConflictManager(database, max_resolution_tasks=max_resolutions)
        executor = AgentTaskExecutor(self.registry, timeout_seconds=TIMEOUT)
        self.scheduler = Scheduler(database, executor, self.manager, self.conflicts)


def replacement_replanner(new_id: str, query: str) -> Callable[[LLMRequest], FakeReply]:
    """Replanner that replaces whichever task failed with a researcher using `query`."""

    def reply(request: LLMRequest) -> FakeReply:
        failed = request.metadata["failed_task_id"]
        return FakeReply(data={
            "strategy_summary": f"The source used by {failed} failed; consult another source.",
            "tasks": [{**planned(new_id, description=f"Find Product X's verified price using a different source ({query})."), "replaces": failed}],
        })

    return reply


def fake_search(fail: Sequence[str] = (), gates: Mapping[str, Any] | None = None) -> Any:
    from tests.recovery_fixtures import search_backend

    return search_backend(fail=list(fail), gates=gates)

