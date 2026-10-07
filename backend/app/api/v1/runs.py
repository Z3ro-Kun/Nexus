"""Minimal run/event endpoints for exercising the event-sourced state end to end."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentTaskExecutor
from app.core.config import Settings
from app.llm.base import LLMProvider
from app.tools.executor import ToolExecutor
from app.tools.factory import build_tool_policy
from app.core.dependencies import (
    DatabaseDep,
    LLMProviderDep,
    SessionDep,
    SettingsDep,
    get_llm_provider,
)
from app.events.base import Event
from app.events.factory import new_event
from app.orchestration.scheduler import ScheduleReport, Scheduler
from app.conflicts.manager import ConflictManager
from app.recovery.manager import RecoveryManager
from app.recovery.policy import RecoveryPolicy
from app.orchestration.task_executor import (
    Outcome,
    Script,
    ScriptedTaskExecutor,
    TaskExecutor,
)
from app.schemas.runs import (
    AppendEventsRequest,
    CreateTasksRequest,
    PlanResponse,
    RunCreate,
    RunRead,
    RunVerification,
    ScheduleRequest,
    TaskRead,
    VerificationRequest,
)
from app.events.types import VerificationSpec
from app.verification.checkpoint import checkpoint_task, run_verified
from app.verification.manager import VerificationManager
from app.verification.semantic import LLMSemanticVerifier
from app.verification.verifier import Verifier
from app.events.authority import check_external_append
from app.policy.actions import ActionTaskExecutor
from app.policy.completion import RunCompletion
from app.policy.engine import engine_from_settings
from app.policy.manager import ApprovalManager, PolicyManager
from app.policy.rules import action_status
from app.schemas.runs import ApprovalContextItem, ApprovalDecisionRequest, ApprovalView, ExecuteRequest
from app.orchestration.orchestrator import OrchestrationResult, Orchestrator
from app.orchestration.result import FinalResult, build_final_result
from app.persistence.database import Database
from app.state.models import ApprovalState
from app.services.planning import PlanningService
from app.services.runs import RunService
from app.state.context_builder import ContextField, build_context
from app.state.models import RunState

router = APIRouter(prefix="/runs", tags=["runs"])


def get_run_service(session: SessionDep) -> RunService:
    return RunService(session)


ServiceDep = Annotated[RunService, Depends(get_run_service)]


@router.post("", response_model=RunRead, status_code=status.HTTP_201_CREATED)
async def create_run(body: RunCreate, service: ServiceDep) -> RunRead:
    return RunRead.model_validate(await service.create_run(body.goal, body.constraints))


@router.get("/{run_id}", response_model=RunRead)
async def get_run(run_id: UUID, service: ServiceDep) -> RunRead:
    return RunRead.model_validate(await service.get_run(run_id))


@router.get("/{run_id}/events", response_model=list[Event])
async def list_events(
    run_id: UUID,
    service: ServiceDep,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
) -> list[Event]:
    return await service.get_events(run_id, after_sequence=after_sequence)


@router.post(
    "/{run_id}/events", response_model=list[Event], status_code=status.HTTP_201_CREATED
)
async def append_events(
    run_id: UUID, body: AppendEventsRequest, service: ServiceDep
) -> list[Event]:
    # Phase 8: privileged event types (policy, approvals, verification, completion) are
    # written only by their NEXUS managers, never through this endpoint.
    check_external_append(item.event_type for item in body.events)
    drafts = [
        new_event(item.event_type, item.payload, agent_id=item.agent_id, task_id=item.task_id)
        for item in body.events
    ]
    return await service.append_events(run_id, drafts, expected_sequence=body.expected_sequence)


@router.get("/{run_id}/state", response_model=RunState)
async def get_state(run_id: UUID, service: ServiceDep) -> RunState:
    return await service.get_state(run_id)


@router.get("/{run_id}/context")
async def get_context(
    run_id: UUID,
    service: ServiceDep,
    fields: Annotated[list[ContextField], Query(min_length=1)],
) -> dict[str, Any]:
    return build_context(await service.get_state(run_id), fields)


@router.post(
    "/{run_id}/tasks", response_model=list[TaskRead], status_code=status.HTTP_201_CREATED
)
async def create_tasks(
    run_id: UUID, body: CreateTasksRequest, service: ServiceDep
) -> list[TaskRead]:
    tasks = await service.create_tasks(run_id, body.tasks)
    return [TaskRead.of(run_id, task) for task in tasks]


@router.get("/{run_id}/tasks", response_model=list[TaskRead])
async def list_tasks(run_id: UUID, service: ServiceDep) -> list[TaskRead]:
    state = await service.get_state(run_id)
    return [TaskRead.of(run_id, task) for task in state.tasks.values()]


@router.get("/{run_id}/tasks/{task_id}", response_model=TaskRead)
async def get_task(run_id: UUID, task_id: str, service: ServiceDep) -> TaskRead:
    return TaskRead.of(run_id, await service.get_task(run_id, task_id))


@router.post(
    "/{run_id}/plan", response_model=PlanResponse, status_code=status.HTTP_201_CREATED
)
async def plan(
    run_id: UUID,
    request: Request,
    session: SessionDep,
    provider: LLMProviderDep,
    settings: SettingsDep,
) -> PlanResponse:
    """Ask the planner for a task graph, validate it, and record it as TaskCreated events.

    Nothing is executed; call /schedule afterwards. If the planner finds the objective not
    executable as stated, no tasks are created: `clarification` says what is missing and
    the run is needs_clarification."""
    registry = _agent_registry(request, provider, settings)
    result = await PlanningService(
        session,
        registry.planner(),
        max_tasks=settings.max_planned_tasks,
        timeout_seconds=settings.agent_timeout_seconds,
    ).plan_run(run_id)
    return PlanResponse(
        tasks=[TaskRead.of(run_id, task) for task in result.tasks],
        provider=result.provider,
        model=result.model,
        clarification=result.clarification,
    )


@router.post("/{run_id}/schedule", response_model=ScheduleReport)
async def schedule(
    run_id: UUID,
    request: Request,
    database: DatabaseDep,
    settings: SettingsDep,
    body: ScheduleRequest | None = None,
) -> ScheduleReport:
    """Run the deterministic scheduler until nothing is runnable (see ScheduleRequest).

    With the agent executor and recovery enabled, eligible task failures are replanned
    (bounded by NEXUS_MAX_REPLANS_PER_RUN); the run is failed when the budget is spent."""
    body = body or ScheduleRequest()
    scheduler, _ = _build_scheduler(request, database, settings, body)
    return await scheduler.run(run_id)


def _build_scheduler(
    request: Request, database: Database, settings: Settings, body: ScheduleRequest
) -> tuple[Scheduler, AgentRegistry | None]:
    """The scheduler with every Phase 5-8 handler, as configured. Shared by /schedule
    and /execute, so both run exactly the same machinery."""
    executor: TaskExecutor
    recovery: RecoveryManager | None = None
    provider: LLMProvider | None = None
    registry: AgentRegistry | None = None
    if body.executor == "scripted":
        executor = ScriptedTaskExecutor(
            {task_id: Script(outcome=Outcome(o)) for task_id, o in body.outcomes.items()}
        )
    else:
        provider = get_llm_provider(request)
        registry = _agent_registry(request, provider, settings)
        executor = AgentTaskExecutor(registry, timeout_seconds=settings.agent_timeout_seconds)
        if body.recovery and settings.max_replans_per_run > 0:
            recovery = RecoveryManager(
                database,
                registry.replanner(),
                RecoveryPolicy(settings.max_replans_per_run),
                max_new_tasks=settings.max_planned_tasks,
                timeout_seconds=settings.agent_timeout_seconds,
            )
    conflicts = (
        ConflictManager(database, max_resolution_tasks=settings.max_conflict_resolutions_per_run)
        if body.conflicts
        else None
    )
    tool_executor = _tool_executor(request, settings)
    policy = PolicyManager(database, tool_executor.engine, ActionTaskExecutor(tool_executor))
    workspace = getattr(request.app.state, "artifact_workspace", None)
    completion = RunCompletion(database, workspace) if body.complete else None
    verification = None
    if body.verification:
        # Deterministic checks always; the LLM verifier only where an LLM is in use.
        semantic = LLMSemanticVerifier(provider, max_tokens=settings.llm_max_tokens) if provider else None
        verification = VerificationManager(
            database, Verifier(semantic), timeout_seconds=settings.agent_timeout_seconds, workspace=workspace
        )
    return Scheduler(database, executor, recovery, conflicts, verification, policy, completion), registry


@router.post("/{run_id}/execute", response_model=OrchestrationResult)
async def execute(
    run_id: UUID,
    request: Request,
    database: DatabaseDep,
    settings: SettingsDep,
    body: ExecuteRequest | None = None,
) -> OrchestrationResult:
    """Carry the run toward verified completion (Phase 9): plan once (with a final
    verification checkpoint), then schedule until the run completes, fails, waits for a
    human approval, or cannot progress. Safe to call again: it resumes the same run and
    never repeats completed work."""
    body = body or ExecuteRequest()
    scheduler, registry = _build_scheduler(
        request, database, settings, ScheduleRequest(executor=body.executor, outcomes=body.outcomes)
    )
    planner = registry.planner() if registry is not None else None
    if planner is None and request.app.state.llm_provider is not None:
        planner = _agent_registry(request, request.app.state.llm_provider, settings).planner()
    return await Orchestrator(
        database,
        scheduler,
        planner,
        max_tasks=settings.max_planned_tasks,
        timeout_seconds=settings.agent_timeout_seconds,
        semantic_verification=settings.verification_semantic and registry is not None,
        max_passes=settings.max_orchestration_passes,
    ).run(run_id)


@router.get("/{run_id}/result", response_model=FinalResult)
async def get_result(run_id: UUID, service: ServiceDep) -> FinalResult:
    """The run's structured result, derived from its current state."""
    return build_final_result(await service.get_state(run_id))


@router.get("/{run_id}/approvals", response_model=list[ApprovalView])
async def list_approvals(run_id: UUID, service: ServiceDep) -> list[ApprovalView]:
    """Every approval of the run (pending first), with what a human needs to decide."""
    state = await service.get_state(run_id)
    views = []
    for approval in sorted(state.approvals.values(), key=lambda a: (a.status.value != "pending", a.requested_sequence)):
        task = state.tasks[approval.task_id]
        views.append(
            ApprovalView(
                approval=approval,
                task_title=task.title,
                task_description=task.description,
                task_status=task.status.value,
                action_status=action_status(state, task.task_id),
                decision=state.policy[task.task_id].decision,
                context=[
                    ApprovalContextItem(
                        task_id=dep, title=state.tasks[dep].title, status=state.tasks[dep].status.value,
                        summary=state.tasks[dep].summary,
                        facts=[f.content for f in state.facts.values() if f.task_id == dep][:10],
                    )
                    for dep in task.dependencies
                ],
            )
        )
    return views


@router.post("/{run_id}/approvals/{approval_id}/approve", response_model=ApprovalState)
async def approve(run_id: UUID, approval_id: str, database: DatabaseDep, body: ApprovalDecisionRequest | None = None) -> ApprovalState:
    """Record a human approval. Executes nothing: the next /schedule runs the action."""
    body = body or ApprovalDecisionRequest()
    return await ApprovalManager(database).decide(run_id, approval_id, granted=True, actor=body.actor, reason=body.reason)


@router.post("/{run_id}/approvals/{approval_id}/reject", response_model=ApprovalState)
async def reject(run_id: UUID, approval_id: str, database: DatabaseDep, body: ApprovalDecisionRequest | None = None) -> ApprovalState:
    """Record a human rejection. The action never executes; its task fails at the gate."""
    body = body or ApprovalDecisionRequest()
    return await ApprovalManager(database).decide(run_id, approval_id, granted=False, actor=body.actor, reason=body.reason)


@router.post("/{run_id}/verification", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_checkpoint(run_id: UUID, body: VerificationRequest, service: ServiceDep) -> TaskRead:
    """Add a verification checkpoint over the given tasks (default: every current work
    task). Nothing runs now; /schedule runs it once its dependencies have completed."""
    state = await service.get_state(run_id)
    spec = VerificationSpec(
        objective=body.objective or state.goal[:2000],
        required_facts=body.required_facts,
        required_artifacts=body.required_artifacts,
        tool_evidence_tasks=body.tool_evidence_tasks,
        semantic=body.semantic,
    )
    task = checkpoint_task(state, task_id=body.task_id, spec=spec, dependencies=body.dependencies, title=body.title)
    [created] = await service.create_tasks(run_id, [task], agent_id="verification_api", expected_sequence=state.last_sequence)
    return TaskRead.of(run_id, created)


@router.get("/{run_id}/verification", response_model=RunVerification)
async def get_verification(run_id: UUID, service: ServiceDep) -> RunVerification:
    state = await service.get_state(run_id)
    return RunVerification(
        verified=run_verified(state),
        verifications=sorted(state.verifications.values(), key=lambda v: (v.checkpoint_id, v.attempt)),
    )


def _tool_executor(request: Request, settings: Settings) -> ToolExecutor:
    """The app's tools behind the tool permissions and the policy gate (Phase 8)."""
    return ToolExecutor(request.app.state.tool_registry, build_tool_policy(settings), engine_from_settings(settings))


def _agent_registry(request: Request, provider: LLMProvider, settings: Settings) -> AgentRegistry:
    """Agents with access to the app's tool registry, under the configured tool policy."""
    return AgentRegistry(
        provider,
        max_tokens=settings.llm_max_tokens,
        tool_executor=_tool_executor(request, settings),
        max_tool_calls=settings.max_tool_calls_per_task,
        tool_output_max_chars=settings.tool_output_max_chars,
    )
