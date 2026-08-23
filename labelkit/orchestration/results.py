"""Structured result contracts for library-level orchestration APIs.

The objects in this module contain data only.  They perform no I/O, derive no
paths and do not translate failures into CLI exit codes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig

EstimateAssumption = Literal[
    "excludes_retries_and_repairs",
    "class_overrides_or_multi_label_lower_bound",
    "stream_downstream_sessions_lower_bound",
    "segment_worst_case_budget_upper_bound",
]

__all__ = [
    "EstimateAssumption",
    "RunArtifacts",
    "RunEstimate",
    "RunResult",
    "RunSummary",
    "ValidationResult",
]


@dataclass(frozen=True)
class ValidationResult:
    """Complete, non-rendered diagnostics from validating one project."""

    valid: bool
    config: "ResolvedConfig | None"
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunEstimate:
    """Read-only record, batch and LLM-call estimate for one resolved project."""

    config_digest: str
    project_digest: str
    mode: Literal["process", "generate_only"]
    modality: Literal["text", "ui"]
    records: int
    batches: int
    calls: Mapping[str, int]
    total_calls: int
    assumptions: tuple[EstimateAssumption, ...]

    def as_legacy_mapping(self) -> dict[str, int]:
        """Return the frozen key order consumed by the existing dry-run UI."""
        return {
            "records": self.records,
            "batches": self.batches,
            **self.calls,
            "total_calls": self.total_calls,
        }


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
