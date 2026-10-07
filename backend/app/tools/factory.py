"""Builds the production tool registry and policy from settings.

What is available in a deployment:
- calculator: always (pure, offline, safe evaluator);
- http_fetch: only if NEXUS_HTTP_FETCH_ENABLED=true (real outbound network, SSRF-guarded);
- web_search: never yet (no real search backend implemented);
- python_analysis: never (no sandbox available; see app.tools.python_analysis);
- artifact_write: if NEXUS_ARTIFACTS_ENABLED (writes only into the artifact workspace).
Fake tools are never registered here.
"""

from app.artifacts.safety import ArtifactLimits
from app.artifacts.workspace import ArtifactWorkspace
from app.core.config import Settings
from app.tools.artifact_write import TOOL_NAME as ARTIFACT_TOOL, ArtifactWriteTool
from app.tools.calculator import CalculatorTool
from app.tools.http_fetch import HTTPFetchTool
from app.tools.policy import ToolPolicy
from app.tools.registry import ToolRegistry


def build_artifact_workspace(settings: Settings) -> ArtifactWorkspace | None:
    if not settings.artifacts_enabled:
        return None
    return ArtifactWorkspace(
        settings.artifact_root,
        ArtifactLimits(
            max_files=settings.artifact_max_files,
            max_file_bytes=settings.artifact_max_file_bytes,
            max_total_bytes=settings.artifact_max_total_bytes,
            max_zip_bytes=settings.artifact_max_zip_bytes,
            max_path_length=settings.artifact_max_path_length,
            max_path_depth=settings.artifact_max_path_depth,
            max_artifacts_per_task=settings.artifact_max_per_task,
        ),
    )


def workspace_of(registry: ToolRegistry) -> ArtifactWorkspace | None:
    """The artifact workspace behind a registry's artifact_write tool, if any."""
    return getattr(registry.get(ARTIFACT_TOOL), "workspace", None) if ARTIFACT_TOOL in registry else None


def build_tool_registry(settings: Settings, workspace: ArtifactWorkspace | None = None) -> ToolRegistry:
    registry = ToolRegistry([CalculatorTool()])
    if workspace is not None:
        registry.register(ArtifactWriteTool(workspace))
    if settings.http_fetch_enabled:
        registry.register(
            HTTPFetchTool(
                max_bytes=settings.http_fetch_max_bytes,
                max_timeout_seconds=settings.http_fetch_max_timeout_seconds,
            )
        )
    return registry


def build_tool_policy(settings: Settings) -> ToolPolicy:
    return ToolPolicy.with_specialist_tools(settings.specialist_tools)
