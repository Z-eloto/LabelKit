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
JsonValueKind = Literal["null", "boolean", "integer", "number", "string", "array", "object"]
SensitivePattern = Literal["email_like", "phone_like", "cn_id_like", "credential_like"]
SessionCloseCause = Literal["gap", "key", "max_len", "max_span", "eof", "limit"]
__all__ = [
    "EstimateAssumption",
    "JsonValueKind",
    "IntegerDistribution",
    "InputProfile",
    "RunArtifacts",
    "RunEstimate",
    "RunResult",
    "RunSummary",
    "SensitivePattern",
    "SessionCloseCause",
    "StreamInputProfile",
    "TextFieldProfile",
    "TextInputProfile",
    "TextLengthProfile",
    "TimeRangeProfile",
    "UIInputProfile",
    "UIPairingProfile",
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


@dataclass(frozen=True)
class TextFieldProfile:
    """Shape-only statistics for one top-level JSON field in the sample."""

    name: str
    present: int
    nulls: int
    kinds: tuple[JsonValueKind, ...]


@dataclass(frozen=True)
class TextLengthProfile:
    """Character-length distribution of extracted text in the sample."""

    minimum: int
    maximum: int
    mean: float
    p50: int
    p95: int


@dataclass(frozen=True)
class TextInputProfile:
    """Bounded, content-free profile of a text JSONL input."""

    config_digest: str
    project_digest: str
    text_field: str
    files: tuple[str, ...]
    estimated_lines: int
    sample_limit: int
    sampled_lines: int
    sampled_records: int
    bad_lines: int
    sample_complete: bool
    fields: tuple[TextFieldProfile, ...]
    fields_truncated: bool
    text_lengths: TextLengthProfile
    duplicate_texts: int
    duplicate_rate: float
    sensitive_record_counts: Mapping[SensitivePattern, int]


@dataclass(frozen=True)
class IntegerDistribution:
    """Aggregate distribution for bounded, non-negative integer samples."""

    minimum: int
    maximum: int
    mean: float
    p50: int
    p95: int


@dataclass(frozen=True)
class UIPairingProfile:
    """Visited UI pair/index counts without per-index locations."""

    estimated_pairs: int
    scanned_indices: int
    sampled_pairs: int
    bad_pairs: int
    missing_pairs: int
    index_conflicts: int


@dataclass(frozen=True)
class UIInputProfile:
    """Bounded, content-free profile of paired UI tree/image input."""

    config_digest: str
    project_digest: str
    files: tuple[str, ...]
    sample_limit: int
    sample_complete: bool
    pairing: UIPairingProfile
    tree_nodes: IntegerDistribution
    image_bytes: IntegerDistribution


@dataclass(frozen=True)
class TimeRangeProfile:
    """Parsed time-key coverage and range for a stream sample."""

    order_by: str
    parsed_frames: int
    minimum_epoch_s: float | None
    maximum_epoch_s: float | None
    span_s: float | None


@dataclass(frozen=True)
class StreamInputProfile:
    """Bounded profile produced by the execution session state machine."""

    config_digest: str
    project_digest: str
    modality: Literal["text", "ui"]
    files: tuple[str, ...]
    estimated_frames: int
    sample_limit: int
    scanned_inputs: int
    sampled_frames: int
    bad_input: int
    disorder: int
    sample_complete: bool
    session_count: int
    session_lengths: IntegerDistribution | None
    close_causes: Mapping[SessionCloseCause, int]
    time_range: TimeRangeProfile | None
    pairing: UIPairingProfile | None
    tree_nodes: IntegerDistribution | None
    image_bytes: IntegerDistribution | None


InputProfile = TextInputProfile | UIInputProfile | StreamInputProfile


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
