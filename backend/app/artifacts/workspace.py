"""The controlled artifact workspace: the only place generated files are written to, and the
only code that reads them back for delivery.

    <root>/<run_id>/files/<task_id>/file/<name>/<name>           a single-file artifact
    <root>/<run_id>/files/<task_id>/project/<name>/<path...>     a project's files
    <root>/<run_id>/packages/<archive_id>.zip                    a packaged project

`<root>` is NEXUS_ARTIFACT_ROOT. Models never see or choose a filesystem path: they name an
artifact and relative paths inside it, which `app.artifacts.safety` validates; every
location is derived here from ids NEXUS assigns. Before any read or write, the target is
resolved and must stay inside its artifact directory, with no symlink on the way.

What belongs to a deliverable is decided by the event log (ArtifactFileAdded), never by
listing a directory: a stale or stray file on disk can never be delivered. Every read for
delivery checks the bytes against the SHA-256 recorded in the log.
"""

import hashlib
import io
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Literal
from uuid import UUID

from app.artifacts.safety import ArtifactLimits, UnsafeArtifactError, check_name, excluded_reason, normalize_path

Kind = Literal["file", "project"]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Fixed metadata so the same files always give byte-identical archives.
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o100644 << 16


class ArtifactIntegrityError(Exception):
    """Stored bytes are missing or no longer match the recorded checksum."""

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.missing = missing


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_id(value: str, what: str) -> str:
    if not _ID.fullmatch(value) or ".." in value:
        raise UnsafeArtifactError(f"invalid {what} {value[:80]!r}")
    return value


class ArtifactWorkspace:
    def __init__(self, root: Path | str, limits: ArtifactLimits = ArtifactLimits()) -> None:
        self.root = Path(root).resolve()
        self.limits = limits

    # --- locations ---------------------------------------------------------------------

    def _run_dir(self, run_id: UUID) -> Path:
        return self.root / str(UUID(str(run_id)))

    def artifact_dir(self, run_id: UUID, task_id: str, kind: Kind, name: str) -> Path:
        if kind not in ("file", "project"):
            raise UnsafeArtifactError(f"invalid artifact kind {kind!r}")
        return self._run_dir(run_id) / "files" / _safe_id(task_id, "task id") / kind / check_name(name)

    def file_location(self, run_id: UUID, task_id: str, kind: Kind, name: str, path: str) -> Path:
        base = self.artifact_dir(run_id, task_id, kind, name)
        target = base.joinpath(*normalize_path(path, self.limits).split("/"))
        self._contained(target, base)
        return target

    def package_location(self, run_id: UUID, archive_id: str) -> Path:
        base = self._run_dir(run_id) / "packages"
        target = base / f"{_safe_id(archive_id, 'archive id')}.zip"
        self._contained(target, base)
        return target

    def _contained(self, target: Path, base: Path) -> None:
        """`target` must resolve inside `base` (itself inside the root), with no symlink
        between the root and the target."""
        if not base.resolve().is_relative_to(self.root) or not target.resolve().is_relative_to(base.resolve()):
            raise UnsafeArtifactError("path escapes the artifact workspace")
        current = self.root
        for part in target.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise UnsafeArtifactError("a symbolic link in the artifact workspace was refused")

    # --- writing -----------------------------------------------------------------------

    def existing_files(self, run_id: UUID, task_id: str, kind: Kind, name: str) -> dict[str, int]:
        """Files already written for this artifact: relative path -> size."""
        base = self.artifact_dir(run_id, task_id, kind, name)
        if not base.is_dir():
            return {}
        return {
            p.relative_to(base).as_posix(): p.stat().st_size
            for p in base.rglob("*")
            if p.is_file() and not p.is_symlink() and not p.name.startswith(".nexus-tmp")
        }

    def artifacts_of_task(self, run_id: UUID, task_id: str) -> set[tuple[str, str]]:
        """(kind, name) of every artifact this task has written."""
        base = self._run_dir(run_id) / "files" / _safe_id(task_id, "task id")
        return {(k.name, a.name) for k in base.iterdir() if k.is_dir() for a in k.iterdir() if a.is_dir()} if base.is_dir() else set()

    def write(self, run_id: UUID, task_id: str, kind: Kind, name: str, path: str, data: bytes) -> Path:
        target = self.file_location(run_id, task_id, kind, name, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._contained(target, self.artifact_dir(run_id, task_id, kind, name))
        _atomic_write(target, data)
        return target

    # --- reading -----------------------------------------------------------------------

    def read_file(self, run_id: UUID, task_id: str, kind: Kind, name: str, path: str, expected_sha256: str) -> bytes:
        return _read_verified(self.file_location(run_id, task_id, kind, name, path), expected_sha256, path)

    def read_package(self, run_id: UUID, archive_id: str, expected_sha256: str) -> bytes:
        return _read_verified(self.package_location(run_id, archive_id), expected_sha256, f"{archive_id}.zip")

    # --- packaging ---------------------------------------------------------------------

    def package(self, run_id: UUID, archive_id: str, root_name: str, files: dict[str, bytes]) -> bytes:
        """Write a deterministic ZIP of exactly `files` (relative path -> bytes) under the
        folder `root_name/`, verify it, and return its bytes."""
        check_name(root_name)
        data = build_zip(root_name, files)
        if len(data) > self.limits.max_zip_bytes:
            raise UnsafeArtifactError(f"archive is {len(data)} bytes; the limit is {self.limits.max_zip_bytes}")
        verify_zip(data, root_name, {p: sha256(b) for p, b in files.items()})
        target = self.package_location(run_id, archive_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, data)
        return data


def build_zip(root_name: str, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(f"{root_name}/{path}", date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = _ZIP_FILE_MODE
            archive.writestr(info, files[path], compresslevel=9)
    return buffer.getvalue()


def verify_zip(data: bytes, root_name: str, expected: dict[str, str]) -> None:
    """The archive opens, passes its CRC checks, and contains exactly the expected files
    (relative path -> SHA-256) under `root_name/`, and nothing excluded or unsafe."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.testzip() is not None:
                raise UnsafeArtifactError("archive failed its CRC check")
            names = archive.namelist()
            prefix = f"{root_name}/"
            if len(names) != len(set(names)) or any(not n.startswith(prefix) for n in names):
                raise UnsafeArtifactError("archive entries are duplicated or outside the project folder")
            relative = [n[len(prefix):] for n in names]
            for path in relative:
                normalize_path(path)  # no absolute, '..' or excluded entries
                if excluded_reason(path):
                    raise UnsafeArtifactError(f"archive contains an excluded path {path!r}")
            if sorted(relative) != sorted(expected):
                raise UnsafeArtifactError("archive contents differ from the project manifest")
            for path in relative:
                if sha256(archive.read(prefix + path)) != expected[path]:
                    raise UnsafeArtifactError(f"archive entry {path!r} does not match its recorded checksum")
    except zipfile.BadZipFile as exc:
        raise UnsafeArtifactError(f"archive cannot be opened: {exc}") from exc


def _atomic_write(target: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".nexus-tmp-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_verified(location: Path, expected_sha256: str, label: str) -> bytes:
    if location.is_symlink() or not location.is_file():
        raise ArtifactIntegrityError(f"{label} is missing from the artifact workspace", missing=True)
    data = location.read_bytes()
    if sha256(data) != expected_sha256:
        raise ArtifactIntegrityError(f"{label} does not match its recorded checksum")
    return data
