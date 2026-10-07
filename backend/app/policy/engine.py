"""Deterministic policy engine: (action, tool metadata, policy, run history) -> decision.

No LLM, no I/O. The same inputs always give the same decision.

The risk category comes from the registered tool's metadata (ToolDefinition.category),
never from the request: a model cannot make an irreversible action "read-only" by saying
so. Rules, in order (the first that applies decides):

1. the tool is not registered                         -> DENY       (unknown_tool)
2. the tool is on the deny list                       -> DENY       (denied_tool)
3. the category's outcome is DENY                     -> DENY       (category:<c>)
4. the identical action (tool + arguments) was
   rejected by a human earlier in this run            -> DENY       (previously_rejected)
5. the tool is on the approval list                   -> APPROVAL_REQUIRED (approval_tool)
6. otherwise the category's outcome                   -> ALLOW / APPROVAL_REQUIRED (category:<c>)

Defaults: read_only and network_read ALLOW; reversible_write ALLOW; irreversible
APPROVAL_REQUIRED. read_only is always ALLOW.

Where a decision applies:
- inside an agent's tool loop, only ALLOW executes: an agent cannot wait for or obtain an
  approval mid-task, so APPROVAL_REQUIRED and DENY are refused before execution;
- an action task (a predeclared tool call) is gated before it starts; APPROVAL_REQUIRED
  waits for a human decision (app.policy.manager).
"""

from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app.events.types import ActionCategory, PolicyDecision, PolicyOutcome, action_fingerprint
from app.tools.schemas import ToolDefinition

if TYPE_CHECKING:
    from app.core.config import Settings

DEFAULT_OUTCOMES: dict[ActionCategory, PolicyOutcome] = {
    ActionCategory.READ_ONLY: PolicyOutcome.ALLOW,
    ActionCategory.NETWORK_READ: PolicyOutcome.ALLOW,
    ActionCategory.REVERSIBLE_WRITE: PolicyOutcome.ALLOW,
    ActionCategory.IRREVERSIBLE: PolicyOutcome.APPROVAL_REQUIRED,
}


class PolicyConfig(BaseModel):
    """Deployment policy (NEXUS_POLICY_*). Not writable by agents or models."""

    model_config = ConfigDict(frozen=True)

    outcomes: Mapping[ActionCategory, PolicyOutcome] = Field(default_factory=lambda: dict(DEFAULT_OUTCOMES))
    denied_tools: frozenset[str] = frozenset()
    approval_tools: frozenset[str] = frozenset()

    @field_validator("outcomes")
    @classmethod
    def _complete(cls, value: Mapping[ActionCategory, PolicyOutcome]) -> Mapping[ActionCategory, PolicyOutcome]:
        merged = {**DEFAULT_OUTCOMES, **value}
        merged[ActionCategory.READ_ONLY] = PolicyOutcome.ALLOW  # pure computation is never gated
        return merged


class ActionRequest(BaseModel):
    """An action to decide on. Only the tool name and arguments come from the requester;
    everything about risk is looked up (no field for it exists)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    tool_name: str
    arguments: dict[str, JsonValue]

    @property
    def fingerprint(self) -> str:
        return action_fingerprint(self.tool_name, self.arguments)


class PolicyEngine:
    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def evaluate(
        self,
        request: ActionRequest,
        definition: ToolDefinition | None,
        *,
        rejected_fingerprints: Collection[str] = (),
    ) -> PolicyDecision:
        def decide(outcome: PolicyOutcome, rule: str, reason: str) -> PolicyDecision:
            return PolicyDecision(
                task_id=request.task_id,
                tool_name=request.tool_name,
                action_fingerprint=request.fingerprint,
                category=definition.category if definition else None,
                outcome=outcome,
                rule=rule,
                reason=reason[:1000],
            )

        if definition is None:
            return decide(PolicyOutcome.DENY, "unknown_tool", f"tool {request.tool_name!r} is not registered")
        category = definition.category
        configured = self.config.outcomes[category]
        if request.tool_name in self.config.denied_tools:
            return decide(PolicyOutcome.DENY, "denied_tool", f"tool {request.tool_name!r} is denied by policy")
        if configured is PolicyOutcome.DENY:
            return decide(PolicyOutcome.DENY, f"category:{category.value}", f"{category.value} actions are denied by policy")
        if request.fingerprint in rejected_fingerprints:
            return decide(
                PolicyOutcome.DENY, "previously_rejected",
                "an identical action (same tool and arguments) was rejected earlier in this run",
            )
        if request.tool_name in self.config.approval_tools:
            return decide(PolicyOutcome.APPROVAL_REQUIRED, "approval_tool",
                          f"tool {request.tool_name!r} requires human approval")
        if configured is PolicyOutcome.APPROVAL_REQUIRED:
            return decide(PolicyOutcome.APPROVAL_REQUIRED, f"category:{category.value}",
                          f"{category.value} actions require human approval")
        return decide(PolicyOutcome.ALLOW, f"category:{category.value}", f"{category.value} actions are allowed")


def engine_from_settings(settings: "Settings") -> PolicyEngine:
    return PolicyEngine(
        PolicyConfig(
            outcomes={
                ActionCategory.NETWORK_READ: PolicyOutcome(settings.policy_network_read),
                ActionCategory.REVERSIBLE_WRITE: PolicyOutcome(settings.policy_reversible_write),
                ActionCategory.IRREVERSIBLE: PolicyOutcome(settings.policy_irreversible),
            },
            denied_tools=frozenset(settings.policy_denied_tools),
            approval_tools=frozenset(settings.policy_approval_tools),
        )
    )


class Authorization(BaseModel):
    """Permission to execute one specific action of one action task. Built only by
    NEXUS from recorded state (app.policy.manager.authorization_for): a recorded ALLOW
    decision, or APPROVAL_REQUIRED plus a granted approval. Never built from model output."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    action_fingerprint: str
    decision: PolicyDecision
    approval_id: str | None = None
