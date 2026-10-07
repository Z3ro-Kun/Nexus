"""Phase 10 end to end: an objective becomes a verified, downloadable project ZIP.

Through the HTTP API (POST /runs, POST /execute, GET /artifacts, GET .../download) over a
real database, with the real orchestrator, scheduler, agent runtime, artifact_write tool,
verification and run completion. The planner, agents and semantic verifier are
FakeLLMProvider scripts; files go to a temporary artifact workspace.

    research (researcher: a fact) --> build_project (specialist: artifact_write "my-project")
        --> validation + packaging (runtime) --> verify.objective --> ArtifactReady + RunCompleted
"""

import asyncio
import io
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.artifacts.workspace import ArtifactWorkspace, sha256
from app.core.config import Settings, get_settings
from app.events.base import Event
from app.events.factory import parse_payload
from app.llm.fake import FakeLLMProvider, FakeReply
from app.llm.schemas import LLMRequest
from app.main import create_app
from app.persistence.database import Database
from app.state.projector import apply, project
from app.tools.artifact_write import ArtifactWriteTool
from tests.agent_fixtures import planned
from tests.orchestration_fixtures import in_order, llm, verifier_says
from tests.tool_fixtures import call_tool, fact, fake_registry, finish

GOAL = "Create a small multi-file Python project."
PROJECT_FILES = {
    "src/main.py": "from utils import greet\n\n\nif __name__ == \"__main__\":\n    print(greet(\"NEXUS\"))\n",
    "src/utils.py": "def greet(name: str) -> str:\n    return f\"Hello, {name}!\"\n",
    "tests/test_main.py": "from utils import greet\n\n\ndef test_greet():\n    assert greet(\"x\") == \"Hello, x!\"\n",
    "README.md": "# my-project\n\nRun `python src/main.py`.\n",
    "requirements.txt": "pytest>=8\n",
}
PLAN = {"tasks": [
    planned("research", description="State the conventional layout of a small Python project."),
    planned("build_project", agent_type="specialist", task_type="domain_task", dependencies=["research"],
            description="Create the Python project my-project with source, tests, README and requirements."),
]}
RESEARCH = [finish("Python layout conventions.", [fact("Small Python projects keep code in src/ and tests in tests/.")])]


def write_project(files: dict[str, str] = PROJECT_FILES, name: str = "my-project") -> list[dict[str, Any]]:
    return [call_tool("artifact_write", project=name, files=[{"path": p, "content": c} for p, c in files.items()]),
            finish(f"Created {name} with {len(files)} files.", [])]


def write_file(name: str, content: str) -> list[dict[str, Any]]:
    return [call_tool("artifact_write", project=None, files=[{"path": name, "content": content}]), finish(f"Wrote {name}.", [])]


class Harness:
    def __init__(self, database: Database, tmp_path: Path, provider: FakeLLMProvider) -> None:
        self.workspace = ArtifactWorkspace(tmp_path / "artifacts")
        registry = fake_registry()
        registry.register(ArtifactWriteTool(self.workspace))
        self.settings = Settings(_env_file=None, NEXUS_ENVIRONMENT="test", NEXUS_VERIFICATION_SEMANTIC=True,  # type: ignore[call-arg]
                                 NEXUS_MAX_TOOL_CALLS_PER_TASK=3)
        self.app = create_app(self.settings, database=database, llm_provider=provider, tool_registry=registry)
        self.app.dependency_overrides[get_settings] = lambda: self.settings
        self.llm = provider

    async def __aenter__(self) -> "Harness":
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.client.aclose()

    async def run(self, goal: str = GOAL) -> tuple[str, dict[str, Any]]:
        run_id = (await self.client.post("/api/v1/runs", json={"goal": goal})).json()["id"]
        outcome = await self.client.post(f"/api/v1/runs/{run_id}/execute")
        assert outcome.status_code == 200, outcome.text
        return run_id, outcome.json()

    async def events(self, run_id: str) -> list[Event]:
        raw = (await self.client.get(f"/api/v1/runs/{run_id}/events")).json()
        return [Event.model_validate({**e, "payload": parse_payload(e["event_type"], e["payload"])}) for e in raw]

    async def artifacts(self, run_id: str) -> dict[str, dict[str, Any]]:
        return {a["artifact_id"]: a for a in (await self.client.get(f"/api/v1/runs/{run_id}/artifacts")).json()}

    def stored(self) -> list[str]:
        return sorted(p.relative_to(self.workspace.root).as_posix().split("/", 1)[1]
                      for p in self.workspace.root.rglob("*") if p.is_file())


def provider(steps: dict[str, list[dict[str, Any]]], plan: dict[str, Any] = PLAN, verifier: Any = None, **kw: Any) -> FakeLLMProvider:
    return llm(steps, planner=plan, verifier=verifier or verifier_says(evidence=["build_project.w1"]), **kw)


@pytest.fixture
async def harness_factory(database: Database, tmp_path: Path) -> AsyncIterator[Any]:
    opened: list[Harness] = []

    async def make(p: FakeLLMProvider) -> Harness:
        h = await Harness(database, tmp_path, p).__aenter__()
        opened.append(h)
        return h

    yield make
    for h in opened:
        await h.__aexit__()


# --- 21: the end-to-end deliverable ----------------------------------------------------------------


async def test_objective_to_verified_project_zip(harness_factory: Any) -> None:
    h = await harness_factory(provider({"research": RESEARCH, "build_project": write_project()}))
    run_id, outcome = await h.run()
    assert outcome["phase"] == "completed", outcome["result"]["completion_blockers"]

    artifacts = await h.artifacts(run_id)
    assert list(artifacts) == ["build_project.w1", "build_project.w1.zip"]
    project_view, archive = artifacts["build_project.w1"], artifacts["build_project.w1.zip"]
    assert (project_view["artifact_type"], project_view["status"], project_view["verified"], project_view["deliverable"]) == ("project", "validated", True, False)
    assert sorted(f["path"] for f in project_view["files"]) == sorted(PROJECT_FILES)
    assert (archive["artifact_type"], archive["status"], archive["name"], archive["deliverable"]) == ("archive", "ready", "my-project.zip", True)
    # Provenance: the generating task, its tool call, and the facts it was given.
    assert (project_view["task_id"], project_view["agent_id"], project_view["tool_call_ids"], project_view["input_fact_ids"]) == (
        "build_project", "specialist", ["build_project.t1"], ["research.f1"])
    listing = json.dumps(artifacts)
    assert str(h.workspace.root) not in listing and "packages/" not in listing and "files/build_project" not in listing

    # The download: exactly the intended files under my-project/, checksum as recorded.
    response = await h.client.get(archive["download_url"])
    assert response.status_code == 200
    data = response.content
    assert sha256(data) == archive["sha256"] == response.headers["x-artifact-sha256"]
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="my-project.zip"'
    assert response.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == sorted(f"my-project/{p}" for p in PROJECT_FILES)
        assert {n.removeprefix("my-project/"): zf.read(n).decode() for n in zf.namelist()} == PROJECT_FILES
    # Nothing else is stored: the manifest's files and the one package.
    assert h.stored() == sorted([*(f"files/build_project/project/my-project/{p}" for p in PROJECT_FILES),
                                 "packages/build_project.w1.zip.zip"])

    # Events: created -> files -> validated -> packaged with TaskCompleted; checked by the
    # verifier; ready right before RunCompleted. File content never enters the log.
    events = await h.events(run_id)
    kinds = [e.event_type.value for e in events]
    gen = [e.event_type.value for e in events if e.task_id == "build_project"]
    assert gen == ["TaskStarted", "ToolCalled", "ToolSucceeded", "ArtifactCreated", *["ArtifactFileAdded"] * 5,
                   "ArtifactValidated", "ArtifactPackaged", "TaskCompleted"]
    assert kinds[-2:] == ["ArtifactReady", "RunCompleted"]
    assert kinds.index("VerificationPassed") < kinds.index("ArtifactReady")
    log = json.dumps([e.payload.model_dump(mode="json") for e in events])
    assert "Hello, {name}" not in log and "pytest>=8" not in log
    called = next(e for e in events if e.event_type.value == "ToolCalled" and e.task_id == "build_project")
    assert called.payload.arguments["files"][0] == {  # type: ignore[attr-defined]
        "path": "src/main.py", "content_bytes": len(PROJECT_FILES["src/main.py"].encode()),
        "content_sha256": sha256(PROJECT_FILES["src/main.py"].encode())}

    # The checkpoint verified the artifacts; the semantic verifier could cite the project.
    state = (await h.client.get(f"/api/v1/runs/{run_id}/state")).json()
    checks = {c["check_id"]: c for c in state["verifications"]["verify.objective"]["checks"]}
    assert checks["artifacts"]["passed"] and "1 generated artifacts (5 files)" in checks["artifacts"]["message"]
    [verifier_request] = [r for r in h.llm.requests if r.purpose == "verifier"]
    assert '"generated_artifacts"' in verifier_request.messages[0].content and "src/main.py" in verifier_request.messages[0].content

    # 17: the event log replays to exactly the served state.
    incremental = None
    for event in events:
        incremental = apply(incremental, event)
    assert project(events).model_dump(mode="json") == incremental.model_dump(mode="json") == state  # type: ignore[union-attr]


# --- 18-20: delivery API guards ------------------------------------------------------------------------


async def test_download_guards(harness_factory: Any) -> None:
    h = await harness_factory(provider({"research": RESEARCH, "build_project": write_project()}))
    run_id, _ = await h.run()
    base = f"/api/v1/runs/{run_id}/artifacts"

    project_download = await h.client.get(f"{base}/build_project.w1/download")
    assert project_download.status_code == 409 and "build_project.w1.zip" in project_download.json()["error"]["message"]
    assert (await h.client.get(f"{base}/nope.w9/download")).status_code == 404
    assert (await h.client.get(f"{base}/bad%20id!/download")).status_code == 422
    assert (await h.client.get(f"{base}/..%2F..%2Fsecrets/download")).status_code in (404, 422)
    other_run = (await h.client.post("/api/v1/runs", json={"goal": "Another run."})).json()["id"]
    assert (await h.client.get(f"/api/v1/runs/{other_run}/artifacts/build_project.w1.zip/download")).json()["error"]["code"] == "artifact_not_found"
    unknown_run = "00000000-0000-0000-0000-000000000000"
    assert (await h.client.get(f"/api/v1/runs/{unknown_run}/artifacts/build_project.w1.zip/download")).status_code == 404

    zip_path = h.workspace.package_location(run_id, "build_project.w1.zip")  # type: ignore[arg-type]
    zip_path.write_bytes(b"tampered")
    corrupted = await h.client.get(f"{base}/build_project.w1.zip/download")
    assert (corrupted.status_code, corrupted.json()["error"]["code"]) == (500, "artifact_corrupted")
    zip_path.unlink()
    missing = await h.client.get(f"{base}/build_project.w1.zip/download")
    assert (missing.status_code, missing.json()["error"]["code"]) == (410, "artifact_missing")
    for response in (corrupted, missing):
        assert str(h.workspace.root) not in response.text and "packages" not in response.text


async def test_tampered_deliverable_is_never_marked_ready(harness_factory: Any) -> None:
    """The ZIP is altered after packaging, before the run completes: the delivery check
    rejects it and the run cannot complete; nothing is downloadable."""
    holder: dict[str, Harness] = {}

    def verifier(_: LLMRequest) -> FakeReply:
        h = holder["h"]
        [package] = list((h.workspace.root).rglob("*.zip"))
        package.write_bytes(package.read_bytes()[:-10] + b"0123456789")
        return FakeReply(data=verifier_says(evidence=["build_project.w1"]))

    h = await harness_factory(provider({"research": RESEARCH, "build_project": write_project()}, verifier=verifier))
    holder["h"] = h
    run_id, outcome = await h.run()

    assert outcome["phase"] != "completed"
    assert any("failed validation: delivery check" in b for b in outcome["result"]["completion_blockers"])
    archive = (await h.artifacts(run_id))["build_project.w1.zip"]
    assert archive["status"] == "rejected" and archive["download_url"] is None
    assert (await h.client.get(f"/api/v1/runs/{run_id}/artifacts/build_project.w1.zip/download")).status_code == 409
    kinds = [e.event_type.value for e in await h.events(run_id)]
    assert "ArtifactReady" not in kinds and "RunCompleted" not in kinds


# --- 10: a leaked secret is refused and remediated, never written or logged ----------------------------


async def test_secret_is_refused_and_the_task_is_remediated(harness_factory: Any) -> None:
    leaked = {**PROJECT_FILES, "src/config.py": 'API_KEY = "sk-' + "A1b2C3d4" * 6 + '"\n'}
    clean = {**PROJECT_FILES, "src/config.py": 'import os\n\nAPI_KEY = os.environ.get("API_KEY")\n'}
    replacement = {**planned("build_project_safe", agent_type="specialist", task_type="domain_task", dependencies=["research"],
                             description="Create my-project again, reading credentials from the environment."),
                   "replaces": "build_project"}
    h = await harness_factory(provider(
        {"research": RESEARCH, "build_project": write_project(leaked), "build_project_safe": write_project(clean)},
        verifier=verifier_says(evidence=["build_project_safe.w1"]),
        replanner=in_order({"strategy_summary": "Remove the hard-coded key.", "tasks": [replacement]}),
    ))
    run_id, outcome = await h.run()

    assert outcome["phase"] == "completed" and outcome["replanned"] == ["build_project_safe"]
    events = await h.events(run_id)
    failed = next(e for e in events if e.event_type.value == "ToolFailed" and e.task_id == "build_project")
    assert failed.payload.error_type == "artifact_rejected"  # type: ignore[attr-defined]
    assert "src/config.py line 1: likely OpenAI-style API key" in failed.payload.error  # type: ignore[attr-defined]
    assert "A1b2C3d4" not in json.dumps([e.payload.model_dump(mode="json") for e in events])
    assert list(await h.artifacts(run_id)) == ["build_project_safe.w1", "build_project_safe.w1.zip"]
    assert not any("build_project/" in p for p in h.stored())  # the refused write left nothing


# --- 23 + 16: concurrent generators; a single-file deliverable -------------------------------------------


async def test_concurrent_generators_get_separate_artifacts(harness_factory: Any) -> None:
    plan = {"tasks": [
        planned("gen_a", agent_type="specialist", task_type="domain_task", description="Write solution.py for the objective."),
        planned("gen_b", agent_type="specialist", task_type="domain_task", description="Create the project my-project."),
    ]}
    gates = {"gen_a": asyncio.Event(), "gen_b": asyncio.Event()}
    p = llm({"gen_a": write_file("solution.py", "print('solution')\n"), "gen_b": write_project({"main.py": "print(1)\n"})},
            planner=plan, verifier=verifier_says(evidence=["gen_a.w1", "gen_b.w1"]), gates=gates)
    h = await harness_factory(p)
    run_id = (await h.client.post("/api/v1/runs", json={"goal": GOAL})).json()["id"]
    execution = asyncio.create_task(h.client.post(f"/api/v1/runs/{run_id}/execute"))
    await p.wait_until_called("gen_a")
    await p.wait_until_called("gen_b")
    assert p.active == {"gen_a", "gen_b"}  # both generators at work at once
    for gate in gates.values():
        gate.set()
    assert (await execution).json()["phase"] == "completed"

    artifacts = await h.artifacts(run_id)
    assert sorted(artifacts) == ["gen_a.w1", "gen_b.w1", "gen_b.w1.zip"]
    solution = artifacts["gen_a.w1"]
    assert (solution["artifact_type"], solution["status"], solution["media_type"], solution["size"]) == ("file", "ready", "text/x-python", 18)
    response = await h.client.get(solution["download_url"])
    assert response.content == b"print('solution')\n" and sha256(response.content) == solution["sha256"]
    assert response.headers["content-type"] == "text/x-python; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="solution.py"'
