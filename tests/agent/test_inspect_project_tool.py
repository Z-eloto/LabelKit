"""Offline contracts for the deidentified ``inspect_project`` Agent tool."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from labelkit.agent import (
    INSPECT_PROJECT_BUDGET_RULE,
    INSPECT_PROJECT_PATH_RULE,
    BudgetLimits,
    BudgetPolicy,
    PathPolicy,
    ToolCall,
    ToolRegistry,
    ToolRouter,
    inspect_project_spec,
    register_inspect_project,
)
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ConsoleConfig,
    Criterion,
    DedupConfig,
    EmbeddingProfile,
    ExtractConfig,
    GenerateConfig,
    GenerateStreamConfig,
    GenerateTimeProfile,
    InputConfig,
    LLMProfile,
    OutputConfig,
    QualityConfig,
    ResolvedConfig,
    Rubric,
    RunConfig,
    SegmentConfig,
    SequenceRuleSpec,
    SequenceWindowSpec,
    StitchConfig,
    StreamConfig,
    TierSpec,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)

PRIVATE = "private-value-that-must-not-cross-the-tool-boundary"


def _profile(name: str, *, priced: bool = False) -> LLMProfile:
    return LLMProfile(
        name=name,
        provider="openai_compatible",
        base_url=f"https://{PRIVATE}.example/v1",
        model=f"model-{PRIVATE}",
        api_key_env=f"ENV_{PRIVATE}",
        api_key=f"sk-{PRIVATE}",
        api_key_envs=(f"ENV_{PRIVATE}",),
        api_keys=(f"sk-{PRIVATE}",),
        price_per_mtok_in=1.0 if priced else None,
        price_per_mtok_out=2.0 if priced else None,
    )


def _config(tmp_path: Path, *, profile_name: str = "planner_profile") -> ResolvedConfig:
    config_path = tmp_path / "config.toml"
    project_path = tmp_path / "project.toml"
    config_path.write_text("# validated before Agent startup\n", encoding="utf-8")
    project_path.write_text("# validated before Agent startup\n", encoding="utf-8")
    return ResolvedConfig(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={profile_name: _profile(profile_name)},
        embedding_profiles={
            "embedding_profile": EmbeddingProfile(
                name="embedding_profile",
                base_url=f"https://embedding-{PRIVATE}.example/v1",
                model=f"embedding-{PRIVATE}",
                api_key_env=f"EMBED_{PRIVATE}",
                api_key=f"sk-embedding-{PRIVATE}",
            ),
        },
        run=RunConfig(
            output=str(tmp_path / PRIVATE / "out.jsonl"),
            modality="text",
            input=None,
            mode="generate_only",
            fatal_error_threshold=17,
        ),
        input=InputConfig(text_field=PRIVATE),
        stream=StreamConfig(
            order_by=f"meta:{PRIVATE}", key=(f"meta:{PRIVATE}",),
            gap_s=90, session_max_span_s=600),
        dedup=DedupConfig(
            semantic=True, semantic_embedding="embedding_profile",
            minhash_threshold=0.81, semantic_threshold=0.93),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
        classify=ClassifyConfig(),
        quality=QualityConfig(enabled=False),
        generate=GenerateConfig(
            enabled=True,
            llms=(profile_name,),
            instruction=PRIVATE,
            sample_validator=f"{PRIVATE}:sample",
            sequence_validator=f"{PRIVATE}:sequence",
            sequences=7,
            time_profiles=(GenerateTimeProfile(PRIVATE, 1, 0, 60, PRIVATE),),
        ),
        annotate=AnnotateConfig(enabled=False, instruction=PRIVATE),
        verify=VerifyConfig(),
        output=OutputConfig(
            schema_inline=PRIVATE,
            validator=f"{PRIVATE}:output",
            passthrough_fields=(PRIVATE,),
            rejects="full",
        ),
        trace=TraceConfig(enabled=True, path=str(tmp_path / PRIVATE), content="full"),
        rubric=Rubric(name=PRIVATE, criteria=(
            Criterion("private", PRIVATE, PRIVATE),)),
        class_views={},
        user_schema={
            "type": "object",
            "properties": {PRIVATE: {"type": "string"}},
            "required": [PRIVATE],
            "additionalProperties": False,
        },
        limit=None,
        strict=False,
        dry_run=False,
        config_path=str(config_path),
        project_path=str(project_path),
        config_digest="sha256:config-safe-digest",
        project_digest="sha256:project-safe-digest",
        generate_stream=GenerateStreamConfig(
            enabled=True,
            sessions=2,
            duplicates=1,
            tiers=(TierSpec(1, 1, ("frame",)),),
            rules=(SequenceRuleSpec("existence", frame_class="frame"),),
            windows=(SequenceWindowSpec("frame", (("00:00", "01:00"),)),),
        ),
    )


def _call(**overrides) -> ToolCall:
    arguments = {"config_path": "config.toml", "project_path": "project.toml"}
    arguments.update(overrides)
    return ToolCall("call-project", "inspect_project", arguments, "inspect-project-1")


def _budget() -> BudgetPolicy:
    return BudgetPolicy(
        BudgetLimits(
            max_cost_usd=Decimal("0"), max_tool_calls=2, max_pilot_runs=0,
            max_iterations=2, max_elapsed_s=60),
        rules={"inspect_project": INSPECT_PROJECT_BUDGET_RULE},
    )


def test_spec_is_fresh_r0_strict_and_closes_nested_summary_fields():
    first = inspect_project_spec()
    first.arguments_schema["properties"]["config_path"]["minLength"] = 99
    first.result_schema["properties"]["referenced_llm_profiles"]["items"]["maxLength"] = 1
    assert first.result_schema["properties"]["referenced_embedding_profiles"]["items"][
        "maxLength"] == 128
    second = inspect_project_spec()

    assert second.name == "inspect_project"
    assert second.risk == "R0"
    assert second.arguments_schema["required"] == ["config_path", "project_path"]
    assert second.arguments_schema["additionalProperties"] is False
    assert second.arguments_schema["properties"]["config_path"]["minLength"] == 1
    assert second.result_schema["additionalProperties"] is False
    for name in ("schema", "thresholds", "generation", "time"):
        nested = second.result_schema["properties"][name]
        assert nested["additionalProperties"] is False
        assert nested["required"] == list(nested["properties"])


def test_registration_is_explicit_and_invalid_base_is_atomic(tmp_path):
    registry = ToolRegistry()
    assert registry.specs() == ()
    with pytest.raises(TypeError, match="ResolvedConfig"):
        register_inspect_project(registry, object())
    assert registry.specs() == ()

    register_inspect_project(registry, _config(tmp_path))
    assert [spec.name for spec in registry.specs()] == ["inspect_project"]


def test_summary_is_bounded_deidentified_and_never_reloads_or_builds_runtime(
    tmp_path, monkeypatch, capsys,
):
    cfg = _config(tmp_path)
    monkeypatch.setattr(
        "labelkit.common.config.loader.load",
        lambda *_a, **_k: pytest.fail("R0 inspection must not reload TOML"),
    )
    monkeypatch.setattr(
        "labelkit.common.runtime.llm_client.LLMClient.__init__",
        lambda *_a, **_k: pytest.fail("inspection must not construct LLMClient"),
    )
    monkeypatch.setattr(
        "labelkit.operators.emitter.Emitter.__init__",
        lambda *_a, **_k: pytest.fail("inspection must not construct Emitter"),
    )
    registry = ToolRegistry()
    register_inspect_project(registry, cfg)

    # Mutating caller-owned mappings after registration cannot alter the bound snapshot.
    cfg.user_schema["properties"]["late-private-field"] = {"type": "string"}
    cfg.llm_profiles["late-private-profile"] = _profile("late-private-profile")
    path_policy = PathPolicy(
        base_dir=tmp_path,
        allowed_reads=(tmp_path / "config.toml", tmp_path / "project.toml"),
        rules={"inspect_project": INSPECT_PROJECT_PATH_RULE},
    )
    budget = _budget()
    result = ToolRouter(registry, policies=(path_policy, budget)).route(_call())

    assert result.status == "success"
    assert result.output["config_digest"] == "sha256:config-safe-digest"
    assert result.output["project_digest"] == "sha256:project-safe-digest"
    assert result.output["mode"] == "generate_only"
    assert result.output["modality"] == "text"
    assert result.output["enabled_operators"] == ["dedup", "generate"]
    assert result.output["referenced_llm_profiles"] == ["planner_profile"]
    assert result.output["referenced_embedding_profiles"] == ["embedding_profile"]
    assert result.output["profile_summary_truncated"] is False
    assert result.output["schema"] == {
        "root_object": True,
        "property_count": 1,
        "required_count": 1,
        "allows_additional_properties": False,
        "frame_schema_present": False,
        "class_schema_override_count": 0,
    }
    assert result.output["thresholds"] == {
        "fatal_errors": 17,
        "dedup_minhash": 0.81,
        "dedup_semantic": 0.93,
        "quality": None,
        "top_ratio": None,
    }
    assert result.output["generation"] == {
        "enabled": True,
        "seed_example_count": 0,
        "standalone_count": None,
        "num_per_record": 2,
        "configured_sequences": 7,
        "sessions": 2,
        "duplicates": 1,
        "declared_tier_rows": 1,
    }
    assert result.output["time"] == {
        "segmentation_enabled": False,
        "generation_enabled": True,
        "metadata_ordering": True,
        "partition_key_count": 1,
        "gap_s": 90,
        "max_span_s": 600,
        "global_rule_rows": 1,
        "class_rule_override_count": 0,
        "global_window_rows": 1,
        "class_window_override_count": 0,
        "time_profile_rows": 1,
    }
    assert result.output["risk_flags"] == [
        "custom_code_hooks", "content_capturing_trace", "full_rejects",
        "passthrough_fields", "unknown_llm_pricing",
    ]
    rendered = json.dumps(result.output, ensure_ascii=False)
    assert PRIVATE not in rendered
    assert "late-private" not in rendered
    assert str(tmp_path) not in rendered
    assert capsys.readouterr() == ("", "")
    assert budget.snapshot().tool_calls == 1
    assert not (tmp_path / PRIVATE).exists()


def test_denied_path_precedes_budget_claim(tmp_path):
    cfg = _config(tmp_path)
    registry = ToolRegistry()
    register_inspect_project(registry, cfg)
    allowed = tmp_path / "config.toml"
    path_policy = PathPolicy(
        base_dir=tmp_path,
        allowed_reads=(allowed,),
        rules={"inspect_project": INSPECT_PROJECT_PATH_RULE},
    )
    budget = _budget()

    result = ToolRouter(registry, policies=(path_policy, budget)).route(_call())

    assert result.error.kind == "policy_denied"
    assert result.error.details == {"rule": "read_scope"}
    assert budget.snapshot().tool_calls == 0


def test_allowed_but_unbound_paths_fail_without_leaking_path(tmp_path):
    cfg = _config(tmp_path)
    other = tmp_path / "other.toml"
    other.write_text("private other project", encoding="utf-8")
    registry = ToolRegistry()
    register_inspect_project(registry, cfg)
    path_policy = PathPolicy(
        base_dir=tmp_path,
        allowed_reads=(tmp_path,),
        rules={"inspect_project": INSPECT_PROJECT_PATH_RULE},
    )

    result = ToolRouter(registry, policies=(path_policy,)).route(
        _call(project_path="other.toml"))

    assert result.error.kind == "execution_failed"
    assert result.error.details == {"exception_type": "ValueError"}
    assert "other.toml" not in repr(result)


@pytest.mark.parametrize("arguments", [
    {"config_path": "config.toml"},
    {"config_path": "", "project_path": "project.toml"},
    {"config_path": "config.toml", "project_path": "project.toml", "raw": True},
])
def test_invalid_arguments_are_rejected_before_execution(tmp_path, arguments):
    registry = ToolRegistry()
    register_inspect_project(registry, _config(tmp_path))

    result = ToolRouter(registry).route(
        ToolCall("call-project", "inspect_project", arguments, "inspect-project-1"))

    assert result.error.kind == "invalid_arguments"


def test_profile_names_are_count_and_length_bounded(tmp_path):
    cfg = _config(tmp_path)
    names = ["x" * 200] + [f"profile_{index:03d}" for index in range(70)]
    profiles = {name: _profile(name, priced=True) for name in names}
    cfg = replace(
        cfg,
        llm_profiles=profiles,
        embedding_profiles={},
        dedup=replace(cfg.dedup, semantic=False, semantic_embedding=None),
        generate=replace(
            cfg.generate, llms=tuple(names), sample_validator=None,
            sequence_validator=None),
        output=OutputConfig(schema_inline="{}"),
        trace=TraceConfig(),
    )
    registry = ToolRegistry()
    register_inspect_project(registry, cfg)

    result = ToolRouter(registry).route(_call(
        config_path=cfg.config_path, project_path=cfg.project_path))

    assert result.status == "success"
    assert result.output["profile_summary_truncated"] is True
    assert len(result.output["referenced_llm_profiles"]) == 64
    assert len(result.output["referenced_llm_profiles"][0]) == 128
    assert all(len(name) <= 128 for name in result.output["referenced_llm_profiles"])
    assert result.output["risk_flags"] == []
