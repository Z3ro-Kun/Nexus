"""Deterministic verification: what a checkpoint covers, and every check that code can
decide reliably. Pure functions of projected state; no LLM, no I/O.

The projector calls these too, so a recorded verdict is re-checked against the same
rules no matter who wrote it.

Coverage
--------
A checkpoint covers its dependencies and, transitively, their dependencies. A FAILED
task that was replaced (Phase 5) is covered through its replacement: the replacement's
work is what counts, the failed attempt stays in history. Conflict-resolution tasks are
not covered directly; conflicts are checked through their facts.

Checks (in this order; each yields one VerificationCheck)
--------------------------------------------------------
- tasks_completed: every covered task is COMPLETED.
- provenance: every fact of a covered task has provenance; a tool-derived fact cites a
  successful tool call of the same task, with the same tool name, a source that appears
  in that call's output (or is the call's default source) and the call's fake flag; task
  evidence that cites a tool call cites a successful call of that task.
- conflicts: no relevant conflict (one involving a covered fact, or on a required fact
  key) is OPEN or UNRESOLVED, and no unrecorded disagreement involves a covered fact. A
  RESOLVED conflict passes through its accepted fact; nothing picks a side.
- required_fact[i]: the requirement's key has a usable value (Phase 6 `current_value`:
  accepted, or corroborated by covered facts), tool-derived if required, and the
  value satisfies the requirement's constraint.
- required_artifact[i]: a covered task produced an artifact with that name (and media
  type), and, for json_fields, a JSON object containing every field (not null).
- tool_evidence:<task>: the task (through replacements) has a valid tool-derived fact.
- actions (Phase 8, only when action tasks are covered): every covered action was
  authorized by the policy gate (allowed, or approved by a human) AND executed: its task
  COMPLETED with a successful tool call that is exactly the approved action. Approved is
  not executed, and executed is not verified.
- artifacts (Phase 10, only when covered tasks generated workspace artifacts): every one
  passed deterministic validation, and every project was packaged into an archive. The
  bytes themselves are re-checked against their checksums before delivery (RunCompletion).
"""

import json
from collections.abc import Iterable

from app.events.types import (
    PolicyOutcome,
    ArtifactRequirement,
    CheckKind,
    EvidenceRef,
    FactRequirement,
    ValueConstraint,
    VerificationCheck,
)
from app.state.conflict_detector import (
    canonical_value,
    current_value,
    detect_conflicts,
    display_value,
    normalize,
)
from app.state.models import (
    ConflictState,
    ConflictStatus,
    Fact,
    RunState,
    TaskStatus,
    ToolCallState,
    ToolCallStatus,
)

MAX_LISTED = 20  # items named in one check message


def effective_task(state: RunState, task_id: str) -> str:
    """The task that stands for `task_id`: itself, or the end of its replacement chain."""
    seen = {task_id}
    while (replacement := state.tasks[task_id].replaced_by) is not None and replacement not in seen:
        seen.add(replacement)
        task_id = replacement
    return task_id


def covered_tasks(state: RunState, verification_id: str) -> tuple[str, ...]:
    """Every task the checkpoint covers (effective tasks, transitively), sorted."""
    covered: set[str] = set()
    stack = list(state.tasks[verification_id].dependencies)
    while stack:
        task_id = effective_task(state, stack.pop())
        if task_id in covered or task_id == verification_id:
            continue
        covered.add(task_id)
        stack.extend(state.tasks[task_id].dependencies)
    return tuple(sorted(covered))


def covered_facts(state: RunState, covered: Iterable[str]) -> list[Fact]:
    tasks = set(covered)
    return sorted((f for f in state.facts.values() if f.task_id in tasks), key=lambda f: f.sequence)


def relevant_conflicts(state: RunState, verification_id: str, covered: Iterable[str]) -> list[ConflictState]:
    """Conflicts involving a covered fact, or on a key the checkpoint requires."""
    fact_ids = {f.fact_id for f in covered_facts(state, covered)}
    spec = state.tasks[verification_id].verification
    keys = {(normalize(r.subject), normalize(r.attribute)) for r in (spec.required_facts if spec else [])}
    relevant = [
        c
        for c in state.conflicts.values()
        if fact_ids.intersection(c.fact_ids)
        or (c.fact_key is not None and (c.fact_key.subject, c.fact_key.attribute) in keys)
    ]
    return sorted(relevant, key=lambda c: (c.detected_sequence or 0, c.conflict_id))


def context_refs(state: RunState, verification_id: str) -> dict[str, tuple[str, ...]]:
    """What a verification considers; recorded in VerificationStarted."""
    covered = covered_tasks(state, verification_id)
    tasks = set(covered)
    return {
        "covered_task_ids": covered,
        "fact_ids": tuple(f.fact_id for f in covered_facts(state, covered)),
        "artifact_ids": tuple(
            a.artifact_id for a in sorted(state.artifacts.values(), key=lambda a: a.sequence) if a.task_id in tasks
        ),
        "tool_call_ids": tuple(
            c.tool_call_id for c in sorted(state.tool_calls.values(), key=lambda c: c.sequence) if c.task_id in tasks
        ),
        "conflict_ids": tuple(c.conflict_id for c in relevant_conflicts(state, verification_id, covered)),
    }


def pending_resolution(state: RunState, verification_id: str) -> str | None:
    """A relevant OPEN conflict whose resolution task (or its replacement) has not
    finished yet: the checkpoint should wait for it rather than fail on it. The
    scheduler uses this to defer starting a verification task. None if nothing is
    pending."""
    for conflict in relevant_conflicts(state, verification_id, covered_tasks(state, verification_id)):
        if conflict.status is not ConflictStatus.OPEN or conflict.resolution_task_id is None:
            continue
        resolver = effective_task(state, conflict.resolution_task_id)
        if state.tasks[resolver].status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING):
            return conflict.conflict_id
    return None


# --- provenance ------------------------------------------------------------------------------


def _output_urls(output: object) -> set[str]:
    if not isinstance(output, dict):
        return set()
    urls = {str(output[k]) for k in ("url", "final_url") if isinstance(output.get(k), str)}
    for item in output.get("results") or []:
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            urls.add(item["url"])
    return urls


def _default_source(call: ToolCallState) -> str:
    """Mirrors the runtime's default source for a tool-derived fact without a URL."""
    output = call.output if isinstance(call.output, dict) else {}
    if call.tool_name == "http_fetch" and isinstance(output.get("final_url"), str):
        return str(output["final_url"])
    for key in ("expression", "query", "backend"):
        if isinstance(output.get(key), str):
            return f"{call.tool_name}: {output[key]}"
    return call.tool_name


def provenance_problem(state: RunState, fact: Fact) -> str | None:
    """Why the fact's provenance is not valid, or None if it is."""
    provenance = fact.provenance
    if provenance is None:
        return "no provenance recorded"
    if provenance.kind != "tool_output":
        if provenance.tool_call_id is not None:
            return f"cites tool call {provenance.tool_call_id!r} but its kind is {provenance.kind}"
        return None
    call = state.tool_calls.get(provenance.tool_call_id or "")
    if call is None:
        return f"cites tool call {provenance.tool_call_id!r}, which does not exist"
    if call.status is not ToolCallStatus.SUCCEEDED:
        return f"cites tool call {call.tool_call_id!r}, which {call.status.value}"
    if call.task_id != fact.task_id:
        return f"cites tool call {call.tool_call_id!r} of another task ({call.task_id!r})"
    if provenance.tool_name != call.tool_name:
        return f"names tool {provenance.tool_name!r}, but call {call.tool_call_id!r} used {call.tool_name!r}"
    if provenance.source not in _output_urls(call.output) | {_default_source(call)}:
        return f"source {provenance.source!r} does not appear in the output of {call.tool_call_id!r}"
    if provenance.fake != bool(call.metadata.get("fake", False)):
        return f"fake flag does not match tool call {call.tool_call_id!r}"
    return None


def has_valid_tool_fact(state: RunState, facts: Iterable[Fact]) -> list[Fact]:
    return [
        f for f in facts
        if f.provenance is not None and f.provenance.kind == "tool_output" and provenance_problem(state, f) is None
    ]


# --- checks ----------------------------------------------------------------------------------


def _listed(items: list[str]) -> str:
    shown = "; ".join(items[:MAX_LISTED])
    return shown + (f"; and {len(items) - MAX_LISTED} more" if len(items) > MAX_LISTED else "")


def _check(check_id: str, kind: CheckKind, passed: bool, message: str, refs: Iterable[EvidenceRef] = ()) -> VerificationCheck:
    unique = list(dict.fromkeys(refs))
    return VerificationCheck(check_id=check_id, kind=kind, passed=passed, message=message[:2000], references=unique[:500])


def _ref(kind: str, item_id: str) -> EvidenceRef:
    return EvidenceRef(kind=kind, id=item_id)  # type: ignore[arg-type]


def _tasks_completed(state: RunState, covered: tuple[str, ...]) -> VerificationCheck:
    open_ = [t for t in covered if state.tasks[t].status is not TaskStatus.COMPLETED]
    if open_:
        return _check(
            "tasks_completed", CheckKind.TASKS_COMPLETED, False,
            "covered tasks not completed: " + _listed([f"{t} is {state.tasks[t].status.value}" for t in open_]),
            (_ref("task", t) for t in open_),
        )
    return _check(
        "tasks_completed", CheckKind.TASKS_COMPLETED, True,
        f"all {len(covered)} covered tasks completed", (_ref("task", t) for t in covered),
    )


def _provenance(state: RunState, covered: tuple[str, ...], facts: list[Fact]) -> VerificationCheck:
    problems: list[tuple[EvidenceRef, str]] = []
    for fact in facts:
        problem = provenance_problem(state, fact)
        if problem is not None:
            problems.append((_ref("fact", fact.fact_id), f"fact {fact.fact_id} {problem}"))
    for task_id in covered:
        task = state.tasks[task_id]
        for item in task.evidence:
            if item.source != "tool_output":
                continue
            call = state.tool_calls.get(item.reference or "")
            if call is None or call.status is not ToolCallStatus.SUCCEEDED or call.task_id != task_id:
                problems.append(
                    (_ref("task", task_id), f"task {task_id} cites tool call {item.reference!r}, not a successful call of the task")
                )
    if problems:
        return _check("provenance", CheckKind.PROVENANCE, False,
                      "invalid provenance: " + _listed([p for _, p in problems]), (r for r, _ in problems))
    tool_facts = sum(1 for f in facts if f.provenance is not None and f.provenance.kind == "tool_output")
    return _check(
        "provenance", CheckKind.PROVENANCE, True,
        f"{len(facts)} facts with valid provenance ({tool_facts} tool-derived)",
        (_ref("fact", f.fact_id) for f in facts),
    )


def _conflicts(state: RunState, verification_id: str, covered: tuple[str, ...], facts: list[Fact]) -> VerificationCheck:
    relevant = relevant_conflicts(state, verification_id, covered)
    blocking = [c for c in relevant if c.status is not ConflictStatus.RESOLVED]
    fact_ids = {f.fact_id for f in facts}
    unrecorded = [c for c in detect_conflicts(state) if fact_ids.intersection(c.fact_ids)]
    if blocking or unrecorded:
        lines = [
            f"conflict {c.conflict_id} is {c.status.value}"
            + (f" ({c.fact_key.subject} / {c.fact_key.attribute}: {', '.join(c.fact_ids)})" if c.fact_key else "")
            for c in blocking
        ] + [f"unrecorded disagreement: {c.reason}" for c in unrecorded]
        refs = [r for c in blocking for r in (_ref("conflict", c.conflict_id), *(_ref("fact", f) for f in c.fact_ids))]
        refs += [_ref("fact", f) for c in unrecorded for f in c.fact_ids]
        return _check("conflicts", CheckKind.CONFLICTS, False,
                      "conflicting facts not resolved (no side is chosen): " + _listed(lines), refs)
    if not relevant:
        return _check("conflicts", CheckKind.CONFLICTS, True, "no relevant conflicts")
    refs = [
        r
        for c in relevant
        for r in (
            _ref("conflict", c.conflict_id),
            *([_ref("fact", c.resolved_fact_id)] if c.resolved_fact_id else []),
            *(_ref("fact", f) for f in c.evidence_ids),
        )
    ]
    return _check(
        "conflicts", CheckKind.CONFLICTS, True,
        "relevant conflicts resolved by evidence: "
        + _listed([f"{c.conflict_id} -> {c.resolved_fact_id or c.resolution or 'resolved'}" for c in relevant]),
        refs,
    )


def _compare(value: object, constraint: ValueConstraint) -> bool:
    op = constraint.operator
    if isinstance(constraint.value, str):
        equal = isinstance(value, str) and normalize(value) == normalize(constraint.value)
        return equal if op == "eq" else not equal
    if isinstance(value, str) or isinstance(value, bool):
        return False
    a, b = float(value), float(constraint.value)  # type: ignore[arg-type]
    return {"eq": a == b, "ne": a != b, "lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]


def _required_fact(state: RunState, index: int, req: FactRequirement, facts: list[Fact]) -> VerificationCheck:
    subject, attribute = normalize(req.subject), normalize(req.attribute)
    check_id = f"required_fact[{index}]:{subject}/{attribute}"
    kind = CheckKind.REQUIRED_FACT
    value = current_value(state, req.subject, req.attribute)
    covered_ids = {f.fact_id for f in facts}
    if value.status == "conflicting":
        on_key = [c.conflict_id for c in state.conflicts.values()
                  if c.fact_key is not None and (c.fact_key.subject, c.fact_key.attribute) == (subject, attribute)]
        return _check(check_id, kind, False, f"{subject} / {attribute} is conflicting; no value can be relied on",
                      [*(_ref("conflict", c) for c in on_key), *(_ref("fact", f) for f in value.fact_ids)])
    if value.status == "accepted":
        relied = list(value.fact_ids)
    elif value.status == "corroborated":
        relied = [f for f in value.fact_ids if f in covered_ids]
    else:
        relied = []
    if not relied or value.value is None:
        return _check(check_id, kind, False, f"no fact of the verified work states {subject} / {attribute}")
    refs = [_ref("fact", f) for f in relied]
    shown = f"{subject} / {attribute} = {display_value(value.value)} ({value.status}: {', '.join(relied)})"
    if req.tool_derived and not has_valid_tool_fact(state, (state.facts[f] for f in relied)):
        return _check(check_id, kind, False, f"{shown} is not backed by a valid tool-derived fact", refs)
    constraint = req.constraint
    if constraint is not None:
        claim_unit = canonical_value(value.value)[2]
        wanted_unit = normalize(constraint.unit) if constraint.unit else None
        target = f"{constraint.operator} {constraint.value}{' ' + constraint.unit if constraint.unit else ''}"
        if wanted_unit is not None and claim_unit != wanted_unit:
            return _check(check_id, kind, False, f"{shown}: unit {claim_unit!r} does not match the constraint ({target})", refs)
        if not _compare(value.value.value, constraint):
            return _check(check_id, kind, False, f"{shown} violates the constraint {target}", refs)
        shown += f", satisfies {target}"
    return _check(check_id, kind, True, shown, refs)


def _required_artifact(state: RunState, index: int, req: ArtifactRequirement, covered: tuple[str, ...]) -> VerificationCheck:
    check_id = f"required_artifact[{index}]:{normalize(req.name)}"
    kind = CheckKind.REQUIRED_ARTIFACT
    tasks = set(covered)
    candidates = [
        a for a in sorted(state.artifacts.values(), key=lambda a: a.sequence)
        if a.task_id in tasks and normalize(a.name) == normalize(req.name)
        and (req.media_type is None or a.media_type == req.media_type)
    ]
    if not candidates:
        wanted = f"{req.name!r}" + (f" ({req.media_type})" if req.media_type else "")
        return _check(check_id, kind, False, f"no artifact {wanted} in the verified work")
    if not req.json_fields:
        return _check(check_id, kind, True, f"artifact {candidates[0].artifact_id} ({candidates[0].name})",
                      [_ref("artifact", candidates[0].artifact_id)])
    problems = []
    for artifact in candidates:
        try:
            content = json.loads(artifact.content)
        except json.JSONDecodeError:
            problems.append(f"{artifact.artifact_id} is not valid JSON")
            continue
        if not isinstance(content, dict):
            problems.append(f"{artifact.artifact_id} is not a JSON object")
            continue
        missing = [f for f in req.json_fields if content.get(f) is None]
        if missing:
            problems.append(f"{artifact.artifact_id} lacks {', '.join(missing)}")
            continue
        return _check(check_id, kind, True,
                      f"artifact {artifact.artifact_id} has fields {', '.join(req.json_fields)}",
                      [_ref("artifact", artifact.artifact_id)])
    return _check(check_id, kind, False, "required structured fields missing: " + _listed(problems),
                  (_ref("artifact", a.artifact_id) for a in candidates))


def _tool_evidence(state: RunState, task_id: str) -> VerificationCheck:
    check_id = f"tool_evidence:{task_id}"
    effective = effective_task(state, task_id) if task_id in state.tasks else task_id
    facts = [f for f in state.facts.values() if f.task_id == effective]
    valid = has_valid_tool_fact(state, facts)
    if not valid:
        return _check(check_id, CheckKind.TOOL_EVIDENCE, False,
                      f"task {effective} produced no fact backed by a valid tool call", [_ref("task", effective)])
    return _check(
        check_id, CheckKind.TOOL_EVIDENCE, True,
        f"task {effective}: {len(valid)} tool-derived facts",
        [*(_ref("fact", f.fact_id) for f in valid),
         *(_ref("tool_call", f.provenance.tool_call_id) for f in valid if f.provenance and f.provenance.tool_call_id)],
    )


def _actions(state: RunState, covered: tuple[str, ...]) -> VerificationCheck | None:
    actions = [state.tasks[t] for t in covered if state.tasks[t].action is not None]
    if not actions:
        return None
    problems: list[tuple[str, str]] = []
    refs: list[EvidenceRef] = []
    for task in actions:
        assert task.action is not None
        record = state.policy.get(task.task_id)
        approval = next((a for a in state.approvals.values() if a.task_id == task.task_id), None)
        if record is None:
            problems.append((task.task_id, f"action {task.task_id} was never evaluated by the policy gate"))
            continue
        outcome = record.decision.outcome
        if outcome is PolicyOutcome.DENY:
            problems.append((task.task_id, f"action {task.task_id} was denied ({record.decision.rule})"))
            continue
        if outcome is PolicyOutcome.APPROVAL_REQUIRED and (approval is None or approval.status.value != "granted"):
            state_ = approval.status.value if approval else "not requested"
            problems.append((task.task_id, f"action {task.task_id} requires approval, which is {state_}"))
            continue
        calls = [
            c for c in state.tool_calls.values()
            if c.task_id == task.task_id and c.status is ToolCallStatus.SUCCEEDED
            and (c.tool_name, c.arguments) == (task.action.tool_name, task.action.arguments)
        ]
        if task.status is not TaskStatus.COMPLETED or not calls:
            problems.append((task.task_id, f"action {task.task_id} was authorized but not executed ({task.status.value})"))
            continue
        refs += [_ref("task", task.task_id), _ref("tool_call", calls[0].tool_call_id)]
    if problems:
        return _check("actions", CheckKind.ACTIONS, False, "actions not authorized and executed: " + _listed([p for _, p in problems]),
                      (_ref("task", t) for t, _ in problems))
    return _check("actions", CheckKind.ACTIONS, True,
                  f"{len(actions)} actions authorized by the policy gate and executed as approved", refs)


def _artifacts(state: RunState, covered: tuple[str, ...]) -> VerificationCheck | None:
    tasks = set(covered)
    generated = [
        a for a in sorted(state.workspace_artifacts.values(), key=lambda a: a.sequence)
        # A superseded version (replaced by a recovery task's fix) is history, not a deliverable.
        if a.task_id in tasks and a.artifact_type != "archive" and a.status != "superseded"
    ]
    if not generated:
        return None
    problems: list[tuple[str, str]] = []
    for a in generated:
        if a.status not in ("validated", "ready"):
            problems.append((a.artifact_id, f"{a.name} is {a.status}"))
        elif a.artifact_type == "project":
            archive = state.workspace_artifacts.get(a.archive_id or "")
            if archive is None or archive.status not in ("validated", "ready"):
                problems.append((a.artifact_id, f"{a.name} has no valid archive"))
    if problems:
        return _check("artifacts", CheckKind.ARTIFACTS, False, "generated artifacts not deliverable: " + _listed([p for _, p in problems]),
                      (_ref("artifact", i) for i, _ in problems))
    files = sum(len(a.files) for a in generated)
    return _check("artifacts", CheckKind.ARTIFACTS, True,
                  f"{len(generated)} generated artifacts ({files} files) validated; projects packaged",
                  (_ref("artifact", a.artifact_id) for a in generated))


def run_checks(state: RunState, verification_id: str) -> tuple[VerificationCheck, ...]:
    """Every deterministic check of the checkpoint, against `state`. Pure."""
    spec = state.tasks[verification_id].verification
    assert spec is not None
    covered = covered_tasks(state, verification_id)
    facts = covered_facts(state, covered)
    return (
        _tasks_completed(state, covered),
        _provenance(state, covered, facts),
        _conflicts(state, verification_id, covered, facts),
        *(_required_fact(state, i, req, facts) for i, req in enumerate(spec.required_facts)),
        *(_required_artifact(state, i, req, covered) for i, req in enumerate(spec.required_artifacts)),
        *(_tool_evidence(state, t) for t in spec.tool_evidence_tasks),
        *(c for c in (_actions(state, covered),) if c is not None),
        *(c for c in (_artifacts(state, covered),) if c is not None),
    )


def failed_references(checks: Iterable[VerificationCheck]) -> tuple[EvidenceRef, ...]:
    return tuple(dict.fromkeys(r for c in checks if not c.passed for r in c.references))


def describe_failures(checks: Iterable[VerificationCheck]) -> str:
    return "; ".join(f"{c.check_id}: {c.message}" for c in checks if not c.passed)
