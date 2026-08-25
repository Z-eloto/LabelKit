"""Offline contract tests for the read-only ``inspect_dataset`` Agent tool."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest

from labelkit.agent import (
    INSPECT_DATASET_BUDGET_RULE,
    INSPECT_DATASET_PATH_RULE,
    BudgetLimits,
    BudgetPolicy,
    PathPolicy,
    ToolCall,
    ToolRegistry,
    ToolRouter,
    inspect_dataset_spec,
    register_inspect_dataset,
)
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ConsoleConfig,
    DedupConfig,
    ExtractConfig,
    GenerateConfig,
    InputConfig,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.orchestration import (
    IntegerDistribution,
    StreamInputProfile,
    TextFieldProfile,
    TextInputProfile,
    TextLengthProfile,
    TimeRangeProfile,
    UIInputProfile,
    UIPairingProfile,
    profile_input,
)


def _config(tmp_path: Path, *, modality: str = "text") -> ResolvedConfig:
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={},
        embedding_profiles={},
        run=RunConfig(
            output=str(tmp_path / "out.jsonl"),
            modality=modality,
            input=str(tmp_path / "original-input"),
        ),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(),
        generate=GenerateConfig(),
        annotate=AnnotateConfig(),
        verify=VerifyConfig(),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
        rubric=Rubric(name="test", criteria=()),
        class_views={},
        user_schema={"type": "object"},
        limit=7,
        strict=False,
        dry_run=True,
        config_path="config.toml",
        project_path="project.toml",
        config_digest="sha256:config",
        project_digest="sha256:project",
    )


def _text_profile(*, mean: float = 4.0) -> TextInputProfile:
    return TextInputProfile(
        config_digest="sha256:config",
        project_digest="sha256:project",
        text_field="text",
        files=("data.jsonl",),
        estimated_lines=2,
        sample_limit=10,
        sampled_lines=2,
        sampled_records=2,
        bad_lines=0,
        sample_complete=True,
        fields=(TextFieldProfile("text", 2, 0, ("string",)),),
        fields_truncated=False,
        text_lengths=TextLengthProfile(4, 4, mean, 4, 4),
        duplicate_texts=1,
        duplicate_rate=0.5,
        sensitive_record_counts={
            "email_like": 0,
            "phone_like": 0,
            "cn_id_like": 0,
            "credential_like": 0,
        },
    )


def _ui_profile() -> UIInputProfile:
    distribution = IntegerDistribution(1, 3, 2.0, 2, 3)
    return UIInputProfile(
        config_digest="sha256:config",
        project_digest="sha256:project",
        files=("uitree_1.jsonl", "image_1.png"),
        sample_limit=10,
        sample_complete=True,
        pairing=UIPairingProfile(1, 1, 1, 0, 0, 0),
        tree_nodes=distribution,
        image_bytes=distribution,
    )


def _stream_profile() -> StreamInputProfile:
    distribution = IntegerDistribution(2, 2, 2.0, 2, 2)
    return StreamInputProfile(
        config_digest="sha256:config",
        project_digest="sha256:project",
        modality="text",
        files=("stream.jsonl",),
        estimated_frames=2,
        sample_limit=10,
        scanned_inputs=2,
        sampled_frames=2,
        bad_input=0,
        disorder=0,
        sample_complete=True,
        session_count=1,
        session_lengths=distribution,
        close_causes={"eof": 1},
        time_range=TimeRangeProfile("meta:time", 2, 1.0, 2.0, 1.0),
        pairing=None,
        tree_nodes=None,
        image_bytes=None,
    )


def _call(**overrides) -> ToolCall:
    arguments = {
        "input_path": "input",
        "modality": "text",
        "sample_limit": 10,
    }
    arguments.update(overrides)
    return ToolCall("call-1", "inspect_dataset", arguments, "inspect-1")


def _budget_policy() -> BudgetPolicy:
    return BudgetPolicy(
        BudgetLimits(
            max_cost_usd=Decimal("0"),
            max_tool_calls=2,
            max_pilot_runs=0,
            max_iterations=2,
            max_elapsed_s=60,
        ),
        rules={"inspect_dataset": INSPECT_DATASET_BUDGET_RULE},
    )


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def test_spec_is_r0_strict_bounded_and_returns_fresh_schemas():
    first = inspect_dataset_spec()
    first.arguments_schema["properties"]["sample_limit"]["maximum"] = 1
    second = inspect_dataset_spec()

    assert second.name == "inspect_dataset"
    assert second.risk == "R0"
    assert second.arguments_schema["additionalProperties"] is False
    assert second.arguments_schema["required"] == [
        "input_path", "modality", "sample_limit"]
    assert second.arguments_schema["properties"]["sample_limit"] == {
        "type": "integer", "minimum": 1, "maximum": 10_000}
    assert second.result_schema["additionalProperties"] is False
    assert second.result_schema["required"] == ["profile_type", "profile"]
    assert all(
        branch["then"]["properties"]["profile"]["additionalProperties"] is False
        for branch in second.result_schema["allOf"]
    )


def test_registration_is_explicit_and_rejects_invalid_base_without_mutation(tmp_path):
    registry = ToolRegistry()
    assert registry.specs() == ()

    with pytest.raises(TypeError, match="ResolvedConfig"):
        register_inspect_dataset(registry, object())
    generate_only = replace(
        _config(tmp_path), run=replace(_config(tmp_path).run, mode="generate_only"))
    with pytest.raises(ValueError, match="process-mode"):
        register_inspect_dataset(registry, generate_only)
    assert registry.specs() == ()

    register_inspect_dataset(registry, _config(tmp_path), profiler=lambda *_a, **_k: _text_profile())
    assert [spec.name for spec in registry.specs()] == ["inspect_dataset"]


def test_real_text_profile_is_content_free_read_only_and_uses_p1_once(
    tmp_path, monkeypatch, capsys,
):
    secret = "sk-" + "x" * 24
    private_text = f"contact alice@example.com token {secret}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "data.jsonl").write_text(
        "\n".join(json.dumps(row) for row in (
            {"text": private_text, "nullable": None},
            {"text": private_text, "nullable": "present"},
        )) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "labelkit.common.runtime.llm_client.LLMClient.__init__",
        lambda *_a, **_k: pytest.fail("inspect must not construct LLMClient"),
    )
    monkeypatch.setattr(
        "labelkit.operators.emitter.Emitter.__init__",
        lambda *_a, **_k: pytest.fail("inspect must not construct Emitter"),
    )
    base = _config(tmp_path)
    received = []

    def spy(cfg, *, sample_limit):
        received.append((cfg, sample_limit))
        return profile_input(cfg, sample_limit=sample_limit)

    registry = ToolRegistry()
    register_inspect_dataset(registry, base, profiler=spy)
    path_policy = PathPolicy(
        base_dir=tmp_path,
        allowed_reads=(input_dir,),
        rules={"inspect_dataset": INSPECT_DATASET_PATH_RULE},
    )
    budget_policy = _budget_policy()
    result = ToolRouter(
        registry, policies=(path_policy, budget_policy)).route(_call())

    assert result.status == "success"
    assert result.output["profile_type"] == "text"
    profile = result.output["profile"]
    assert profile["estimated_lines"] == profile["sampled_records"] == 2
    assert profile["duplicate_texts"] == 1
    assert profile["sensitive_record_counts"]["email_like"] == 2
    assert received[0][1] == 10
    assert received[0][0].run.input == str(input_dir.resolve())
    assert received[0][0].limit is None
    assert received[0][0].dry_run is False
    assert base.run.input == str(tmp_path / "original-input")
    assert base.limit == 7 and base.dry_run is True
    assert budget_policy.snapshot().tool_calls == 1

    rendered = json.dumps(result.output, ensure_ascii=False)
    assert private_text not in rendered
    assert secret not in rendered
    assert "alice@example.com" not in rendered
    assert capsys.readouterr() == ("", "")
    assert not (tmp_path / "out.jsonl").exists()
    assert not (tmp_path / "out.report.json").exists()
    assert not (tmp_path / "out.trace.jsonl").exists()


def test_path_denial_precedes_budget_claim_and_profiler(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    calls = []
    registry = ToolRegistry()
    register_inspect_dataset(
        registry, _config(tmp_path),
        profiler=lambda *_a, **_k: calls.append(True) or _text_profile(),
    )
    path_policy = PathPolicy(
        base_dir=tmp_path,
        allowed_reads=(allowed,),
        rules={"inspect_dataset": INSPECT_DATASET_PATH_RULE},
    )
    budget_policy = _budget_policy()

    result = ToolRouter(registry, policies=(path_policy, budget_policy)).route(
        _call(input_path="outside"))

    assert result.error.kind == "policy_denied"
    assert result.error.details == {"rule": "read_scope"}
    assert calls == []
    assert budget_policy.snapshot().tool_calls == 0


@pytest.mark.parametrize("arguments", [
    {"input_path": "input", "modality": "audio", "sample_limit": 10},
    {"input_path": "input", "modality": "text", "sample_limit": 0},
    {"input_path": "input", "modality": "text", "sample_limit": 10_001},
    {"input_path": "input", "modality": "text", "sample_limit": 10, "raw": True},
    {"input_path": "input", "modality": "text"},
])
def test_invalid_arguments_never_reach_profiler(tmp_path, arguments):
    calls = []
    registry = ToolRegistry()
    register_inspect_dataset(
        registry, _config(tmp_path),
        profiler=lambda *_a, **_k: calls.append(True) or _text_profile(),
    )

    result = ToolRouter(registry).route(
        ToolCall("call-1", "inspect_dataset", arguments, "inspect-1"))

    assert result.error.kind == "invalid_arguments"
    assert calls == []


def test_modality_mismatch_and_profile_failure_are_sanitized(tmp_path):
    calls = []
    registry = ToolRegistry()
    register_inspect_dataset(
        registry, _config(tmp_path),
        profiler=lambda *_a, **_k: calls.append(True) or _text_profile(),
    )
    mismatch = ToolRouter(registry).route(_call(modality="ui"))
    assert mismatch.error.kind == "execution_failed"
    assert mismatch.error.details == {"exception_type": "ValueError"}
    assert calls == []

    failing_registry = ToolRegistry()

    def fail(*_args, **_kwargs):
        raise RuntimeError("private path and secret")

    register_inspect_dataset(failing_registry, _config(tmp_path), profiler=fail)
    failed = ToolRouter(failing_registry).route(_call())
    assert failed.error.kind == "execution_failed"
    assert failed.error.details == {"exception_type": "RuntimeError"}
    assert "private" not in repr(failed)
    assert "secret" not in repr(failed)


@pytest.mark.parametrize(("profile", "profile_type"), [
    (_text_profile(), "text"),
    (_ui_profile(), "ui"),
    (_stream_profile(), "stream"),
])
def test_all_p1_profile_contracts_are_serialized_without_reimplementation(
    tmp_path, profile, profile_type,
):
    base = _config(tmp_path, modality=profile.modality if profile_type == "stream" else profile_type)
    registry = ToolRegistry()
    register_inspect_dataset(
        registry, base, profiler=lambda *_a, **_k: profile)

    result = ToolRouter(registry).route(_call(modality=base.run.modality))

    assert result.status == "success"
    assert result.output == {
        "profile_type": profile_type,
        "profile": _json_copy(asdict(profile)),
    }


def test_unsupported_or_non_json_profile_never_crosses_router(tmp_path):
    registry = ToolRegistry()
    register_inspect_dataset(
        registry, _config(tmp_path), profiler=lambda *_a, **_k: object())
    unsupported = ToolRouter(registry).route(_call())
    assert unsupported.error.kind == "execution_failed"
    assert unsupported.error.details == {"exception_type": "TypeError"}

    nan_registry = ToolRegistry()
    register_inspect_dataset(
        nan_registry, _config(tmp_path),
        profiler=lambda *_a, **_k: _text_profile(mean=float("nan")),
    )
    invalid = ToolRouter(nan_registry).route(_call())
    assert invalid.error.kind == "invalid_result"
    assert invalid.output is None
