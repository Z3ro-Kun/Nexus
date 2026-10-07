"""Application settings loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

# Role-based LLM routing (see app.llm.routing). "default" = the provider configured by
# NEXUS_LLM_PROVIDER (e.g. the Navigate Labs endpoint); "gemini" / "openrouter" / "groq" =
# their OpenAI-compatible APIs through the same OpenAIProvider adapter.
RouteProvider = Literal["default", "gemini", "openrouter", "groq"]
# Providers reached through OpenAIProvider with NEXUS_<NAME>_API_KEY / _BASE_URL.
HOSTED_PROVIDERS = ("gemini", "openrouter", "groq")
LLM_ROLES = ("planner", "researcher", "analyst", "specialist", "conflict_resolver", "replanner", "verifier")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Looked up relative to the working directory: backend/ or the repo root.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="NEXUS", validation_alias="NEXUS_APP_NAME")
    environment: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="NEXUS_ENVIRONMENT"
    )
    log_level: str = Field(default="INFO", validation_alias="NEXUS_LOG_LEVEL")
    api_prefix: str = "/api"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"],
        validation_alias="NEXUS_CORS_ORIGINS",
    )

    postgres_host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    postgres_user: str = Field(default="nexus", validation_alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(
        default=SecretStr(""), validation_alias="POSTGRES_PASSWORD"
    )
    postgres_db: str = Field(default="nexus", validation_alias="POSTGRES_DB")

    # --- LLM / agents (Phase 3) ------------------------------------------------------
    # "none" disables every LLM-backed endpoint (they return 503). Tests inject a fake.
    llm_provider: Literal["none", "anthropic", "openai"] = Field(
        default="none", validation_alias="NEXUS_LLM_PROVIDER"
    )
    # Default applies to Anthropic only; with NEXUS_LLM_PROVIDER=openai it must be set.
    llm_model: str = Field(default="claude-opus-5", validation_alias="NEXUS_LLM_MODEL")
    # Optional: if unset, the Anthropic SDK resolves credentials itself (ANTHROPIC_API_KEY
    # in the process environment, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile).
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    # --- OpenAI-compatible provider (NEXUS_LLM_PROVIDER=openai) ----------------------
    # Optional: if unset, the OpenAI SDK reads OPENAI_API_KEY / OPENAI_BASE_URL from the
    # process environment (not from .env); without a base URL it uses api.openai.com.
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    # "json_schema" (response_format json_schema) or "json_object" (schema in the prompt,
    # for servers without schema support).
    openai_response_format: Literal["json_schema", "json_object"] = Field(
        default="json_schema", validation_alias="NEXUS_OPENAI_RESPONSE_FORMAT"
    )
    # Strict json_schema: "auto" (default) = strict, with schemas that have optional fields
    # (agent reports, tool steps) sent as required+nullable and the nulls removed again
    # (see app.llm.openai_provider); non-strict only for schemas that cannot be made
    # strict. true = always strict; false = never. Responses are validated either way.
    openai_strict_schema: bool | Literal["auto"] = Field(
        default="auto", validation_alias="NEXUS_OPENAI_STRICT_SCHEMA"
    )
    # Some models (e.g. OpenAI reasoning models) accept only max_completion_tokens.
    openai_max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = Field(
        default="max_tokens", validation_alias="NEXUS_OPENAI_MAX_TOKENS_PARAM"
    )
    # --- Role-based routing (heterogeneous models) -----------------------------------
    # Provider credentials. A key is required only if some role is routed to the provider.
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="NEXUS_GEMINI_API_KEY")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/", validation_alias="NEXUS_GEMINI_BASE_URL"
    )
    openrouter_api_key: SecretStr | None = Field(default=None, validation_alias="NEXUS_OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1", validation_alias="NEXUS_OPENROUTER_BASE_URL")
    groq_api_key: SecretStr | None = Field(default=None, validation_alias="NEXUS_GROQ_API_KEY")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1", validation_alias="NEXUS_GROQ_BASE_URL")
    # Per role: provider + model. Unset = the default provider (behavior before routing).
    planner_provider: RouteProvider | None = Field(default=None, validation_alias="NEXUS_PLANNER_PROVIDER")
    planner_model: str | None = Field(default=None, max_length=200, validation_alias="NEXUS_PLANNER_MODEL")
    researcher_provider: RouteProvider | None = Field(default=None, validation_alias="NEXUS_RESEARCHER_PROVIDER")
    researcher_model: str | None = Field(default=None, max_length=200, validation_alias="NEXUS_RESEARCHER_MODEL")
    analyst_provider: RouteProvider | None = Field(default=None, validation_alias="NEXUS_ANALYST_PROVIDER")
    analyst_model: str | None = Field(default=None, max_length=200, validation_alias="NEXUS_ANALYST_MODEL")
    specialist_provider: RouteProvider | None = Field(default=None, validation_alias="NEXUS_SPECIALIST_PROVIDER")
    specialist_model: str | None = Field(default=None, max_length=200, validation_alias="NEXUS_SPECIALIST_MODEL")
    conflict_resolver_provider: RouteProvider | None = Field(default=None, validation_alias="NEXUS_CONFLICT_RESOLVER_PROVIDER")
    conflict_resolver_model: str | None = Field(default=None, max_length=200, validation_alias="NEXUS_CONFLICT_RESOLVER_MODEL")
    replanner_provider: RouteProvider | None = Field(default=None, validation_alias="NEXUS_REPLANNER_PROVIDER")
    replanner_model: str | None = Field(default=None, max_length=200, validation_alias="NEXUS_REPLANNER_MODEL")
    verifier_provider: RouteProvider | None = Field(default=None, validation_alias="NEXUS_VERIFIER_PROVIDER")
    verifier_model: str | None = Field(default=None, max_length=200, validation_alias="NEXUS_VERIFIER_MODEL")
    llm_timeout_seconds: float = Field(default=60.0, gt=0, validation_alias="NEXUS_LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=1, ge=0, le=5, validation_alias="NEXUS_LLM_MAX_RETRIES")
    llm_max_tokens: int = Field(default=16000, ge=256, validation_alias="NEXUS_LLM_MAX_TOKENS")
    # Hard wall-clock bound on one planner or agent call, including provider retries.
    agent_timeout_seconds: float = Field(
        default=180.0, gt=0, validation_alias="NEXUS_AGENT_TIMEOUT_SECONDS"
    )
    max_planned_tasks: int = Field(default=10, ge=1, le=100, validation_alias="NEXUS_MAX_PLANNED_TASKS")

    # --- Tools (Phase 4) -------------------------------------------------------------
    # Upper bound on tool calls per task; 0 disables tool use entirely.
    max_tool_calls_per_task: int = Field(
        default=5, ge=0, le=20, validation_alias="NEXUS_MAX_TOOL_CALLS_PER_TASK"
    )
    # Real outbound HTTP for agents is opt-in.
    http_fetch_enabled: bool = Field(default=False, validation_alias="NEXUS_HTTP_FETCH_ENABLED")
    http_fetch_max_bytes: int = Field(
        default=262_144, ge=1024, le=5_000_000, validation_alias="NEXUS_HTTP_FETCH_MAX_BYTES"
    )
    http_fetch_max_timeout_seconds: float = Field(
        default=15.0, gt=0, le=30, validation_alias="NEXUS_HTTP_FETCH_MAX_TIMEOUT_SECONDS"
    )
    # Tools the specialist agent may use (JSON list), e.g. ["calculator", "http_fetch"].
    # "artifact_write" (Phase 10) lets it create deliverable files and projects.
    specialist_tools: list[Literal["calculator", "web_search", "http_fetch", "python_analysis", "artifact_write"]] = Field(
        default_factory=lambda: ["calculator", "artifact_write"], validation_alias="NEXUS_SPECIALIST_TOOLS"
    )
    # Tool output longer than this is truncated in what the LLM sees (events keep it all).
    tool_output_max_chars: int = Field(
        default=20_000, ge=1000, validation_alias="NEXUS_TOOL_OUTPUT_MAX_CHARS"
    )

    # --- Artifacts (Phase 10) ---------------------------------------------------------
    # Generated files live in a controlled workspace under this directory (one folder per
    # run); relative paths resolve against the working directory. Only NEXUS writes there.
    artifacts_enabled: bool = Field(default=True, validation_alias="NEXUS_ARTIFACTS_ENABLED")
    artifact_root: str = Field(default="var/artifacts", min_length=1, validation_alias="NEXUS_ARTIFACT_ROOT")
    artifact_max_files: int = Field(default=200, ge=1, le=5000, validation_alias="NEXUS_ARTIFACT_MAX_FILES")
    artifact_max_file_bytes: int = Field(default=1_000_000, ge=1000, le=50_000_000, validation_alias="NEXUS_ARTIFACT_MAX_FILE_BYTES")
    artifact_max_total_bytes: int = Field(default=20_000_000, ge=1000, le=500_000_000, validation_alias="NEXUS_ARTIFACT_MAX_TOTAL_BYTES")
    artifact_max_zip_bytes: int = Field(default=20_000_000, ge=1000, le=500_000_000, validation_alias="NEXUS_ARTIFACT_MAX_ZIP_BYTES")
    artifact_max_path_length: int = Field(default=200, ge=20, le=1000, validation_alias="NEXUS_ARTIFACT_MAX_PATH_LENGTH")
    artifact_max_path_depth: int = Field(default=10, ge=1, le=50, validation_alias="NEXUS_ARTIFACT_MAX_PATH_DEPTH")
    artifact_max_per_task: int = Field(default=5, ge=1, le=50, validation_alias="NEXUS_ARTIFACT_MAX_PER_TASK")

    # --- Recovery (Phase 5) ----------------------------------------------------------
    # Replanner invocations allowed per run (accepted + rejected). 0 disables recovery.
    max_replans_per_run: int = Field(default=2, ge=0, le=5, validation_alias="NEXUS_MAX_REPLANS_PER_RUN")

    # --- Conflicts (Phase 6) ---------------------------------------------------------
    # Resolution tasks created per run. Conflicts beyond it are recorded (OPEN) without
    # a task. 0 = detect and record conflicts only.
    max_conflict_resolutions_per_run: int = Field(
        default=5, ge=0, le=20, validation_alias="NEXUS_MAX_CONFLICT_RESOLUTIONS_PER_RUN"
    )

    # --- Policy gate (Phase 8) ---------------------------------------------------------
    # Outcome per action category: "allow", "approval_required" or "deny". read_only is
    # always allowed. Tools come with an authoritative category (ToolDefinition.category).
    policy_network_read: Literal["allow", "approval_required", "deny"] = Field(
        default="allow", validation_alias="NEXUS_POLICY_NETWORK_READ"
    )
    policy_reversible_write: Literal["allow", "approval_required", "deny"] = Field(
        default="allow", validation_alias="NEXUS_POLICY_REVERSIBLE_WRITE"
    )
    policy_irreversible: Literal["allow", "approval_required", "deny"] = Field(
        default="approval_required", validation_alias="NEXUS_POLICY_IRREVERSIBLE"
    )
    # Tools that are always denied / always need approval (JSON lists of tool names).
    policy_denied_tools: list[str] = Field(default_factory=list, validation_alias="NEXUS_POLICY_DENIED_TOOLS")
    policy_approval_tools: list[str] = Field(default_factory=list, validation_alias="NEXUS_POLICY_APPROVAL_TOOLS")

    # --- Orchestration (Phase 9) -----------------------------------------------------
    # The final checkpoint also asks the LLM verifier (only when an LLM is in use).
    verification_semantic: bool = Field(default=True, validation_alias="NEXUS_VERIFICATION_SEMANTIC")
    # Upper bound on scheduler passes per /execute call (a pass without progress ends it).
    max_orchestration_passes: int = Field(default=10, ge=1, le=100, validation_alias="NEXUS_MAX_ORCHESTRATION_PASSES")

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _provider_case_insensitive(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("openai_strict_schema", mode="before")
    @classmethod
    def _strict_schema_case_insensitive(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("openai_api_key", "openai_base_url", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator(*(f"{p}_api_key" for p in HOSTED_PROVIDERS), *(f"{r}_model" for r in LLM_ROLES), mode="before")
    @classmethod
    def _empty_routing_value_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator(*(f"{r}_provider" for r in LLM_ROLES), mode="before")
    @classmethod
    def _route_provider(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            return value or None
        return value

    @field_validator(*(f"{p}_base_url" for p in HOSTED_PROVIDERS))
    @classmethod
    def _https_base_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("provider base URLs must use https://")
        return value

    @model_validator(mode="after")
    def _routes_are_complete(self) -> "Settings":
        for role in LLM_ROLES:
            provider, model = getattr(self, f"{role}_provider"), getattr(self, f"{role}_model")
            name = f"NEXUS_{role.upper()}"
            if model is not None and provider is None:
                raise ValueError(f"{name}_MODEL is set but {name}_PROVIDER is not")
            if provider in HOSTED_PROVIDERS and model is None:
                raise ValueError(f"{name}_PROVIDER={provider} requires {name}_MODEL")
        return self

    @model_validator(mode="after")
    def _openai_needs_model(self) -> "Settings":
        if self.llm_provider == "openai" and "llm_model" not in self.model_fields_set:
            raise ValueError("NEXUS_LLM_MODEL must be set when NEXUS_LLM_PROVIDER=openai")
        return self

    @property
    def database_url(self) -> URL:
        """Async SQLAlchemy URL. The password is masked when the URL is rendered."""
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value() or None,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
