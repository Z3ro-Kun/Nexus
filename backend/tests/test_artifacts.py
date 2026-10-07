"""Phase 10 unit tests: artifact safety rules, the controlled workspace, the artifact_write
tool, deterministic packaging, and the artifact events' projection rules.

Every file goes to a temporary directory; nothing touches real project files.
"""

import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.agents.registry import AgentRegistry
from app.artifacts.safety import ArtifactLimits, UnsafeArtifactError, find_secrets, normalize_path
from app.artifacts.workspace import ArtifactIntegrityError, ArtifactWorkspace, build_zip, sha256, verify_zip
from app.core.exceptions import InvalidEventError
from app.events.types import (
    ACTION_AGENT_TYPE,
    ArtifactCreated,
    ArtifactFileAdded,
    ArtifactPackaged,
    ArtifactReady,
    ArtifactValidated,
    RunCreated,
    TaskCreated,
    TaskStarted,
    ToolCalled,
    ToolSucceeded,
)
from app.llm.fake import FakeLLMProvider
from app.state.projector import project
from app.tools.artifact_write import ArtifactWriteTool, record_arguments
from app.tools.executor import ToolExecutor
from app.tools.policy import ToolPolicy
from app.tools.registry import ToolRegistry
from app.tools.schemas import ToolCall, ToolContext
from tests.helpers import history

RUN = uuid4()
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
FAKE_OPENAI_KEY = "sk-" + "A1b2C3d4" * 6  # matches the key format; not a real key


def workspace(tmp_path: Path, **limits: Any) -> ArtifactWorkspace:
    return ArtifactWorkspace(tmp_path / "artifacts", ArtifactLimits(**limits))


def executor(ws: ArtifactWorkspace, permitted: bool = True) -> ToolExecutor:
    registry = ToolRegistry([ArtifactWriteTool(ws)])
    return ToolExecutor(registry, ToolPolicy.with_specialist_tools(["artifact_write"] if permitted else []))


async def write(ex: ToolExecutor, files: dict[str, str], project: str | None = None, *, task: str = "gen",
                agent: str = "specialist", run: Any = RUN) -> Any:
    call = ToolCall(tool_name="artifact_write", arguments={"project": project, "files": [{"path": p, "content": c} for p, c in files.items()]})
    return await ex.execute(call, ToolContext(run_id=run, task_id=task, agent_type=agent, tool_call_id=f"{task}.t1"))


def tree(ws: ArtifactWorkspace) -> list[str]:
    return sorted(p.relative_to(ws.root).as_posix() for p in ws.root.rglob("*") if p.is_file())


# --- 1-5: creation -----------------------------------------------------------------------------


async def test_single_file_artifact(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    result = await write(executor(ws), {"solution.py": "print('hi')\n"})
    assert result.success, result.error
    assert result.output == {"artifact": "solution.py", "kind": "file", "files": [
        {"path": "solution.py", "media_type": "text/x-python", "size": 12, "sha256": sha256(b"print('hi')\n")}]}
    assert tree(ws) == [f"{RUN}/files/gen/file/solution.py/solution.py"]


async def test_project_with_nested_files_creates_directories(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    files = {"src/main.py": "from utils import add\n", "src/utils.py": "def add(a, b):\n    return a + b\n",
             "tests/test_main.py": "def test_add():\n    assert True\n", "README.md": "# My project\n", "requirements.txt": "pytest\n"}
    result = await write(executor(ws), files, "my-project")
    assert result.success, result.error
    assert [f["path"] for f in result.output["files"]] == list(files)
    base = f"{RUN}/files/gen/project/my-project"
    assert tree(ws) == sorted(f"{base}/{p}" for p in files)
    assert ws.existing_files(RUN, "gen", "project", "my-project") == {p: len(c.encode()) for p, c in files.items()}


async def test_writing_again_adds_files_and_replaces_a_path(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    ex = executor(ws)
    assert (await write(ex, {"a.py": "1"}, "proj")).success
    assert (await write(ex, {"b.py": "2", "a.py": "one"}, "proj")).success
    assert ws.existing_files(RUN, "gen", "project", "proj") == {"a.py": 3, "b.py": 1}


# --- 8-14: refusals (nothing is written) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("../escape.py", "'..'"),
        ("src/../../etc/passwd", "'..'"),
        ("/etc/passwd", "absolute"),
        ("C:/Windows/system.ini", "absolute"),
        ("src\\main.py", "'/' separators"),
        ("~/notes.md", "absolute"),
        ("src//main.py", "empty"),
        ("./main.py", "'.'"),
        (".env", "environment, credential or key file"),
        ("config/.env.production", "environment, credential or key file"),
        ("deploy/server.pem", "excluded file type"),
        ("keys/id_rsa", "environment, credential or key file"),
        (".venv/lib/site.py", "dependency, VCS, cache"),
        ("node_modules/x/index.js", "dependency, VCS, cache"),
        (".git/config", "dependency, VCS, cache"),
        ("src/__pycache__/m.cpython-310.pyc", "dependency, VCS, cache"),
        ("dist/app.js", "dependency, VCS, cache"),
        ("logs/run.txt", "dependency, VCS, cache"),
        ("aux.py", "reserved"),
    ],
)
async def test_unsafe_or_excluded_paths_are_refused(tmp_path: Path, path: str, rule: str) -> None:
    ws = workspace(tmp_path)
    result = await write(executor(ws), {"ok.py": "x = 1\n", path: "data\n"}, "proj")
    assert not result.success and result.error_type == "artifact_rejected"
    assert rule in (result.error or "")
    assert tree(ws) == []  # all or nothing


async def test_env_templates_are_allowed_but_still_scanned(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    assert (await write(executor(ws), {".env.example": "API_KEY=your-key-here\n"}, "proj")).success
    leaked = await write(executor(ws), {".env.example": f'API_KEY="{FAKE_OPENAI_KEY}"\n'}, "proj2")
    assert not leaked.success and "likely" in (leaked.error or "")


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        (PRIVATE_KEY, "private key block"),
        (f'OPENAI_API_KEY = "{FAKE_OPENAI_KEY}"', "OpenAI-style API key"),
        ("aws = 'AKIA" + "ABCDEFGHIJKLMNOP'", "AWS access key id"),
        ('password = "hunter2hunter2"', "hard-coded secret in 'password'"),
        ("DATABASE_URL = 'postgres://admin:s3cretpass@db/app'", "credentials in a URL"),
    ],
)
async def test_likely_secrets_are_refused_without_echoing_them(tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str, kind: str) -> None:
    ws = workspace(tmp_path)
    caplog.set_level(logging.DEBUG)
    result = await write(executor(ws), {"src/config.py": f"# settings\n{content}\n"}, "proj")
    assert not result.success and result.error_type == "artifact_rejected"
    assert f"src/config.py line 2: likely {kind}" in (result.error or "")
    secret_part = content.split("=")[-1].strip().strip("'\"")[:12] if "=" in content else "MIIEpAIBAAKC"
    assert secret_part not in (result.error or "") and secret_part not in caplog.text
    assert tree(ws) == []


def test_placeholders_and_environment_lookups_are_not_secrets() -> None:
    text = 'API_KEY = os.environ["API_KEY"]\npassword = "changeme"\ntoken = "<your-token>"\nsecret = "${SECRET}"\n'
    assert find_secrets("app.py", text) == []


async def test_size_and_count_limits(tmp_path: Path) -> None:
    ws = workspace(tmp_path, max_file_bytes=1000, max_files=3, max_total_bytes=1500, max_artifacts_per_task=2)
    ex = executor(ws)
    big = await write(ex, {"big.txt": "x" * 1001}, "p1")
    assert not big.success and "per-file limit is 1000" in (big.error or "")
    many = await write(ex, {f"f{i}.txt": "x" for i in range(4)}, "p1")
    assert not many.success and "the limit is 3" in (many.error or "")
    assert (await write(ex, {"a.txt": "x" * 900}, "p1")).success
    total = await write(ex, {"b.txt": "x" * 700}, "p1")
    assert not total.success and "the limit is 1500" in (total.error or "")
    assert (await write(ex, {"c.txt": "x"}, "p2")).success
    third = await write(ex, {"d.txt": "x"}, "p3")
    assert not third.success and "at most 2 artifacts" in (third.error or "")


def test_path_length_and_depth_limits() -> None:
    limits = ArtifactLimits(max_path_length=30, max_path_depth=3)
    assert normalize_path("a/b/c.py", limits) == "a/b/c.py"
    with pytest.raises(UnsafeArtifactError, match="deeper than 3"):
        normalize_path("a/b/c/d.py", limits)
    with pytest.raises(UnsafeArtifactError, match="longer than 30"):
        normalize_path("x" * 31, limits)


async def test_case_insensitive_duplicates_and_file_directory_clashes(tmp_path: Path) -> None:
    ex = executor(workspace(tmp_path))
    assert "duplicates" in ((await write(ex, {"README.md": "a", "readme.md": "b"}, "p")).error or "")
    assert "also a directory" in ((await write(ex, {"src": "a", "src/main.py": "b"}, "p2")).error or "")


async def test_single_file_artifact_is_one_plain_file(tmp_path: Path) -> None:
    ex = executor(workspace(tmp_path))
    assert "exactly one file" in ((await write(ex, {"a.py": "1", "b.py": "2"})).error or "")
    assert "invalid artifact name" in ((await write(ex, {"src/a.py": "1"})).error or "")


def test_symlink_inside_the_workspace_is_refused(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link_parent = ws.artifact_dir(RUN, "gen", "project", "proj")
    link_parent.mkdir(parents=True)
    try:
        os.symlink(outside, link_parent / "src", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symbolic links is not permitted here")
    with pytest.raises(UnsafeArtifactError):
        ws.write(RUN, "gen", "project", "proj", "src/evil.py", b"x")
    assert list(outside.iterdir()) == []


def test_locations_never_leave_the_root(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    for bad in ("../x", "/abs", "a/../../b"):
        with pytest.raises(UnsafeArtifactError):
            ws.file_location(RUN, "gen", "project", "proj", bad)
    with pytest.raises(UnsafeArtifactError):
        ws.artifact_dir(RUN, "../gen", "project", "proj")
    with pytest.raises(UnsafeArtifactError):
        ws.package_location(RUN, "../../x")


# --- permissions and recording ---------------------------------------------------------------


async def test_tool_needs_an_explicit_permission_and_is_agent_only(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    denied = await write(executor(ws, permitted=False), {"a.py": "1"}, "p")
    assert not denied.success and denied.error_type == "unauthorized"
    as_action = await write(executor(ws), {"a.py": "1"}, "p", agent=ACTION_AGENT_TYPE)
    assert not as_action.success and as_action.error_type == "policy_denied"
    assert tree(ws) == []
    registry = AgentRegistry(FakeLLMProvider(), max_tokens=1000, tool_executor=executor(ws), max_tool_calls=3)
    assert registry.planner()._action_tools == ()  # never offered to the planner as an action


def test_recorded_arguments_never_contain_file_content() -> None:
    recorded = record_arguments({"project": "p", "files": [{"path": "a.py", "content": PRIVATE_KEY}, "junk"]})
    assert recorded == {"project": "p", "files": [
        {"path": "a.py", "content_bytes": len(PRIVATE_KEY.encode()), "content_sha256": sha256(PRIVATE_KEY.encode())},
        {"invalid": True}]}


# --- 6, 7, 15, 21: packaging, checksums, integrity ------------------------------------------------


def test_zip_is_deterministic_and_contains_exactly_the_manifest(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    files = {"src/main.py": b"print(1)\n", "README.md": b"# p\n"}
    first = ws.package(RUN, "gen.w1.zip", "my-project", files)
    assert build_zip("my-project", dict(reversed(list(files.items())))) == first  # byte-identical
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ["my-project/README.md", "my-project/src/main.py"]
        assert archive.read("my-project/src/main.py") == b"print(1)\n"
    assert ws.read_package(RUN, "gen.w1.zip", sha256(first)) == first


def test_zip_verification_rejects_tampering(tmp_path: Path) -> None:
    files = {"a.py": b"1"}
    expected = {"a.py": sha256(b"1")}
    verify_zip(build_zip("p", files), "p", expected)
    with pytest.raises(UnsafeArtifactError, match="differ from the project manifest"):
        verify_zip(build_zip("p", {**files, "extra.py": b"2"}), "p", expected)
    with pytest.raises(UnsafeArtifactError, match="does not match its recorded checksum"):
        verify_zip(build_zip("p", {"a.py": b"changed"}), "p", expected)
    with pytest.raises(UnsafeArtifactError, match="cannot be opened"):
        verify_zip(b"not a zip", "p", expected)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("p/a.py", b"1")
        archive.writestr("p/.env", b"SECRET=1")
    with pytest.raises(UnsafeArtifactError):
        verify_zip(buffer.getvalue(), "p", expected)


def test_reads_check_bytes_against_the_recorded_checksum(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    ws.write(RUN, "gen", "file", "a.py", "a.py", b"good")
    assert ws.read_file(RUN, "gen", "file", "a.py", "a.py", sha256(b"good")) == b"good"
    ws.file_location(RUN, "gen", "file", "a.py", "a.py").write_bytes(b"evil")
    with pytest.raises(ArtifactIntegrityError, match="does not match") as corrupted:
        ws.read_file(RUN, "gen", "file", "a.py", "a.py", sha256(b"good"))
    assert not corrupted.value.missing
    ws.file_location(RUN, "gen", "file", "a.py", "a.py").unlink()
    with pytest.raises(ArtifactIntegrityError) as missing:
        ws.read_file(RUN, "gen", "file", "a.py", "a.py", sha256(b"good"))
    assert missing.value.missing


# --- 16, 17, 22: event rules and replay ---------------------------------------------------------------


SHA = sha256(b"x")


def base_events(*extra: Any) -> list[Any]:
    return [
        RunCreated(goal="Create a file."),
        TaskCreated(task_id="gen", title="Generate", task_type="domain_task", agent_type="specialist"),
        TaskStarted(task_id="gen"),
        ToolCalled(tool_call_id="gen.t1", tool_name="artifact_write", arguments={"project": None}),
        ToolSucceeded(tool_call_id="gen.t1", result={}),
        *extra,
    ]


def replay(*payloads: Any, task: str | None = "gen") -> Any:
    events = history(RUN, *payloads)
    events = [e.model_copy(update={"task_id": task}) if i >= 2 else e for i, e in enumerate(events)]
    return project(events)


def test_artifact_events_build_the_state_and_record_provenance() -> None:
    state = replay(*base_events(
        ArtifactCreated(artifact_id="gen.w1", name="proj", artifact_type="project", tool_call_ids=["gen.t1"]),
        ArtifactFileAdded(artifact_id="gen.w1", path="src/a.py", media_type="text/x-python", size=1, sha256=SHA, tool_call_id="gen.t1"),
        ArtifactValidated(artifact_id="gen.w1", passed=True, file_count=1, total_bytes=1),
        ArtifactPackaged(artifact_id="gen.w1.zip", source_artifact_id="gen.w1", name="proj.zip", size=100, sha256=SHA, paths=["src/a.py"]),
    ))
    project_ = state.workspace_artifacts["gen.w1"]
    archive = state.workspace_artifacts["gen.w1.zip"]
    assert (project_.status, project_.archive_id, project_.task_id, project_.tool_call_ids) == ("validated", "gen.w1.zip", "gen", ("gen.t1",))
    assert [f.path for f in project_.files] == ["src/a.py"]
    assert (archive.artifact_type, archive.status, archive.sha256, archive.source_artifact_id) == ("archive", "validated", SHA, "gen.w1")


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (ArtifactFileAdded(artifact_id="gen.w1", path="../x.py", media_type="t", size=1, sha256=SHA, tool_call_id="gen.t1"), "'..'"),
        (ArtifactFileAdded(artifact_id="gen.w1", path=".env", media_type="t", size=1, sha256=SHA, tool_call_id="gen.t1"), "excluded"),
        (ArtifactFileAdded(artifact_id="gen.w1", path="a.py", media_type="t", size=1, sha256=SHA, tool_call_id="other.t9"), "not one of its"),
        (ArtifactValidated(artifact_id="gen.w1", passed=True, file_count=0, total_bytes=0), "counts do not match"),
        (ArtifactPackaged(artifact_id="gen.w1.zip", source_artifact_id="gen.w1", name="p.zip", size=1, sha256=SHA, paths=["a.py"]), "not a validated"),
        (ArtifactReady(artifact_id="gen.w1", sha256=SHA), "not a validated file or archive"),
        (ArtifactFileAdded(artifact_id="nope", path="a.py", media_type="t", size=1, sha256=SHA, tool_call_id="gen.t1"), "does not exist"),
    ],
)
def test_projector_rejects_invalid_artifact_events(bad: Any, match: str) -> None:
    with pytest.raises(InvalidEventError, match=match):
        replay(*base_events(ArtifactCreated(artifact_id="gen.w1", name="proj", artifact_type="project", tool_call_ids=["gen.t1"]), bad))


def test_artifact_needs_a_real_artifact_write_call_of_its_task() -> None:
    with pytest.raises(InvalidEventError, match="not a successful artifact_write call"):
        replay(*base_events(ArtifactCreated(artifact_id="gen.w1", name="p", artifact_type="project", tool_call_ids=["gen.t7"])))
    with pytest.raises(InvalidEventError, match="unknown input facts"):
        replay(*base_events(ArtifactCreated(artifact_id="gen.w1", name="p", artifact_type="project", tool_call_ids=["gen.t1"], input_fact_ids=["ghost.f1"])))


def test_ready_requires_a_passed_verification() -> None:
    events = base_events(
        ArtifactCreated(artifact_id="gen.w1", name="a.py", artifact_type="file", tool_call_ids=["gen.t1"]),
        ArtifactFileAdded(artifact_id="gen.w1", path="a.py", media_type="text/x-python", size=1, sha256=SHA, tool_call_id="gen.t1"),
        ArtifactValidated(artifact_id="gen.w1", passed=True, file_count=1, total_bytes=1),
    )
    assert replay(*events).workspace_artifacts["gen.w1"].sha256 == SHA
    with pytest.raises(InvalidEventError, match="verification has not passed"):
        replay(*events, ArtifactReady(artifact_id="gen.w1", sha256=SHA))
