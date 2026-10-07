"""Regression for the real React counter run (run 8d086599): generation without a packaging
task, verification from the actual generated source, scoped dependency file context, and
remediation that fixes the deliverable instead of writing "evidence" about it.

The planner, agents, replanner and verifier are FakeLLMProvider scripts. The scripted
verifier below is "faithful": it judges only from the generated file content NEXUS puts
into its context, which is what these tests establish is now possible. The rest is the
real pipeline (HTTP API, scheduler, agent runtime, artifact_write, verification, recovery,
completion) over a real database and a temporary artifact workspace.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.agents.planner import PlannerAgent
from app.agents.reasoning import AGENT_RULES
from app.artifacts.content import BUDGET_USED, INTEGRITY, NO_WORKSPACE, NOT_TEXT, read_texts
from app.artifacts.workspace import ArtifactWorkspace, sha256
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.schemas import LLMRequest
from app.state.models import ArtifactFile, WorkspaceArtifact
from app.tools.artifact_write import ArtifactWriteTool
from app.verification.semantic import VERIFIER_SYSTEM
from tests.agent_fixtures import planned
from tests.orchestration_fixtures import in_order, verifier_says
from tests.test_artifact_integration import harness_factory, provider  # noqa: F401  (fixture)
from tests.tool_fixtures import call_tool, finish

GOAL = ("Create a small React + TypeScript counter app with increment, decrement and reset buttons, "
        "display the current count, and provide it as a project ZIP.")
PROJECT = "counter-app"
APP = """import { useState } from 'react'

export default function App() {
  const [count, setCount] = useState<number>(0)
  return (
    <main>
      <p className="count">Count: {count}</p>
      <button onClick={() => setCount((c) => c + 1)}>Increment</button>
      <button onClick={() => setCount((c) => c - 1)}>Decrement</button>
      <button onClick={() => setCount(0)}>Reset</button>
    </main>
  )
}
"""
APP_WITHOUT_RESET = APP.replace("      <button onClick={() => setCount(0)}>Reset</button>\n", "")
FILES = {
    "package.json": '{\n  "name": "counter-app",\n  "private": true,\n  "scripts": {"dev": "vite"},\n'
                    '  "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},\n'
                    '  "devDependencies": {"@vitejs/plugin-react": "^4.3.1", "typescript": "^5.5.0", "vite": "^5.4.0"}\n}\n',
    "index.html": '<!doctype html>\n<html><body><div id="root"></div>'
                  '<script type="module" src="/src/main.tsx"></script></body></html>\n',
    "src/main.tsx": "import React from 'react'\nimport ReactDOM from 'react-dom/client'\nimport App from './App'\n"
                    "import './index.css'\n\nReactDOM.createRoot(document.getElementById('root')!).render(<App />)\n",
    "src/App.tsx": APP,
    "src/index.css": "body { font-family: system-ui, sans-serif; }\n",
}
# What recovery wrote in the real run: a description that quotes code found in no file.
FABRICATED_EVIDENCE = ("# App Component Buttons Evidence\n\n- Increment: `onClick={() => setCount(count + 1)}`\n"
                       "- Decrement: `onClick={() => setCount(count - 1)}`\n- Reset: `onClick={() => setCount(0)}`\n"
                       "- Count display: `{count}`\n")


def write_counter(files: dict[str, str] = FILES, **report: Any) -> list[dict[str, Any]]:
    return [call_tool("artifact_write", project=PROJECT, files=[{"path": p, "content": c} for p, c in files.items()]),
            finish(f"Wrote {PROJECT}: Vite config, entry point and a counter App component.", [], **report)]


def generation_plan(*extra: dict[str, Any]) -> dict[str, Any]:
    return {"tasks": [
        planned("build_counter", agent_type="specialist", task_type="domain_task",
                description="Write the complete counter-app project with artifact_write."),
        *extra,
    ]}


def context_json(request: LLMRequest, tag: str) -> dict[str, Any]:
    match = re.search(rf"<{tag}>\n(.*)\n</{tag}>", request.messages[0].content, re.S)
    assert match, f"no <{tag}> block"
    return json.loads(match.group(1))  # type: ignore[no-any-return]


def judge_app(ctx: dict[str, Any]) -> tuple[bool, str | None, dict[str, bool]]:
    """What the latest generated src/App.tsx (as shown to the verifier) actually supports."""
    for artifact in reversed(ctx["generated_artifacts"]):
        for f in artifact["files"]:
            if f["path"] == "src/App.tsx" and f["content"] is not None:
                source = f["content"]
                found = {
                    "count displayed": "{count}" in source,
                    "increment": "+ 1" in source,
                    "decrement": "- 1" in source,
                    "reset": "setCount(0)" in source,
                }
                return all(found.values()), artifact["artifact_id"], found
    return False, None, {}


def faithful_verifier(request: LLMRequest) -> FakeReply:
    """Passes only if the verified App.tsx content shows every required behaviour; any
    agent-written description of the code is ignored, as VERIFIER_SYSTEM instructs."""
    ok, artifact_id, found = judge_app(context_json(request, "verification_context"))
    if ok:
        return FakeReply(data=verifier_says("pass", evidence=[artifact_id], explanation="App.tsx implements all four."))  # type: ignore[list-item]
    missing = ", ".join(k for k, v in found.items() if not v) or "App.tsx content"
    return FakeReply(data=verifier_says("fail", evidence=[artifact_id] if artifact_id else [],
                                        explanation=f"App.tsx does not show: {missing}."))


def verifier_requests(llm: FakeLLMProvider) -> list[LLMRequest]:
    return [r for r in llm.requests if r.purpose == "verifier"]


def agent_requests(llm: FakeLLMProvider, task_id: str) -> list[LLMRequest]:
    return [r for r in llm.requests if r.purpose.startswith("agent:") and r.metadata.get("task_id") == task_id]


# --- A, B: the planner is told how deliverable files are packaged ----------------------------


async def test_planner_sees_artifact_write_packaging_behaviour(tmp_path: Path) -> None:
    definition = ArtifactWriteTool(ArtifactWorkspace(tmp_path)).definition
    llm = FakeLLMProvider({"planner": FakeReply(data=generation_plan())})
    from app.agents.registry import TASK_AGENT_SPECS

    planner = PlannerAgent(llm, TASK_AGENT_SPECS, max_tokens=1000, max_tool_calls=4,
                           tools_by_agent={"specialist": ["artifact_write"]}, agent_tools=[definition, definition])
    plan = await planner.plan(GOAL, [], max_tasks=8)

    system = llm.requests[0].system
    assert "tools: artifact_write" in system  # still per agent
    assert system.count("- artifact_write: ") == 1  # described once, with the planning note
    for phrase in ("Packaging is automatic", "packages it as a ZIP",
                   "Never plan a task whose purpose is to package, zip, bundle, collect or copy generated files",
                   "Prefer ONE task that writes the whole project",
                   "depend explicitly on the task whose files it builds on",
                   "Independent parallel tasks must never write the same project or overlapping files"):
        assert phrase in system, phrase
    assert "counter" not in system.lower() and "react" not in system.lower()  # nothing run-specific
    # B: the plan this prompt asks for: generation only, no packaging task.
    assert [t.id for t in plan.tasks] == ["build_counter"]


async def test_planner_without_agent_tools_has_no_tool_section() -> None:
    from app.agents.registry import TASK_AGENT_SPECS

    llm = FakeLLMProvider({"planner": FakeReply(data=generation_plan())})
    await PlannerAgent(llm, TASK_AGENT_SPECS, max_tokens=1000).plan(GOAL, [], max_tasks=8)
    assert "Agent tools" not in llm.requests[0].system


# --- C: written files are not repeated inline ---------------------------------------------------


def test_agent_rules_make_written_files_the_deliverable() -> None:
    assert "canonical deliverable" in AGENT_RULES
    assert "Do not reproduce the full contents of written files" in AGENT_RULES
    assert "unless the task explicitly asks for the source to be returned as text" in AGENT_RULES


async def test_inline_copy_of_a_written_file_is_not_recorded(harness_factory: Any) -> None:  # noqa: F811
    inline = [
        {"name": "App.tsx", "media_type": "text/plain", "content": f"Here is the app:\n```tsx\n{APP}```\n"},
        {"name": "notes.md", "media_type": "text/markdown", "content": "# Notes\n\nRun `npm install` then `npm run dev`."},
    ]
    h = await harness_factory(provider({"build_counter": write_counter(artifacts=inline)}, plan=generation_plan(),
                                       verifier=faithful_verifier))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] == "completed"
    events = await h.events(run_id)
    added = [e.payload.name for e in events if e.event_type.value == "ArtifactAdded"]  # type: ignore[attr-defined]
    assert added == ["notes.md"]  # the legitimate inline text is kept
    [done] = [e for e in events if e.event_type.value == "TaskCompleted" and e.task_id == "build_counter"]
    assert done.payload.metadata["inline_duplicates_omitted"] == ["App.tsx"]  # type: ignore[attr-defined]


# --- D, E, I: the verifier reads the actual App.tsx -----------------------------------------------


async def test_verifier_inspects_verified_app_source_and_passes_without_evidence_tasks(harness_factory: Any) -> None:  # noqa: F811
    h = await harness_factory(provider({"build_counter": write_counter()}, plan=generation_plan(), verifier=faithful_verifier,
                                       replanner=in_order({"strategy_summary": "unused", "tasks": []})))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] == "completed", outcome["result"]["completion_blockers"]

    [request] = verifier_requests(h.llm)
    ctx = context_json(request, "verification_context")
    [generated] = ctx["generated_artifacts"]
    files = {f["path"]: f for f in generated["files"]}
    app = files["src/App.tsx"]
    assert (generated["artifact_id"], generated["archive_name"]) == ("build_counter.w1", "counter-app.zip")
    assert app["content"] == APP and app["content_truncated"] is False and app["content_note"] is None
    assert app["sha256"] == sha256(APP.encode()) and app["size"] == len(APP.encode())
    assert judge_app(ctx)[2] == {"count displayed": True, "increment": True, "decrement": True, "reset": True}
    assert "the content wins" in request.system and "not that it was run" in request.system

    # I: one generation task, one verification, no recovery, no evidence artifact, one ZIP.
    events = await h.events(run_id)
    kinds = [e.event_type.value for e in events]
    assert "ReplanTriggered" not in kinds and "ArtifactAdded" not in kinds
    assert [t for t in (await h.client.get(f"/api/v1/runs/{run_id}/state")).json()["tasks"]] == ["build_counter", "verify.objective"]
    ready = [a for a in (await h.artifacts(run_id)).values() if a["status"] == "ready"]
    assert [(a["name"], a["artifact_type"]) for a in ready] == [("counter-app.zip", "archive")]


async def test_verifier_fails_when_the_source_lacks_a_button(harness_factory: Any) -> None:  # noqa: F811
    broken = {**FILES, "src/App.tsx": APP_WITHOUT_RESET}
    h = await harness_factory(provider({"build_counter": write_counter(broken)}, plan=generation_plan(),
                                       verifier=faithful_verifier, replanner=in_order({"strategy_summary": "none", "tasks": []})))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] != "completed"
    state = (await h.client.get(f"/api/v1/runs/{run_id}/state")).json()
    semantic = state["verifications"]["verify.objective"]["semantic"]
    assert semantic["passed"] is False and "reset" in semantic["objective"]["explanation"]


# --- F: a description of the code cannot outweigh the code ------------------------------------------


async def test_fabricated_evidence_cannot_make_verification_pass(harness_factory: Any) -> None:  # noqa: F811
    broken = {**FILES, "src/App.tsx": APP_WITHOUT_RESET}
    describe = [call_tool("artifact_write", project=None, files=[{"path": "buttons_evidence.md", "content": FABRICATED_EVIDENCE}]),
                finish("Wrote buttons_evidence.md.", [], artifacts=[
                    {"name": "buttons_evidence.md", "media_type": "text/markdown", "content": FABRICATED_EVIDENCE}])]
    plan = generation_plan(planned("evidence_app_buttons", agent_type="specialist", task_type="domain_task",
                                   dependencies=["build_counter"], description="Summarize the App buttons."))
    h = await harness_factory(provider({"build_counter": write_counter(broken), "evidence_app_buttons": describe}, plan=plan,
                                       verifier=faithful_verifier, replanner=in_order({"strategy_summary": "none", "tasks": []})))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] != "completed"

    [request] = verifier_requests(h.llm)
    ctx = context_json(request, "verification_context")
    shown = {(a["artifact_id"], f["path"]): f["content"] for a in ctx["generated_artifacts"] for f in a["files"]}
    # Both are in front of the verifier: the real source and the claim about it.
    assert shown[("build_counter.w1", "src/App.tsx")] == APP_WITHOUT_RESET
    assert shown[("evidence_app_buttons.w1", "buttons_evidence.md")] == FABRICATED_EVIDENCE
    assert "setCount(count + 1)" not in APP_WITHOUT_RESET  # the evidence quotes code that does not exist
    assert "A description of a generated file" in request.system and "the content wins" in request.system
    state = (await h.client.get(f"/api/v1/runs/{run_id}/state")).json()
    assert state["verifications"]["verify.objective"]["semantic"]["passed"] is False
    assert not [a for a in (await h.artifacts(run_id)).values() if a["status"] == "ready"]


# --- G, H: dependency file context is scoped to declared dependencies --------------------------------


async def test_dependent_task_receives_files_and_siblings_do_not(harness_factory: Any) -> None:  # noqa: F811
    extended = {**FILES, "README.md": "# counter-app\n\n`npm install && npm run dev`\n"}
    plan = generation_plan(
        planned("add_readme", agent_type="specialist", task_type="domain_task", dependencies=["build_counter"],
                description="Add a README to counter-app, keeping every existing file."),
        planned("sibling", agent_type="analyst", task_type="analysis", description="Unrelated note."),
        planned("after_readme", agent_type="analyst", task_type="analysis", dependencies=["add_readme"],
                description="Summarize the README task."),
    )
    h = await harness_factory(provider(
        {"build_counter": write_counter(), "add_readme": write_counter(extended),
         "sibling": [finish("A note.", [])], "after_readme": [finish("Summary.", [])]},
        plan=plan, verifier=verifier_says(evidence=["add_readme.w1"])))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] == "completed", outcome["result"]["completion_blockers"]

    # G: the dependent task gets build_counter's manifest, checksums and verified text.
    first = agent_requests(h.llm, "add_readme")[0]
    [dep] = context_json(first, "task_context")["dependency_results"]
    [generated] = dep["generated_artifacts"]
    assert (dep["task_id"], generated["artifact_id"], generated["name"], generated["artifact_type"]) == (
        "build_counter", "build_counter.w1", PROJECT, "project")
    files = {f["path"]: f for f in generated["files"]}
    assert sorted(files) == sorted(FILES)
    assert all(files[p]["content"] == c and files[p]["sha256"] == sha256(c.encode()) for p, c in FILES.items())
    assert "Build on these files rather than recreating them" in first.system

    # H: an independent sibling sees no generated files; nor does a task two steps away
    # (it depends on add_readme's task, whose files it gets, but never on build_counter's).
    for request in agent_requests(h.llm, "sibling"):
        assert "generated_artifacts" not in request.messages[0].content or all(
            not d["generated_artifacts"] for d in context_json(request, "task_context")["dependency_results"])
        assert "setCount" not in request.messages[0].content
    after = context_json(agent_requests(h.llm, "after_readme")[0], "task_context")
    assert [a["artifact_id"] for d in after["dependency_results"] for a in d["generated_artifacts"]] == ["add_readme.w1"]


# --- Step 5 + I: remediation fixes the deliverable --------------------------------------------------


async def test_remediation_fixes_the_project_from_its_real_files(harness_factory: Any) -> None:  # noqa: F811
    fix = {**planned("fix_reset", agent_type="specialist", task_type="domain_task", dependencies=["build_counter"],
                     description="Add the missing Reset button to src/App.tsx and write the complete counter-app again."),
           "replaces": None}
    h = await harness_factory(provider(
        {"build_counter": write_counter({**FILES, "src/App.tsx": APP_WITHOUT_RESET}), "fix_reset": write_counter()},
        plan=generation_plan(), verifier=faithful_verifier,
        replanner=in_order({"strategy_summary": "Add the missing Reset button to the project.", "tasks": [fix]})))
    run_id, outcome = await h.run(GOAL)
    assert outcome["phase"] == "completed", outcome["result"]["completion_blockers"]

    # The replanner was told how to remediate and which task produced the deliverable.
    [replan] = [r for r in h.llm.requests if r.purpose == "replanner"]
    for phrase in ("fix the deliverable itself", "Never plan a task that writes a document, summary or \"evidence\" file",
                   "Packaging is automatic", "not a reason to produce deliverables the user did not ask for"):
        assert phrase in replan.system, phrase
    recovery = context_json(replan, "recovery_context")
    assert [(a["artifact_id"], a["task_id"], a["name"]) for a in recovery["generated_artifacts"]] == [
        ("build_counter.w1", "build_counter", PROJECT)]
    assert "src/App.tsx" in recovery["generated_artifacts"][0]["paths"]

    # The fix task started from the real (broken) source, and the second verification read the fixed one.
    [dep] = context_json(agent_requests(h.llm, "fix_reset")[0], "task_context")["dependency_results"]
    assert {f["path"]: f["content"] for f in dep["generated_artifacts"][0]["files"]}["src/App.tsx"] == APP_WITHOUT_RESET
    first, second = verifier_requests(h.llm)
    assert judge_app(context_json(first, "verification_context"))[0] is False
    assert [a["artifact_id"] for a in context_json(second, "verification_context")["generated_artifacts"]] == ["fix_reset.w1"]
    assert judge_app(context_json(second, "verification_context"))[:2] == (True, "fix_reset.w1")
    events = await h.events(run_id)
    assert "ArtifactAdded" not in [e.event_type.value for e in events]  # no evidence document anywhere
    # The fix supersedes the broken version: only the fixed project is delivered.
    ready = sorted(a["artifact_id"] for a in (await h.artifacts(run_id)).values() if a["status"] == "ready")
    assert ready == ["fix_reset.w1.zip"]


# --- the content reader's bounds and integrity ------------------------------------------------------


def _artifact(workspace: ArtifactWorkspace, run_id: Any, files: dict[str, bytes], name: str = "proj") -> WorkspaceArtifact:
    for path, data in files.items():
        workspace.write(run_id, "t1", "project", name, path, data)
    return WorkspaceArtifact(
        artifact_id="t1.w1", name=name, artifact_type="project", status="validated", task_id="t1", sequence=1,
        files=tuple(ArtifactFile(path=p, media_type="text/plain", size=len(d), sha256=sha256(d), tool_call_id="t1.t1")
                    for p, d in files.items()),
    )


def test_read_texts_bounds_integrity_and_text_only(tmp_path: Path) -> None:
    from uuid import uuid4

    workspace, run_id = ArtifactWorkspace(tmp_path), uuid4()
    artifact = _artifact(workspace, run_id, {
        "a.txt": b"a" * 50, "b.txt": b"b" * 50, "c.txt": b"\x00\x01binary", "d.txt": "é".encode() * 10, "e.txt": b"e" * 10,
    })
    texts = read_texts(workspace, run_id, [artifact], max_file_chars=30, max_total_chars=70)
    assert texts[("t1.w1", "a.txt")].content == "a" * 30 and texts[("t1.w1", "a.txt")].truncated
    assert texts[("t1.w1", "b.txt")].content == "b" * 30 and texts[("t1.w1", "b.txt")].truncated
    assert texts[("t1.w1", "c.txt")].content is None and texts[("t1.w1", "c.txt")].note == NOT_TEXT
    assert texts[("t1.w1", "d.txt")].content == "é" * 10 and not texts[("t1.w1", "d.txt")].truncated
    assert texts[("t1.w1", "e.txt")].content is None and texts[("t1.w1", "e.txt")].note == BUDGET_USED

    # A file altered after it was recorded is never shown.
    workspace.file_location(run_id, "t1", "project", "proj", "a.txt").write_bytes(b"tampered")
    tampered = read_texts(workspace, run_id, [artifact])[("t1.w1", "a.txt")]
    assert (tampered.content, tampered.note) == (None, INTEGRITY)
    # Only recorded files are read, and only through the workspace.
    assert set(read_texts(workspace, run_id, [artifact])) == {("t1.w1", p) for p in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt")}
    assert read_texts(None, run_id, [artifact])[("t1.w1", "a.txt")].note == NO_WORKSPACE


@pytest.mark.parametrize("bad_path", ["../escape.txt", "/etc/passwd"])
def test_read_texts_refuses_unsafe_recorded_paths(tmp_path: Path, bad_path: str) -> None:
    from uuid import uuid4

    workspace, run_id = ArtifactWorkspace(tmp_path), uuid4()
    artifact = WorkspaceArtifact(
        artifact_id="t1.w1", name="proj", artifact_type="project", status="validated", task_id="t1", sequence=1,
        files=(ArtifactFile(path=bad_path, media_type="text/plain", size=1, sha256="0" * 64, tool_call_id="t1.t1"),),
    )
    assert read_texts(workspace, run_id, [artifact])[("t1.w1", bad_path)].note == INTEGRITY
