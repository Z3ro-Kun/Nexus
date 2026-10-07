"""Minimal supersession: a recovery task's fixed version of a generated project replaces
the version it fixes, instead of becoming a second deliverable.

    build_counter -> counter-app v1 -> verification fails -> replan
        -> fix_reset (depends on build_counter) -> counter-app v2 (supersedes v1)
        -> verification of v2 -> only v2 is ready and downloadable

Real pipeline (HTTP API, scheduler, runtime, artifact_write, verification, recovery,
completion) over SQLite and PostgreSQL, with FakeLLMProvider scripts. The projector's
rules are also checked directly, by replaying events with forged `supersedes` links.
"""

from typing import Any

import pytest

from app.artifacts.workspace import sha256
from app.core.exceptions import InvalidEventError
from app.events.base import Event
from app.events.types import ArtifactCreated
from app.state.projector import apply, project
from tests.agent_fixtures import planned
from tests.orchestration_fixtures import in_order, verifier_says
from tests.test_artifact_integration import harness_factory, provider  # noqa: F401  (fixture)
from tests.test_counter_regression import (
    APP_WITHOUT_RESET,
    FILES,
    GOAL,
    PROJECT,
    context_json,
    faithful_verifier,
    generation_plan,
    verifier_requests,
    write_counter,
)
from tests.tool_fixtures import call_tool, finish

BROKEN = {**FILES, "src/App.tsx": APP_WITHOUT_RESET}


def fix_task(dependencies: list[str] | None = None) -> dict[str, Any]:
    deps = ["build_counter"] if dependencies is None else dependencies
    return {**planned("fix_reset", agent_type="specialist", task_type="domain_task", dependencies=deps,
                      description="Add the missing Reset button and write the complete counter-app again."),
            "replaces": None}


def remediation(task: dict[str, Any]) -> Any:
    return in_order({"strategy_summary": "Fix the counter-app project.", "tasks": [task]})


async def remediated_run(harness_factory: Any, fix_steps: list[dict[str, Any]], task: dict[str, Any] | None = None) -> Any:  # noqa: F811
    h = await harness_factory(provider(
        {"build_counter": write_counter(BROKEN), "fix_reset": fix_steps},
        plan=generation_plan(), verifier=faithful_verifier, replanner=remediation(task or fix_task())))
    run_id, outcome = await h.run(GOAL)
    return h, run_id, outcome


async def state_of(h: Any, run_id: str) -> dict[str, Any]:
    return (await h.client.get(f"/api/v1/runs/{run_id}/state")).json()  # type: ignore[no-any-return]


def created(events: list[Event], artifact_id: str) -> ArtifactCreated:
    [payload] = [e.payload for e in events if isinstance(e.payload, ArtifactCreated) and e.payload.artifact_id == artifact_id]
    return payload


def forge(events: list[Event], artifact_id: str, supersedes: str) -> list[Event]:
    """The log with `supersedes` set on one ArtifactCreated."""
    return [
        e.model_copy(update={"payload": e.payload.model_copy(update={"supersedes": supersedes})})
        if isinstance(e.payload, ArtifactCreated) and e.payload.artifact_id == artifact_id else e
        for e in events
    ]


# --- B: an ordinary project stays the current deliverable -------------------------------------------


async def test_ordinary_project_is_current_and_supersedes_nothing(harness_factory: Any) -> None:  # noqa: F811
    h = await harness_factory(provider({"build_counter": write_counter()}, plan=generation_plan(), verifier=faithful_verifier))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] == "completed"
    artifacts = await h.artifacts(run_id)
    assert {i: (a["status"], a["supersedes"], a["superseded_by"]) for i, a in artifacts.items()} == {
        "build_counter.w1": ("validated", None, None), "build_counter.w1.zip": ("ready", None, None)}
    assert created(await h.events(run_id), "build_counter.w1").supersedes is None
    assert (await h.client.get(artifacts["build_counter.w1.zip"]["download_url"])).status_code == 200


# --- C, F, G, H, I: remediation replaces the broken version --------------------------------------------


async def test_remediation_supersedes_the_broken_version(harness_factory: Any) -> None:  # noqa: F811
    h, run_id, outcome = await remediated_run(harness_factory, write_counter())
    assert outcome["phase"] == "completed", outcome["result"]["completion_blockers"]
    state = await state_of(h, run_id)
    ws = state["workspace_artifacts"]

    # C: v1 (and its archive) superseded by v2; v2 current.
    assert (ws["build_counter.w1"]["status"], ws["build_counter.w1"]["superseded_by"]) == ("superseded", "fix_reset.w1")
    assert (ws["build_counter.w1.zip"]["status"], ws["build_counter.w1.zip"]["superseded_by"]) == ("superseded", "fix_reset.w1")
    assert (ws["fix_reset.w1"]["status"], ws["fix_reset.w1"]["supersedes"], ws["fix_reset.w1"]["name"]) == (
        "validated", "build_counter.w1", PROJECT)
    assert ws["fix_reset.w1.zip"]["status"] == "ready"

    # F: the second verification judged v2 only, and completion delivered only v2.
    first, second = verifier_requests(h.llm)
    assert [a["artifact_id"] for a in context_json(first, "verification_context")["generated_artifacts"]] == ["build_counter.w1"]
    assert [a["artifact_id"] for a in context_json(second, "verification_context")["generated_artifacts"]] == ["fix_reset.w1"]
    checks = {c["check_id"]: c for c in state["verifications"]["verify.objective_attempt2"]["checks"]}
    assert checks["artifacts"]["passed"] and [r["id"] for r in checks["artifacts"]["references"]] == ["fix_reset.w1"]
    events = await h.events(run_id)
    ready = [e.payload.artifact_id for e in events if e.event_type.value == "ArtifactReady"]  # type: ignore[attr-defined]
    assert ready == ["fix_reset.w1.zip"] and events[-1].event_type.value == "RunCompleted"

    # G: the listing shows the current deliverable; history only on request; v1 never downloads.
    current = await h.artifacts(run_id)
    assert sorted(current) == ["fix_reset.w1", "fix_reset.w1.zip"]
    base = f"/api/v1/runs/{run_id}/artifacts"
    history = {a["artifact_id"]: a for a in (await h.client.get(f"{base}?include_superseded=true")).json()}
    assert sorted(history) == ["build_counter.w1", "build_counter.w1.zip", "fix_reset.w1", "fix_reset.w1.zip"]
    assert history["build_counter.w1.zip"]["download_url"] is None and not history["build_counter.w1.zip"]["deliverable"]
    old = await h.client.get(f"{base}/build_counter.w1.zip/download")
    assert old.status_code == 409 and "superseded" in old.json()["error"]["message"]
    new = await h.client.get(current["fix_reset.w1.zip"]["download_url"])
    assert new.status_code == 200 and sha256(new.content) == current["fix_reset.w1.zip"]["sha256"]

    # H: provenance of both versions, and v1 unchanged.
    v2 = created(events, "fix_reset.w1")
    assert (v2.supersedes, history["fix_reset.w1"]["task_id"], history["fix_reset.w1"]["tool_call_ids"]) == (
        "build_counter.w1", "fix_reset", ["fix_reset.t1"])
    v1_recorded = {e.payload.path: e.payload.sha256 for e in events  # type: ignore[attr-defined]
                   if e.event_type.value == "ArtifactFileAdded" and e.payload.artifact_id == "build_counter.w1"}  # type: ignore[attr-defined]
    assert v1_recorded == {p: sha256(c.encode()) for p, c in BROKEN.items()}
    assert {f["path"]: f["sha256"] for f in ws["build_counter.w1"]["files"]} == v1_recorded
    for path, digest in v1_recorded.items():  # the stored bytes still match: nothing was rewritten
        h.workspace.read_file(run_id, "build_counter", "project", PROJECT, path, digest)
    h.workspace.read_package(run_id, "build_counter.w1.zip", ws["build_counter.w1.zip"]["sha256"])

    # I: replaying the log gives exactly the served state, supersession included.
    incremental = None
    for event in events:
        incremental = apply(incremental, event)
    assert project(events).model_dump(mode="json") == incremental.model_dump(mode="json") == state  # type: ignore[union-attr]


# --- D: a failed fix supersedes nothing ---------------------------------------------------------------


async def test_failed_remediation_leaves_the_original_current(harness_factory: Any) -> None:  # noqa: F811
    failing = [call_tool("artifact_write", project=PROJECT, files=[{"path": p, "content": c} for p, c in FILES.items()]),
               finish("Could not finish.", [], success=False, error="gave up")]
    h, run_id, outcome = await remediated_run(harness_factory, failing)
    assert outcome["phase"] == "failed"
    ws = (await state_of(h, run_id))["workspace_artifacts"]
    assert (ws["build_counter.w1"]["status"], ws["build_counter.w1"]["superseded_by"]) == ("validated", None)
    assert ws["build_counter.w1.zip"]["status"] == "validated"
    assert not [i for i in ws if i.startswith("fix_reset")]  # the failed task recorded no artifact
    assert sorted(await h.artifacts(run_id)) == ["build_counter.w1", "build_counter.w1.zip"]


# --- E: same name without the relationship supersedes nothing -------------------------------------------


async def test_independent_same_name_projects_stay_independent(harness_factory: Any) -> None:  # noqa: F811
    plan = {"tasks": [
        planned("build_counter", agent_type="specialist", task_type="domain_task", description="Write counter-app."),
        planned("build_other", agent_type="specialist", task_type="domain_task", description="Write another counter-app."),
    ]}
    h = await harness_factory(provider({"build_counter": write_counter(), "build_other": write_counter(BROKEN)}, plan=plan,
                                       verifier=verifier_says(evidence=["build_counter.w1"])))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] == "completed"
    artifacts = await h.artifacts(run_id)
    assert sorted(i for i, a in artifacts.items() if a["status"] == "ready") == ["build_counter.w1.zip", "build_other.w1.zip"]
    assert all(a["supersedes"] is None and a["superseded_by"] is None for a in artifacts.values())


async def test_planned_dependent_task_does_not_supersede(harness_factory: Any) -> None:  # noqa: F811
    """Not created by recovery: writing the same name after depending on the producer
    gives a second project (planned work), and the projector refuses a forged link."""
    plan = generation_plan(planned("extend", agent_type="specialist", task_type="domain_task", dependencies=["build_counter"],
                                   description="Write counter-app again with a README."))
    h = await harness_factory(provider({"build_counter": write_counter(), "extend": write_counter({**FILES, "README.md": "# c\n"})},
                                       plan=plan, verifier=verifier_says(evidence=["extend.w1"])))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] == "completed"
    events = await h.events(run_id)
    assert created(events, "extend.w1").supersedes is None
    with pytest.raises(InvalidEventError, match="only a task created by recovery"):
        project(forge(events, "extend.w1", "build_counter.w1"))


async def test_recovery_task_without_the_dependency_does_not_supersede(harness_factory: Any) -> None:  # noqa: F811
    h, run_id, outcome = await remediated_run(harness_factory, write_counter(), task=fix_task(dependencies=[]))
    events = await h.events(run_id)
    assert created(events, "fix_reset.w1").supersedes is None
    ws = (await state_of(h, run_id))["workspace_artifacts"]
    assert ws["build_counter.w1"]["status"] != "superseded"
    with pytest.raises(InvalidEventError, match="not made by a direct dependency"):
        project(forge(events, "fix_reset.w1", "build_counter.w1"))


async def test_projector_refuses_invalid_supersession_targets(harness_factory: Any) -> None:  # noqa: F811
    h, run_id, outcome = await remediated_run(harness_factory, write_counter())
    assert outcome["phase"] == "completed"
    events = await h.events(run_id)
    with pytest.raises(InvalidEventError, match="supersedes unknown file or project"):
        project(forge(events, "fix_reset.w1", "nope.w1"))
    with pytest.raises(InvalidEventError, match="supersedes unknown file or project"):
        project(forge(events, "fix_reset.w1", "build_counter.w1.zip"))  # an archive is not a version
    # Same type and name only: a fix under another name cannot replace v1.
    renamed = [e.model_copy(update={"payload": e.payload.model_copy(update={"name": "other-app"})})
               if isinstance(e.payload, ArtifactCreated) and e.payload.artifact_id == "fix_reset.w1" else e for e in events]
    with pytest.raises(InvalidEventError, match="may supersede only a project named 'other-app'"):
        project(renamed)
