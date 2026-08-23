"""Contract tests for structured orchestration results."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import get_args

import pytest

from labelkit.orchestration import (
    EstimateAssumption,
    JsonValueKind,
    RunArtifacts,
    RunEstimate,
    RunResult,
    RunSummary,
    SensitivePattern,
    TextFieldProfile,
    TextInputProfile,
    TextLengthProfile,
    ValidationResult,
    execute_project,
)
from labelkit.orchestration.orchestrator import RunSummary as LegacyRunSummary
from labelkit.orchestration.results import RunSummary as CanonicalRunSummary
from labelkit.orchestration.runtime import execute_project as RuntimeExecuteProject


def _summary() -> RunSummary:
    return RunSummary(
        counts={"scanned": 2, "emitted": 1},
        interrupted=False,
        exit_code=0,
        wall_s=0.25,
        output_lines=1,
        rejects_lines=1,
    )


def test_run_summary_keeps_legacy_import_identity_and_shape():
    assert RunSummary is CanonicalRunSummary is LegacyRunSummary
    assert [field.name for field in dataclasses.fields(RunSummary)] == [
        "counts", "interrupted", "exit_code", "wall_s",
        "output_lines", "rejects_lines",
    ]


def test_execute_project_is_exported_from_orchestration_package():
    assert execute_project is RuntimeExecuteProject


def test_run_artifacts_distinguish_produced_and_absent_channels(tmp_path):
    artifacts = RunArtifacts(
        report=tmp_path / "output.report.json",
        output=tmp_path / "output.jsonl",
        trace=tmp_path / "trace.jsonl",
    )

    assert artifacts.output == Path(tmp_path / "output.jsonl")
    assert artifacts.report == Path(tmp_path / "output.report.json")
    assert artifacts.rejects is None
    assert artifacts.sidecar is None
    assert artifacts.stream is None


def test_run_result_composes_identity_summary_and_artifacts(tmp_path):
    summary = _summary()
    artifacts = RunArtifacts(report=tmp_path / "output.report.json")

    result = RunResult(run_id="abcdef012345", summary=summary, artifacts=artifacts)

    assert result.run_id == "abcdef012345"
    assert result.summary is summary
    assert result.artifacts is artifacts


def test_validation_result_carries_config_and_all_diagnostics():
    result = ValidationResult(
        valid=False,
        config=None,
        errors=("project.toml:schema_version: expected 1, got 2",),
        warnings=("config.toml:[tool].future: unknown key",),
    )

    assert result.valid is False
    assert result.config is None
    assert len(result.errors) == len(result.warnings) == 1


def _estimate() -> RunEstimate:
    return RunEstimate(
        config_digest="sha256:config",
        project_digest="sha256:project",
        mode="process",
        modality="text",
        records=2,
        batches=1,
        calls={"generate_calls": 0, "quality_calls": 4},
        total_calls=4,
        assumptions=("excludes_retries_and_repairs",),
    )


def test_run_estimate_preserves_legacy_mapping_order():
    estimate = _estimate()

    assert estimate.as_legacy_mapping() == {
        "records": 2,
        "batches": 1,
        "generate_calls": 0,
        "quality_calls": 4,
        "total_calls": 4,
    }
    assert tuple(estimate.as_legacy_mapping()) == (
        "records", "batches", "generate_calls", "quality_calls", "total_calls",
    )


def test_estimate_assumption_vocabulary_is_closed():
    assert get_args(EstimateAssumption) == (
        "excludes_retries_and_repairs",
        "class_overrides_or_multi_label_lower_bound",
        "stream_downstream_sessions_lower_bound",
        "segment_worst_case_budget_upper_bound",
    )


def test_text_profile_vocabularies_and_shapes_are_closed():
    assert get_args(JsonValueKind) == (
        "null", "boolean", "integer", "number", "string", "array", "object",
    )
    assert get_args(SensitivePattern) == (
        "email_like", "phone_like", "cn_id_like", "credential_like",
    )
    assert [field.name for field in dataclasses.fields(TextFieldProfile)] == [
        "name", "present", "nulls", "kinds",
    ]
    assert [field.name for field in dataclasses.fields(TextLengthProfile)] == [
        "minimum", "maximum", "mean", "p50", "p95",
    ]
    assert [field.name for field in dataclasses.fields(TextInputProfile)] == [
        "config_digest", "project_digest", "text_field", "files",
        "estimated_lines", "sample_limit", "sampled_lines", "sampled_records",
        "bad_lines", "sample_complete", "fields", "fields_truncated",
        "text_lengths", "duplicate_texts", "duplicate_rate",
        "sensitive_record_counts",
    ]


@pytest.mark.parametrize("factory", [
    lambda tmp: _summary(),
    lambda tmp: TextFieldProfile("text", 1, 0, ("string",)),
    lambda tmp: TextLengthProfile(1, 1, 1.0, 1, 1),
    lambda tmp: RunArtifacts(report=tmp / "output.report.json"),
    lambda tmp: RunResult(
        run_id="abcdef012345",
        summary=_summary(),
        artifacts=RunArtifacts(report=tmp / "output.report.json"),
    ),
    lambda tmp: ValidationResult(valid=True, config=None),
    lambda tmp: _estimate(),
])
def test_result_contracts_are_frozen(factory, tmp_path):
    value = factory(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.unexpected = True
