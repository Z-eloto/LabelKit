"""Structured result contracts for library-level orchestration APIs.

The objects in this module describe completed work only.  They perform no I/O,
derive no paths and do not translate failures into CLI exit codes.  Runtime API
adapters populate them in later Phase 1 slices.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig

__all__ = ["RunArtifacts", "RunResult", "RunSummary", "ValidationResult"]


@dataclass(frozen=True)
class ValidationResult:
    """Complete, non-rendered diagnostics from validating one project."""

    valid: bool
    config: "ResolvedConfig | None"
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)  # Existing shape, moved from orchestrator.py in P1.1.
class RunSummary:
    """Deterministic summary of a completed orchestration run."""

    counts: Mapping
    interrupted: bool
    exit_code: int
    wall_s: float
    output_lines: int
    rejects_lines: int


@dataclass(frozen=True)
class RunArtifacts:
    """Paths actually produced by a run.

    ``report`` is required because successful real and dry runs both produce a
    report.  Optional paths are ``None`` when their channel was disabled or did
    not produce a deliverable; callers must not infer enablement from a guessed
    filename.
    """

    report: Path
    output: Path | None = None
    rejects: Path | None = None
    sidecar: Path | None = None
    trace: Path | None = None
    stream: Path | None = None


@dataclass(frozen=True)
class RunResult:
    """Library-facing result of one run, including identity and artifacts."""

    run_id: str
    summary: RunSummary
    artifacts: RunArtifacts
