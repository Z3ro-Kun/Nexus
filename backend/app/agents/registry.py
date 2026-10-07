"""agent_type -> agent implementation.

The registry is the single list of agents NEXUS can use. The planner may only assign tasks
to the task agents listed here, and the runtime only dispatches to them.
"""

from app.agents.base import Agent
from app.agents.planner import PlannerAgent
from app.agents.reasoning import AgentSpec, ReasoningAgent
from app.agents.replanner import ReplannerAgent
from app.core.exceptions import UnknownAgentTypeError
from app.llm.base import LLMProvider
from app.tools.executor import ToolExecutor

TASK_AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        agent_type="researcher",
        role="gathers and organizes relevant information",
        task_types=frozenset({"research"}),
        instructions=(
            "You research a topic: identify relevant options, facts and considerations, "
            "using your tools when you have them, and state uncertainty honestly."
        ),
    ),
    AgentSpec(
        agent_type="analyst",
        role="compares, evaluates and draws conclusions from other tasks' results",
        task_types=frozenset({"analysis"}),
        instructions=(
            "You analyze the results of the dependency tasks in your context: compare, "
            "weigh trade-offs and reach clearly reasoned conclusions. Rely on the provided "
            "results first and cite them as task_context evidence. Use the calculator for "
            "arithmetic when you have it rather than computing in your head."
        ),
    ),
    AgentSpec(
        agent_type="specialist",
        role="applies domain expertise to produce a focused deliverable",
        task_types=frozenset({"domain_task"}),
        instructions=(
            "You apply domain expertise to produce the specific deliverable the task "
            "describes, such as a recommendation, checklist or draft."
        ),
    ),
)

PLANNER_AGENT_TYPE = "planner"
REPLANNER_AGENT_TYPE = "replanner"


class AgentRegistry:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_tokens: int,
        specs: tuple[AgentSpec, ...] = TASK_AGENT_SPECS,
        tool_executor: ToolExecutor | None = None,
        max_tool_calls: int = 0,
        tool_output_max_chars: int = 20_000,
    ) -> None:
        self._specs = {spec.agent_type: spec for spec in specs}
        self.tool_executor = tool_executor if max_tool_calls > 0 else None
        self.max_tool_calls = max_tool_calls
        tools = {
            spec.agent_type: (
                self.tool_executor.available_tools(spec.agent_type) if self.tool_executor else []
            )
            for spec in specs
        }
        self._agents: dict[str, Agent] = {
            spec.agent_type: ReasoningAgent(
                spec,
                provider,
                max_tokens=max_tokens,
                tools=tools[spec.agent_type],
                max_tool_calls=max_tool_calls,
                tool_output_max_chars=tool_output_max_chars,
            )
            for spec in specs
        }
        tools_by_agent = {name: [t.name for t in defs] for name, defs in tools.items()}
        agent_tools = [d for defs in tools.values() for d in defs]
        # Phase 9: side-effecting tools the planner may propose as (policy-gated) actions.
        action_tools = tool_executor.registry.definitions() if tool_executor is not None else []
        self._planner = PlannerAgent(
            provider,
            specs,
            max_tokens=max_tokens,
            tools_by_agent=tools_by_agent,
            action_tools=action_tools,
            max_tool_calls=self.max_tool_calls,
            agent_tools=agent_tools,
        )
        # Like the planner, the replanner only proposes tasks; it never executes one.
        self._replanner = ReplannerAgent(
            provider, specs, max_tokens=max_tokens, tools_by_agent=tools_by_agent, agent_tools=agent_tools
        )

    @property
    def agent_types(self) -> tuple[str, ...]:
        """Every registered agent, including the planner."""
        return (PLANNER_AGENT_TYPE, *sorted(self._agents))

    def task_agent_specs(self) -> tuple[AgentSpec, ...]:
        return tuple(self._specs[name] for name in sorted(self._specs))

    def planner(self) -> PlannerAgent:
        return self._planner

    def replanner(self) -> ReplannerAgent:
        return self._replanner

    def spec(self, agent_type: str) -> AgentSpec:
        self.resolve(agent_type)
        return self._specs[agent_type]

    def resolve(self, agent_type: str | None) -> Agent:
        """The task agent for `agent_type`. The planner and replanner do not execute tasks."""
        if agent_type in (PLANNER_AGENT_TYPE, REPLANNER_AGENT_TYPE):
            raise UnknownAgentTypeError(f"the {agent_type} agent does not execute tasks")
        agent = self._agents.get(agent_type or "")
        if agent is None:
            raise UnknownAgentTypeError(f"unsupported agent type {agent_type!r}")
        return agent
