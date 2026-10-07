"""Validates and executes tool calls. The only path to a tool's `execute`.

    ToolCall -> registry lookup -> permission -> POLICY GATE -> argument validation
             -> tool (timeout) -> output validation -> ToolResult

Nothing is executed until the lookup, permission, policy gate and argument validation
all succeed. Every failure becomes a failed `ToolResult` with an `error_type`; nothing
is retried and no exception escapes (except cancellation).

Policy gate (Phase 8, app.policy.engine), decided from the tool's registered category:
- agent tool loops: only ALLOW executes. APPROVAL_REQUIRED -> `approval_required`, DENY
  -> `policy_denied`, both without executing. Agents are only shown ALLOW tools.
- action tasks (agent type "action_executor"): execute only with an `Authorization`
  built by NEXUS from the recorded decision (and granted approval) for exactly this
  task, tool and arguments; the policy is re-evaluated, so a tool denied since is
  still refused.
"""

import asyncio
import logging
import time

from pydantic import BaseModel, ValidationError

from app.events.types import ACTION_AGENT_TYPE, PolicyDecision, PolicyOutcome
from app.llm.schemas import describe_validation_error
from app.policy.engine import ActionRequest, Authorization, PolicyEngine
from app.tools.errors import (
    ApprovalRequiredError,
    InvalidToolArgumentsError,
    InvalidToolOutputError,
    PolicyDeniedError,
    ToolError,
    ToolTimeoutError,
)
from app.tools.policy import ToolPolicy
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolCall, ToolContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: ToolPolicy, engine: PolicyEngine | None = None) -> None:
        self.registry = registry
        self.policy = policy
        self.engine = engine or PolicyEngine()

    def available_tools(self, agent_type: str) -> list[ToolDefinition]:
        """Tools an agent may use: registered, authorized for its type, and allowed by
        the policy engine without approval (the agent loop cannot wait for one)."""
        allowed = self.policy.allowed(agent_type)
        return [
            d for d in self.registry.definitions()
            if d.name in allowed and self._decide(d, "listing", d.name, {}).outcome is PolicyOutcome.ALLOW
        ]

    def _decide(self, definition: ToolDefinition, task_id: str, tool_name: str, arguments: dict[str, object]) -> PolicyDecision:
        return self.engine.evaluate(ActionRequest(task_id=task_id, tool_name=tool_name, arguments=arguments), definition)  # type: ignore[arg-type]

    def _gate(self, definition: ToolDefinition, call: ToolCall, context: ToolContext, authorization: Authorization | None) -> None:
        """Raise unless policy lets this exact call execute now. Runs before anything else
        touches the tool."""
        decision = self.engine.evaluate(
            ActionRequest(task_id=context.task_id, tool_name=call.tool_name, arguments=dict(call.arguments)), definition
        )
        if decision.outcome is PolicyOutcome.DENY:
            raise PolicyDeniedError(f"{call.tool_name}: denied by policy ({decision.rule}): {decision.reason}")
        if context.agent_type == ACTION_AGENT_TYPE:
            if definition.agent_only:
                raise PolicyDeniedError(f"{call.tool_name}: only agents may use this tool, not action tasks")
            if (
                authorization is None
                or authorization.task_id != context.task_id
                or authorization.action_fingerprint != decision.action_fingerprint
                or authorization.decision.tool_name != call.tool_name
            ):
                raise PolicyDeniedError(f"{call.tool_name}: no authorization for this exact action")
            if decision.outcome is PolicyOutcome.APPROVAL_REQUIRED and authorization.approval_id is None:
                raise ApprovalRequiredError(f"{call.tool_name}: requires a granted approval ({decision.rule})")
            return
        self.policy.authorize(context.agent_type, call.tool_name)
        if decision.outcome is PolicyOutcome.APPROVAL_REQUIRED:
            raise ApprovalRequiredError(
                f"{call.tool_name}: requires human approval ({decision.rule}); it can only run as an approved action task"
            )

    async def execute(
        self, call: ToolCall, context: ToolContext, authorization: Authorization | None = None
    ) -> ToolResult:
        started = time.perf_counter()
        definition: ToolDefinition | None = None
        try:
            tool = self.registry.get(call.tool_name)
            definition = tool.definition
            self._gate(definition, call, context, authorization)
            try:
                arguments = definition.input_model.model_validate(call.arguments)
            except ValidationError as exc:
                raise InvalidToolArgumentsError(describe_validation_error(exc)) from exc
            try:
                output = await asyncio.wait_for(
                    tool.execute(arguments, context), definition.timeout_seconds
                )
            except asyncio.TimeoutError:
                raise ToolTimeoutError(
                    f"{call.tool_name} exceeded {definition.timeout_seconds:g}s"
                ) from None
            output = _validate_output(definition, output)
        except ToolError as exc:
            logger.info("tool %s failed (%s): %s", call.tool_name, exc.error_type, exc.message)
            return ToolResult(
                success=False,
                error=exc.message,
                error_type=exc.error_type,
                metadata=_metadata(definition, started),
            )
        except Exception as exc:  # a bug in a tool must not crash the agent loop silently
            logger.exception("tool %s raised unexpectedly", call.tool_name)
            return ToolResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                error_type="execution_error",
                metadata=_metadata(definition, started),
            )
        return ToolResult(
            success=True,
            output=output.model_dump(mode="json"),
            metadata=_metadata(definition, started),
        )


def _validate_output(definition: ToolDefinition, output: object) -> BaseModel:
    try:
        if isinstance(output, definition.output_model):
            return definition.output_model.model_validate(output.model_dump())
        return definition.output_model.model_validate(output)
    except ValidationError as exc:
        raise InvalidToolOutputError(
            f"{definition.name} returned invalid output: {describe_validation_error(exc)}"
        ) from exc


def _metadata(definition: ToolDefinition | None, started: float) -> dict[str, object]:
    metadata: dict[str, object] = {"duration_ms": round((time.perf_counter() - started) * 1000, 1)}
    if definition is not None:
        metadata["fake"] = definition.fake
        metadata["risk_level"] = definition.risk_level
    return metadata  # type: ignore[return-value]
