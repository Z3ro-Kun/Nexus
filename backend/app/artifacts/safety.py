"""Deterministic safety rules for generated artifacts. Pure functions, no I/O.

Used by the `artifact_write` tool (before anything is written), by the agent runtime (when
it validates and packages a task's artifacts) and by the projector (artifact events must
name safe paths). Each rule is applied in more than one place on purpose: the manifest
says what a deliverable contains; these rules are the defense in depth on top of it.

- Paths are relative POSIX paths inside one artifact: no absolute paths, drive letters,
  backslashes, `.`/`..` or empty segments, control characters, Windows reserved names or
  trailing dots/spaces; bounded length and depth; unique case-insensitively.
- Excluded paths: dependency, VCS, cache, build and log directories (.venv, node_modules,
  .git, __pycache__, dist, build, ...), and secret-bearing files (.env, *.pem, *.key,
  credentials, private SSH keys, ...). NEXUS delivers project source, not environments.
- Likely secrets: private-key blocks, well-known token formats and password/key
  assignments with literal values. This is a heuristic, not a complete secret scanner.
  Findings name the file, line and kind, never the matched text.
"""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# Artifact names: a project directory name or a single file name.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

EXCLUDED_DIRECTORIES = frozenset({
    ".venv", "venv", "env", ".env", "node_modules", "bower_components", "vendor", ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".cache", "cache",
    ".gradle", ".idea", ".vscode", "dist", "build", "target", "out", ".next", ".nuxt", "coverage",
    ".coverage", "htmlcov", "logs", "log", "tmp", "temp", ".terraform", ".huggingface", ".ollama",
    "site-packages", ".eggs", ".ipynb_checkpoints", "secrets", ".secrets", ".ssh", ".aws", ".gnupg",
})
EXCLUDED_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".crt", ".der", ".kdbx", ".pyc", ".pyo",
    ".so", ".dll", ".dylib", ".exe", ".log", ".tmp", ".swp", ".sqlite", ".sqlite3", ".db",
    ".safetensors", ".ckpt", ".pt", ".pth", ".onnx", ".gguf", ".bin", ".whl", ".egg-info",
)
EXCLUDED_FILES = frozenset({
    ".env", ".envrc", ".netrc", ".npmrc", ".pypirc", ".pgpass", ".git-credentials", ".dockercfg",
    "credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml",
    "service-account.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".ds_store", "thumbs.db",
})
# .env variants that are conventional, secret-free templates (still scanned for secrets).
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})

MEDIA_TYPES = {
    ".py": "text/x-python", ".md": "text/markdown", ".txt": "text/plain", ".rst": "text/plain",
    ".json": "application/json", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".toml": "application/toml", ".yaml": "application/yaml", ".yml": "application/yaml",
    ".ini": "text/plain", ".cfg": "text/plain", ".js": "text/javascript", ".mjs": "text/javascript",
    ".ts": "text/plain", ".tsx": "text/plain", ".jsx": "text/plain", ".html": "text/html", ".css": "text/css",
    ".sh": "text/x-shellscript", ".sql": "application/sql", ".xml": "application/xml", ".svg": "image/svg+xml",
}


@dataclass(frozen=True)
class ArtifactLimits:
    """Resource bounds (NEXUS_ARTIFACT_* settings)."""

    max_files: int = 200  # per artifact
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 20_000_000  # per artifact
    max_zip_bytes: int = 20_000_000
    max_path_length: int = 200
    max_path_depth: int = 10
    max_artifacts_per_task: int = 5


class UnsafeArtifactError(ValueError):
    """An artifact name, path or content breaks a safety rule. The message never contains
    file content."""


def media_type_for(path: str) -> str:
    return MEDIA_TYPES.get(PurePosixPath(path).suffix.lower(), "text/plain")


def check_name(name: str) -> str:
    """A safe artifact name (directory or file name). Returns it unchanged."""
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name) or name.endswith("."):
        raise UnsafeArtifactError(
            f"invalid artifact name {name[:80]!r}: use 1-64 letters, digits, '.', '_' or '-', not starting with '.'"
        )
    if name.lower().split(".")[0] in WINDOWS_RESERVED:
        raise UnsafeArtifactError(f"invalid artifact name {name!r}: reserved name")
    reason = excluded_reason(name)
    if reason:
        raise UnsafeArtifactError(f"artifact name {name!r} is not allowed: {reason}")
    return name


def normalize_path(path: str, limits: ArtifactLimits = ArtifactLimits()) -> str:
    """The canonical form of a relative path inside an artifact, or UnsafeArtifactError.
    Nothing is resolved against a filesystem: a path that is not already clean is refused
    rather than repaired."""
    if not isinstance(path, str) or not path:
        raise UnsafeArtifactError("empty path")
    shown = path[:120]
    if "\\" in path:
        raise UnsafeArtifactError(f"path {shown!r}: use '/' separators, not '\\'")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or path.startswith("~"):
        raise UnsafeArtifactError(f"path {shown!r}: absolute paths are not allowed")
    if any(ord(c) < 32 or c in '<>:"|?*' for c in path):
        raise UnsafeArtifactError(f"path {shown!r}: contains a control or reserved character")
    if len(path) > limits.max_path_length:
        raise UnsafeArtifactError(f"path {shown!r}...: longer than {limits.max_path_length} characters")
    parts = path.split("/")
    if len(parts) > limits.max_path_depth:
        raise UnsafeArtifactError(f"path {shown!r}: deeper than {limits.max_path_depth} levels")
    for part in parts:
        if part in ("", ".", ".."):
            raise UnsafeArtifactError(f"path {shown!r}: empty, '.' or '..' segments are not allowed")
        if part != part.strip() or part.endswith("."):
            raise UnsafeArtifactError(f"path {shown!r}: segments cannot start/end with spaces or end with '.'")
        if part.lower().split(".")[0] in WINDOWS_RESERVED:
            raise UnsafeArtifactError(f"path {shown!r}: {part!r} is a reserved name")
    reason = excluded_reason(path)
    if reason:
        raise UnsafeArtifactError(f"path {shown!r} is excluded from deliverables: {reason}")
    return path


def excluded_reason(path: str) -> str | None:
    """Why `path` may never be part of a deliverable, or None."""
    parts = [p.lower() for p in path.split("/")]
    for directory in parts[:-1]:
        if directory in EXCLUDED_DIRECTORIES or directory.endswith(".egg-info"):
            return f"'{directory}/' is a dependency, VCS, cache, build, log or secret directory"
    name = parts[-1]
    if name in ENV_TEMPLATES:
        return None
    if name in EXCLUDED_FILES or name.startswith(".env") or name.startswith("id_rsa") or name.startswith("id_ed25519"):
        return f"'{name}' is an environment, credential or key file"
    if name.endswith(EXCLUDED_SUFFIXES):
        return f"'{name}' has an excluded file type (keys, certificates, binaries, caches, logs, databases, model weights)"
    return None


def unique_paths(paths: list[str]) -> None:
    """Paths must be unique, also case-insensitively (artifacts are opened on Windows and
    macOS too), and no file may also be a directory of another path."""
    seen: dict[str, str] = {}
    for p in paths:
        key = p.lower()
        if key in seen:
            raise UnsafeArtifactError(f"path {p!r} duplicates {seen[key]!r}")
        seen[key] = p
    for p in paths:
        prefix = p.lower() + "/"
        clash = next((q for q in paths if q.lower().startswith(prefix)), None)
        if clash:
            raise UnsafeArtifactError(f"path {p!r} is a file but also a directory of {clash!r}")


# --- Secrets ------------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}")),
    ("OpenRouter API key", re.compile(r"\bsk-or-v1-[a-f0-9]{32,}")),
    ("Groq API key", re.compile(r"\bgsk_[A-Za-z0-9]{40,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Stripe secret key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("credentials in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]{6,}@", re.IGNORECASE)),
)
# name = "literal value" / name: 'literal' for secret-looking names.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?P<name>[a-z0-9_.-]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|
        access[_-]?token|refresh[_-]?token|private[_-]?key|client[_-]?secret|token)[a-z0-9_]*)\b
    ["']?\s*[:=]\s*(?P<quote>["'])(?P<value>[^"'\n]{8,})(?P=quote)
    """
)
_PLACEHOLDER = re.compile(
    r"(?i)^(?:x+|\*+|\.+|-+|<[^>]*>|\$\{[^}]*\}|\{\{[^}]*\}\}|%\([^)]*\)s|changeme|change[_-]?me|"
    r"your[_-].*|example.*|placeholder.*|dummy.*|test.*|sample.*|todo.*|replace.*|redacted.*|none|null)$"
)


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    kind: str

    def describe(self) -> str:
        return f"{self.path} line {self.line}: likely {self.kind}"


def find_secrets(path: str, text: str) -> list[SecretFinding]:
    """Likely secrets in `text`. Heuristic; findings never include the matched value."""
    findings: list[SecretFinding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(SecretFinding(path, number, kind))
        for match in _ASSIGNMENT.finditer(line):
            value = match.group("value").strip()
            if not _PLACEHOLDER.match(value) and not value.startswith(("os.environ", "process.env", "getenv")):
                findings.append(SecretFinding(path, number, f"hard-coded secret in '{match.group('name')[:40]}'"))
    return findings
