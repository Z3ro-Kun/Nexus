# NEXUS Artifact Model (Phase 10)

NEXUS can deliver generated files: a single file (`solution.py`), a multi-file project
(`my-project/`), and the project packaged as a ZIP (`my-project.zip`). Code references are
relative to `backend/app/`.

```
Intent -> plan -> parallel work -> shared state -> generation (artifact_write)
       -> validation + packaging (runtime) -> verification -> delivery check -> ready
```

## Two kinds of artifacts

| | text artifacts (Phase 3) | workspace artifacts (Phase 10) |
|---|---|---|
| produced by | an agent's report (`artifacts[]`) | the `artifact_write` tool |
| stored in | the event payload (`ArtifactAdded`, max 50 kB, text/markdown/JSON) | the artifact workspace; the log records paths, sizes and SHA-256 |
| state | `RunState.artifacts` | `RunState.workspace_artifacts` |
| delivered as | part of the result | download (`GET .../artifacts/{id}/download`) |

Both share one artifact-id namespace (the projector rejects a collision).

## Workspace (`artifacts/workspace.py`)

```
<NEXUS_ARTIFACT_ROOT>/<run_id>/files/<task_id>/file/<name>/<name>         single file
<NEXUS_ARTIFACT_ROOT>/<run_id>/files/<task_id>/project/<name>/<path...>   project files
<NEXUS_ARTIFACT_ROOT>/<run_id>/packages/<archive_id>.zip                  packaged project
```

Models never see or choose filesystem paths. They name an artifact and relative paths
inside it; every location is derived from ids NEXUS assigns. Before each read or write
the target is resolved and must stay inside its artifact directory, with no symbolic link
on the way. Writes are atomic (temporary file, then rename). Each task writes into its own
directory, so concurrent tasks never share files.

What a deliverable contains is decided by the event log (`ArtifactFileAdded`), never by
listing a directory: stray or stale files on disk are never packaged or delivered.

## The tool: `artifact_write` (`tools/artifact_write.py`)

```json
{"project": "my-project" | null, "files": [{"path": "src/main.py", "content": "..."}]}
```

- `project` set: files of a project (up to 50 per call; call again to add files or
  replace a path). `project` null: one file with a plain name, e.g. `solution.py`.
- **Permission:** an explicit tool permission (`NEXUS_SPECIALIST_TOOLS`, default
  `["calculator", "artifact_write"]`). Category `reversible_write`, so the policy engine
  decides whether agents get it at all (`NEXUS_POLICY_REVERSIBLE_WRITE`).
- **Agent-only:** never offered to the planner as an action; refused for action tasks.
- **Validates everything before writing anything.** A refusal fails the call with
  `artifact_rejected`, which ends the task; recovery can then replan (remediation).
- **Recorded without content:** `ToolCalled` stores each file's path, byte count and
  SHA-256 instead of its content (`ToolDefinition.record_arguments`).

## Safety rules (`artifacts/safety.py`, pure)

Applied by the tool, again by the runtime on the stored bytes, and by the projector to
paths in events.

- **Paths:** relative POSIX paths only. Rejected:
  - absolute paths, drive letters, `~`, backslashes;
  - `.`, `..` and empty segments, control or reserved characters;
  - Windows reserved names, segments that start or end with spaces or end with `.`.
  - Bounded length (`NEXUS_ARTIFACT_MAX_PATH_LENGTH`) and depth (`NEXUS_ARTIFACT_MAX_PATH_DEPTH`).
  - Unique case-insensitively; no file may also be a directory.
- **Never deliverable:** these are excluded even when listed in the manifest.
  - Directories: `.venv`, `venv`, `env`, `node_modules`, `vendor`, `.git`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.cache`, `cache`, `dist`, `build`, `target`, `out`, `.next`, `coverage`, `htmlcov`, `logs`, `log`, `tmp`, `temp`, `site-packages`, `*.egg-info`, `secrets`, `.ssh`, `.aws` and similar.
  - Files: `.env` and `.env.*`, except the templates `.env.example`, `.env.sample` and `.env.template`; `.netrc`, `.npmrc`, `.pypirc`, `credentials*`, `secrets.*`, `id_rsa*`, `id_ed25519*`.
  - Types: keys and certificates (`.pem`, `.key`, `.p12`, `.pfx`, `.crt`, ...); binaries, caches, logs, databases and model weights (`.pyc`, `.so`, `.exe`, `.log`, `.sqlite`, `.safetensors`, `.gguf`, `.bin`, ...).
- **Likely secrets** (heuristic, not a complete scanner):
  - private-key blocks;
  - AWS, GitHub, Anthropic, OpenAI-style, OpenRouter, Groq, Google, Slack and Stripe key formats;
  - JWTs and credentials embedded in URLs;
  - `password`/`secret`/`api_key`/`token` style assignments of literal values. Placeholders and environment lookups are allowed.

  Findings report the file, line and kind, never the value.
- **Limits** (configurable):

  | setting | default |
  |---|---|
  | `NEXUS_ARTIFACT_MAX_FILES` (per artifact) | 200 |
  | `NEXUS_ARTIFACT_MAX_FILE_BYTES` | 1 MB |
  | `NEXUS_ARTIFACT_MAX_TOTAL_BYTES` (per artifact) | 20 MB |
  | `NEXUS_ARTIFACT_MAX_ZIP_BYTES` | 20 MB |
  | `NEXUS_ARTIFACT_MAX_PATH_LENGTH` | 200 |
  | `NEXUS_ARTIFACT_MAX_PATH_DEPTH` | 10 |
  | `NEXUS_ARTIFACT_MAX_PER_TASK` | 5 |

NEXUS delivers project source, not environments: `requirements.txt` / `package.json`,
not installed packages. Generated code is never executed to verify it.

## Runtime: validation and packaging (`agents/runtime.py`)

When a task finishes successfully, the runtime:

1. Rebuilds each artifact from its own trace (the tool's validated outputs, last write of
   a path wins), not from the agent's report.
2. Re-reads every file from the workspace and checks its SHA-256, re-applies every rule
   above, and checks that the content is UTF-8.
3. For a project, builds a **deterministic ZIP**: sorted entries, fixed timestamps and
   modes, everything under `<project>/`. It reopens the ZIP and checks its CRCs, that the
   entry list exactly matches the manifest, and every entry's SHA-256.
4. Records `ArtifactCreated`, `ArtifactFileAdded`×n, `ArtifactValidated` and
   `ArtifactPackaged` atomically with `TaskCompleted`.

Any problem fails the task (`artifact_rejected`, a `VALIDATION_FAILURE`, replannable) and
nothing is packaged.

## Events

| event | written by | meaning |
|---|---|---|
| `ArtifactCreated` | runtime | file or project of the envelope's task; `tool_call_ids` (its writes), `input_fact_ids` (facts the task was given) |
| `ArtifactFileAdded` | runtime | one file: path, media type, size, SHA-256, tool call |
| `ArtifactValidated` | runtime / RunCompletion | passed (counts must match), or failed with `problems` (never content) |
| `ArtifactPackaged` | runtime | a new archive artifact: name, size, SHA-256 of the ZIP bytes, exact paths |
| `ArtifactReady` | RunCompletion | the bytes were re-checked after verification passed; only now downloadable |

All five are privileged: the raw events API cannot append them. The projector enforces:
- creation only in a task's context, citing that task's successful `artifact_write` calls and existing facts;
- safe, canonical, unique paths;
- validation counts that match the files;
- packaging only of a validated project, once, with exactly its paths;
- readiness only for a validated file or archive with the recorded checksum, in a run whose verification passed.

The log (metadata and checksums) reconstructs artifact state exactly; the bytes live in
the workspace and are verified against those checksums on every delivery.

## Verification and completion

- **Checkpoint check `artifacts`:** added when the covered work generated artifacts. Every one must be validated, and every project packaged.
- **Semantic verifier:** sees the generated artifacts' manifests (names, paths, sizes) and can cite their ids. It does not see file contents.
- **`completion_blockers`:** RunCompleted is refused while any artifact is rejected, unvalidated, unpackaged, or not ready.
- **`RunCompletion` delivery check:** when everything else allows completion, it re-reads each deliverable and checks it (SHA-256; archives are reopened and compared with the manifest).
  - All pass: `ArtifactReady`… then `RunCompleted`, in one append.
  - Any fails: `ArtifactValidated(passed=False, "delivery check: …")` and no completion.
  - With no workspace configured, nothing is recorded and the run waits.

## Delivery API (`api/v1/artifacts.py`)

| route | |
|---|---|
| `GET /api/v1/runs/{run_id}/artifacts` | every workspace artifact: name, type, status, `deliverable`, `verified`, media type, size, SHA-256, files, archive link, provenance, `download_url` |
| `GET /api/v1/runs/{run_id}/artifacts/{artifact_id}` | one artifact |
| `GET /api/v1/runs/{run_id}/artifacts/{artifact_id}/download` | the bytes of a ready file or archive |

Lookup happens only in that run's state, and the id format is validated.

The download response:
- re-verifies the SHA-256 on every request;
- is always sent as an attachment, with `X-Content-Type-Options: nosniff`, a sandboxing CSP and `Cache-Control: private, no-store`;
- carries the checksum in `Digest` and `X-Artifact-SHA256`;
- never contains workspace or filesystem paths.

| response | when |
|---|---|
| 404 `artifact_not_found` / `run_not_found` | unknown artifact or run, including an artifact of another run |
| 422 | malformed id |
| 409 `artifact_not_ready` | not ready, or a project (download its archive) |
| 410 `artifact_missing` | the stored bytes are gone |
| 500 `artifact_corrupted` | the bytes no longer match the checksum |
| 503 `artifacts_disabled` | no workspace configured |

NEXUS has no user accounts yet, so "belongs to the user" is enforced as "belongs to the
requested run".

## Limitations

- **Text files only (UTF-8).** Binary artifacts are not supported.
- **Disk cleanup:** files written by failed or replaced tasks stay on disk, but are never delivered. Nothing prunes the workspace yet.
- **Imperfect secret scanning:** heuristic patterns can miss secrets or flag look-alikes.
- **No code execution:** generated code is not run or tested; the semantic verifier judges from manifests, not file contents.
- **Local storage only:** no object storage, signed URLs or retention policy.
