"""Owned, atomically published filesystem workspaces for Agent runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CANDIDATE_ID_RE = re.compile(r"^candidate-[0-9]{3,6}$")
_SCHEMA_VERSION = 1
_MAX_METADATA_BYTES = 4096


class WorkspaceError(RuntimeError):
    """Base class for expected Agent workspace failures."""


class WorkspaceExistsError(WorkspaceError):
    """The requested run or candidate identity has already been claimed."""


class WorkspaceIntegrityError(WorkspaceError):
    """An existing workspace does not match its ownership contract."""


@dataclass(frozen=True, slots=True)
class CandidateWorkspace:
    agent_run_id: str
    candidate_id: str
    root: Path
    run_dir: Path


@dataclass(frozen=True, slots=True)
class AgentWorkspace:
    agent_run_id: str
    root: Path
    source_dir: Path
    candidates_dir: Path

    @classmethod
    def create(cls, project_root: str | Path, agent_run_id: str) -> AgentWorkspace:
        """Create and publish one new ``out/agent/<run_id>`` workspace."""
        run_id = _validated_id(agent_run_id, _RUN_ID_RE, "agent_run_id")
        agent_root = _agent_root(project_root, create=True)
        root = _atomic_directory(
            agent_root,
            run_id,
            marker=".workspace.json",
            metadata=_workspace_metadata(run_id),
            directories=("source", "candidates"),
        )
        return cls(run_id, root, root / "source", root / "candidates")

    @classmethod
    def open(cls, project_root: str | Path, agent_run_id: str) -> AgentWorkspace:
        """Open an existing workspace only after validating ownership and layout."""
        run_id = _validated_id(agent_run_id, _RUN_ID_RE, "agent_run_id")
        root = _agent_root(project_root, create=False) / run_id
        _verify_directory(
            root,
            marker=".workspace.json",
            metadata=_workspace_metadata(run_id),
            directories=("source", "candidates"),
        )
        return cls(run_id, root, root / "source", root / "candidates")

    def create_candidate(self, candidate_id: str) -> CandidateWorkspace:
        """Atomically allocate one immutable candidate identity and its run directory."""
        self._verify()
        selected = _validated_id(candidate_id, _CANDIDATE_ID_RE, "candidate_id")
        root = _atomic_directory(
            self.candidates_dir,
            selected,
            marker=".candidate.json",
            metadata=_candidate_metadata(self.agent_run_id, selected),
            directories=("run",),
        )
        return CandidateWorkspace(self.agent_run_id, selected, root, root / "run")

    def open_candidate(self, candidate_id: str) -> CandidateWorkspace:
        selected = _validated_id(candidate_id, _CANDIDATE_ID_RE, "candidate_id")
        self._verify()
        root = self.candidates_dir / selected
        _verify_directory(
            root,
            marker=".candidate.json",
            metadata=_candidate_metadata(self.agent_run_id, selected),
            directories=("run",),
        )
        return CandidateWorkspace(self.agent_run_id, selected, root, root / "run")

    def _verify(self) -> None:
        if self.root.name != self.agent_run_id:
            raise WorkspaceIntegrityError("workspace identity does not match its path")
        if self.root.parent.name != "agent" or self.root.parent.parent.name != "out":
            raise WorkspaceIntegrityError("workspace is outside the out/agent layout")
        if self.source_dir != self.root / "source" or self.candidates_dir != self.root / "candidates":
            raise WorkspaceIntegrityError("workspace paths do not match the owned layout")
        _verify_directory(
            self.root,
            marker=".workspace.json",
            metadata=_workspace_metadata(self.agent_run_id),
            directories=("source", "candidates"),
        )


def _agent_root(project_root: str | Path, *, create: bool) -> Path:
    raw = Path(project_root)
    absolute = raw if raw.is_absolute() else Path.cwd() / raw
    if any(part.casefold().rstrip(" .") in {".git", ".ssh", ".aws"}
           for part in absolute.parts):
        raise WorkspaceIntegrityError("project path contains a forbidden component")
    if _has_link_component(absolute):
        raise WorkspaceIntegrityError("project path contains a linked component")
    try:
        project = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceIntegrityError("project root does not exist") from exc
    if not project.is_dir():
        raise WorkspaceIntegrityError("project root is not a directory")

    out_root = project / "out"
    agent_root = out_root / "agent"
    if create and _has_link_component(out_root):
        raise WorkspaceIntegrityError("Agent output parent contains a linked component")
    if create:
        try:
            out_root.mkdir(mode=0o700, exist_ok=True)
            agent_root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError("cannot create the Agent workspace parent") from exc
    if _has_link_component(agent_root) or not agent_root.is_dir():
        raise WorkspaceIntegrityError("Agent workspace parent is missing or linked")
    return agent_root.resolve(strict=True)


def _atomic_directory(
    parent: Path,
    name: str,
    *,
    marker: str,
    metadata: Mapping[str, object],
    directories: tuple[str, ...],
) -> Path:
    if _has_link_component(parent) or not parent.is_dir():
        raise WorkspaceIntegrityError("workspace parent is missing or linked")
    target = parent / name
    claim = parent / f".{name}.claim"
    if target.exists() or target.is_symlink():
        raise WorkspaceExistsError("workspace identity already exists")

    try:
        claim_fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise WorkspaceExistsError("workspace identity is already being created") from exc
    except OSError as exc:
        raise WorkspaceError("cannot claim workspace identity") from exc

    staging: Path | None = None
    try:
        os.close(claim_fd)
        if target.exists() or target.is_symlink():
            raise WorkspaceExistsError("workspace identity already exists")
        staging = Path(tempfile.mkdtemp(prefix=f".{name}.", suffix=".part", dir=parent))
        for directory in directories:
            (staging / directory).mkdir(mode=0o700)
        _write_metadata(staging / marker, metadata)
        _publish_directory(staging, target)
        staging = None
        return target
    except WorkspaceError:
        raise
    except OSError as exc:
        raise WorkspaceError("cannot initialize workspace directory") from exc
    finally:
        if staging is not None:
            _cleanup_staging(staging, parent, name)
        claim.unlink(missing_ok=True)


def _write_metadata(path: Path, metadata: Mapping[str, object]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(metadata, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _publish_directory(staging: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise WorkspaceExistsError("workspace identity already exists")
    staging.rename(target)


def _cleanup_staging(staging: Path, parent: Path, name: str) -> None:
    if staging.parent != parent or not staging.name.startswith(f".{name}."):
        raise WorkspaceIntegrityError("refusing to clean an unowned staging directory")
    if not staging.exists():
        return
    if _has_link_component(staging):
        raise WorkspaceIntegrityError("refusing to clean a linked staging directory")
    shutil.rmtree(staging)


def _verify_directory(
    root: Path,
    *,
    marker: str,
    metadata: Mapping[str, object],
    directories: tuple[str, ...],
) -> None:
    if _has_link_component(root) or not root.is_dir():
        raise WorkspaceIntegrityError("workspace directory is missing or linked")
    marker_path = root / marker
    if _has_link_component(marker_path) or not marker_path.is_file():
        raise WorkspaceIntegrityError("workspace ownership marker is missing or linked")
    try:
        if marker_path.stat().st_size > _MAX_METADATA_BYTES:
            raise WorkspaceIntegrityError("workspace ownership marker is too large")
        observed = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceIntegrityError("workspace ownership marker is invalid") from exc
    if observed != metadata:
        raise WorkspaceIntegrityError("workspace ownership marker does not match")
    for directory in directories:
        child = root / directory
        if _has_link_component(child) or not child.is_dir():
            raise WorkspaceIntegrityError("workspace owned directory is missing or linked")


def _validated_id(value: str, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _workspace_metadata(agent_run_id: str) -> dict[str, object]:
    return {"agent_run_id": agent_run_id, "kind": "agent_workspace",
            "schema_version": _SCHEMA_VERSION}


def _candidate_metadata(agent_run_id: str, candidate_id: str) -> dict[str, object]:
    return {"agent_run_id": agent_run_id, "candidate_id": candidate_id,
            "kind": "agent_candidate", "schema_version": _SCHEMA_VERSION}


def _has_link_component(path: Path) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            return True
        if getattr(metadata, "st_file_attributes", 0) & reparse_flag:
            return True
    return False


__all__ = [
    "AgentWorkspace", "CandidateWorkspace", "WorkspaceError",
    "WorkspaceExistsError", "WorkspaceIntegrityError",
]
