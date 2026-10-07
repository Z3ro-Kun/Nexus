"""Scripted LLM outputs for Phase 3 tests. Nothing here calls a real provider."""

import asyncio
from typing import Any

from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.schemas import LLMRequest

GOAL = "Research several candidate products and compare them."


def planned(
    task_id: str,
    agent_type: str = "researcher",
    task_type: str = "research",
    dependencies: list[str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": task_id.replace("_", " ").capitalize(),
        "task_type": task_type,
        "agent_type": agent_type,
        "description": f"Produce the result for {task_id.replace('_', ' ')}.",
        "dependencies": dependencies or [],
        **overrides,
    }


# T1 Research candidate products, T2 Research alternative candidates, T3 Compare results.
RESEARCH_PLAN: dict[str, Any] = {
    "tasks": [
        planned("research_candidates", description="Identify leading candidate products and their key traits."),
        planned("research_alternatives", description="Identify alternative candidates that are less obvious."),
        planned(
            "compare_results",
            agent_type="analyst",
            task_type="analysis",
            dependencies=["research_candidates", "research_alternatives"],
            description="Compare all candidates found and recommend the best fit.",
        ),
    ]
}


def report(
    summary: str,
    facts: list[str] = (),  # type: ignore[assignment]
    *,
    success: bool = True,
    error: str | None = None,
    artifacts: list[dict[str, str]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "summary": summary,
        "facts": [{"content": fact} for fact in facts],
        "evidence": evidence
        if evidence is not None
        else [{"source": "model_knowledge", "reference": None, "note": "general knowledge"}],
        "artifacts": artifacts or [],
        "error": error,
    }


AGENT_REPORTS: dict[str, dict[str, Any]] = {
    "research_candidates": report(
        "Found two leading candidates.",
        ["Product A is the market leader.", "Product B is the cheapest option."],
    ),
    "research_alternatives": report(
        "Found one alternative.", ["Product C targets professional users."]
    ),
    "compare_results": report(
        "Product A offers the best balance.",
        ["Product A is recommended overall."],
        artifacts=[
            {
                "name": "comparison.md",
                "media_type": "text/markdown",
                "content": "| Product | Verdict |\n|---|---|\n| A | recommended |",
            }
        ],
        evidence=[
            {"source": "task_context", "reference": "research_candidates", "note": "leader data"},
            {"source": "task_context", "reference": "research_alternatives", "note": "alternative"},
        ],
    ),
}


def agent_reply(
    reports: dict[str, dict[str, Any]] = AGENT_REPORTS,
    gates: dict[str, asyncio.Event] | None = None,
    errors: dict[str, Exception] | None = None,
) -> Any:
    """A reply source answering each agent request by its task id."""

    def reply(request: LLMRequest) -> FakeReply:
        task_id = request.metadata["task_id"]
        if errors and task_id in errors:
            return FakeReply(error=errors[task_id])  # type: ignore[arg-type]
        return FakeReply(data=reports[task_id], wait_for=(gates or {}).get(task_id))

    return reply


def research_provider(**agent_kwargs: Any) -> FakeLLMProvider:
    reply = agent_reply(**agent_kwargs)
    return FakeLLMProvider(
        {
            "planner": FakeReply(data=RESEARCH_PLAN),
            "agent:researcher": reply,
            "agent:analyst": reply,
            "agent:specialist": reply,
        }
    )
