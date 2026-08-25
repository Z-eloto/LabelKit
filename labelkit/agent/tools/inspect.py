"""Explicitly registered, read-only Agent inspection tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Protocol, cast

from labelkit.agent.policies import ToolBudgetRule, ToolPathRule
from labelkit.agent.tools.contracts import JsonObject, ToolSpec
from labelkit.agent.tools.registry import ToolRegistry
from labelkit.common.config.model import ResolvedConfig
from labelkit.orchestration import (
    InputProfile,
    StreamInputProfile,
    TextInputProfile,
    UIInputProfile,
    profile_input,
    referenced_profiles,
)

INSPECT_DATASET_PATH_RULE = ToolPathRule(read_arguments=("input_path",))
INSPECT_DATASET_BUDGET_RULE = ToolBudgetRule()
INSPECT_PROJECT_PATH_RULE = ToolPathRule(
    read_arguments=("config_path", "project_path"))
INSPECT_PROJECT_BUDGET_RULE = ToolBudgetRule()

_PROFILE_LIMIT = 64
_RISK_FLAGS = [
    "custom_code_hooks", "content_capturing_trace", "full_rejects",
    "passthrough_fields", "unknown_llm_pricing",
]


class InputProfiler(Protocol):
    def __call__(
        self, cfg: ResolvedConfig, *, sample_limit: int
    ) -> InputProfile: ...


def inspect_dataset_spec() -> ToolSpec:
    """Return fresh Planner-visible metadata for the dataset inspection tool."""
    arguments_schema = {
        "type": "object",
        "properties": {
            "input_path": {"type": "string", "minLength": 1},
            "modality": {"type": "string", "enum": ["text", "ui"]},
            "sample_limit": {"type": "integer", "minimum": 1, "maximum": 10_000},
        },
        "required": ["input_path", "modality", "sample_limit"],
        "additionalProperties": False,
    }
    result_schema = {
        "type": "object",
        "properties": {
            "profile_type": {"type": "string", "enum": ["text", "ui", "stream"]},
            "profile": {"type": "object"},
        },
        "required": ["profile_type", "profile"],
        "additionalProperties": False,
        "allOf": [
            _profile_result_branch("text", TextInputProfile),
            _profile_result_branch("ui", UIInputProfile),
            _profile_result_branch("stream", StreamInputProfile),
        ],
    }
    return ToolSpec(
        "inspect_dataset",
        "Read a bounded dataset prefix and return content-free aggregate statistics.",
        "R0",
        arguments_schema,
        result_schema,
    )


def register_inspect_dataset(
    registry: ToolRegistry,
    base_config: ResolvedConfig,
    *,
    profiler: InputProfiler = profile_input,
) -> None:
    """Bind one validated config and explicitly register ``inspect_dataset``."""
    if not isinstance(base_config, ResolvedConfig):
        raise TypeError("base_config must be a ResolvedConfig instance")
    if base_config.run.mode != "process":
        raise ValueError("inspect_dataset requires a process-mode base config")
    if not callable(profiler):
        raise TypeError("profiler must be callable")

    def execute(arguments: JsonObject) -> JsonObject:
        modality = cast(str, arguments["modality"])
        if modality != base_config.run.modality:
            raise ValueError("requested modality does not match the validated config")
        cfg = replace(
            base_config,
            run=replace(
                base_config.run,
                input=cast(str, arguments["input_path"]),
                modality=modality,
            ),
            limit=None,
            dry_run=False,
        )
        profile = profiler(cfg, sample_limit=cast(int, arguments["sample_limit"]))
        profile_type = _profile_type(profile)
        return {"profile_type": profile_type, "profile": asdict(profile)}

    registry.register(inspect_dataset_spec(), execute)


def inspect_project_spec() -> ToolSpec:
    """Return fresh metadata for inspecting one bound configuration snapshot."""
    string_list = {"type": "array", "maxItems": _PROFILE_LIMIT,
                   "items": {"type": "string", "maxLength": 128}}
    result_schema = _strict_object({
        "config_digest": {"type": "string", "minLength": 1},
        "project_digest": {"type": "string", "minLength": 1},
        "mode": {"type": "string", "enum": ["process", "generate_only"]},
        "modality": {"type": "string", "enum": ["text", "ui"]},
        "enabled_operators": {**string_list, "items": {"type": "string"}},
        "referenced_llm_profiles": {**string_list, "items": {**string_list["items"]}},
        "referenced_embedding_profiles": {**string_list, "items": {**string_list["items"]}},
        "profile_summary_truncated": {"type": "boolean"},
        "schema": _closed_keys(
            "root_object", "property_count", "required_count",
            "allows_additional_properties", "frame_schema_present",
            "class_schema_override_count"),
        "thresholds": _closed_keys(
            "fatal_errors", "dedup_minhash", "dedup_semantic", "quality", "top_ratio"),
        "generation": _closed_keys(
            "enabled", "seed_example_count", "standalone_count", "num_per_record",
            "configured_sequences", "sessions", "duplicates", "declared_tier_rows"),
        "time": _closed_keys(
            "segmentation_enabled", "generation_enabled", "metadata_ordering",
            "partition_key_count", "gap_s", "max_span_s", "global_rule_rows",
            "class_rule_override_count", "global_window_rows",
            "class_window_override_count", "time_profile_rows"),
        "risk_flags": {
            "type": "array", "items": {"type": "string", "enum": _RISK_FLAGS},
            "uniqueItems": True, "maxItems": len(_RISK_FLAGS),
        },
    })
    return ToolSpec(
        "inspect_project",
        "Return a bounded, deidentified summary of a validated project snapshot.",
        "R0",
        _strict_object({
            "config_path": {"type": "string", "minLength": 1},
            "project_path": {"type": "string", "minLength": 1},
        }),
        result_schema,
    )


def register_inspect_project(registry: ToolRegistry, base_config: ResolvedConfig) -> None:
    """Bind and summarize one validated config without reloading executable hooks."""
    if not isinstance(base_config, ResolvedConfig):
        raise TypeError("base_config must be a ResolvedConfig instance")
    expected = (_canonical(base_config.config_path), _canonical(base_config.project_path))
    summary = _project_summary(base_config)

    def execute(arguments: JsonObject) -> JsonObject:
        requested = (_canonical(cast(str, arguments["config_path"])),
                     _canonical(cast(str, arguments["project_path"])))
        if requested != expected:
            raise ValueError("requested paths do not match the validated config")
        return summary

    registry.register(inspect_project_spec(), execute)


def _profile_type(profile: InputProfile) -> str:
    if type(profile) is TextInputProfile:
        return "text"
    if type(profile) is UIInputProfile:
        return "ui"
    if type(profile) is StreamInputProfile:
        return "stream"
    raise TypeError("profiler returned an unsupported profile type")


def _profile_result_branch(profile_type: str, contract: type) -> JsonObject:
    """Allow exactly the fields owned by one frozen P1 profile dataclass."""
    names = [field.name for field in fields(contract)]
    return {
        "if": {"properties": {"profile_type": {"const": profile_type}}},
        "then": {"properties": {"profile": {
            "type": "object",
            "properties": {name: {} for name in names},
            "required": names,
            "additionalProperties": False,
        }}},
    }


def _strict_object(properties: JsonObject) -> JsonObject:
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


def _closed_keys(*names: str) -> JsonObject:
    return _strict_object({name: {} for name in names})


def _canonical(value: str) -> str:
    return str(Path(value).resolve(strict=False))


def _project_summary(cfg: ResolvedConfig) -> JsonObject:
    llm_names, embedding_names = referenced_profiles(cfg)
    llm, llm_cut = _bounded_names(llm_names)
    embeddings, embedding_cut = _bounded_names(embedding_names)
    truncated = llm_cut or embedding_cut
    schema_properties = cfg.user_schema.get("properties")
    schema_required = cfg.user_schema.get("required")
    enabled = ["ingest"] if cfg.run.mode == "process" else []
    enabled.extend(name for name, active in (
        ("segment", cfg.segment.enabled), ("stitch", cfg.stitch.enabled),
        ("dedup", cfg.dedup.enabled), ("classify", cfg.classify.enabled),
        ("extract", cfg.extract.enabled), ("quality", cfg.quality.enabled),
        ("generate", cfg.generate.enabled), ("annotate", cfg.annotate.enabled),
        ("frame_classify", cfg.frame_classify.enabled),
        ("frame_annotate", cfg.frame_annotate.enabled), ("verify", cfg.verify.enabled),
    ) if active)
    risk_flags = [name for name, active in (
        ("custom_code_hooks", bool(cfg.output.validator or cfg.generate.sample_validator
                                   or cfg.generate.sequence_validator)),
        ("content_capturing_trace", cfg.trace.enabled and cfg.trace.content in ("excerpt", "full")),
        ("full_rejects", cfg.output.rejects == "full"),
        ("passthrough_fields", bool(cfg.output.passthrough_fields)),
        ("unknown_llm_pricing", any(cfg.llm_profiles[item].price_per_mtok_in is None
                                    or cfg.llm_profiles[item].price_per_mtok_out is None for item in llm_names)),
    ) if active]
    class_views = tuple(cfg.class_views.values())
    return {
        "config_digest": cfg.config_digest, "project_digest": cfg.project_digest,
        "mode": cfg.run.mode, "modality": cfg.run.modality,
        "enabled_operators": enabled,
        "referenced_llm_profiles": llm, "referenced_embedding_profiles": embeddings,
        "profile_summary_truncated": truncated,
        "schema": {
            "root_object": cfg.user_schema.get("type") == "object",
            "property_count": len(schema_properties) if isinstance(schema_properties, Mapping) else 0,
            "required_count": len(schema_required) if isinstance(schema_required, (list, tuple)) else 0,
            "allows_additional_properties": cfg.user_schema.get("additionalProperties") is not False,
            "frame_schema_present": cfg.frame_schema is not None, "class_schema_override_count": sum(
                view.schema is not None for view in class_views),
        },
        "thresholds": {
            "fatal_errors": cfg.run.fatal_error_threshold,
            "dedup_minhash": cfg.dedup.minhash_threshold if cfg.dedup.enabled else None,
            "dedup_semantic": cfg.dedup.semantic_threshold if cfg.dedup.enabled and cfg.dedup.semantic else None,
            "quality": cfg.quality.threshold if cfg.quality.enabled else None, "top_ratio": (
                cfg.quality.top_ratio if cfg.quality.enabled else None),
        },
        "generation": {
            "enabled": cfg.generate.enabled, "seed_example_count": len(cfg.generate.seed_examples),
            "standalone_count": cfg.generate.standalone_count, "num_per_record": cfg.generate.num_per_record,
            "configured_sequences": sum(view.generate.sequences for view in class_views)
            if cfg.generate_stream.enabled and class_views else (
                cfg.generate.sequences if cfg.generate_stream.enabled else 0),
            "sessions": cfg.generate_stream.sessions, "duplicates": cfg.generate_stream.duplicates,
            "declared_tier_rows": len(cfg.generate_stream.tiers) + sum(
                len(view.tiers) for view in class_views if view.tiers is not None),
        },
        "time": {
            "segmentation_enabled": cfg.segment.enabled, "generation_enabled": cfg.generate_stream.enabled,
            "metadata_ordering": cfg.stream.order_by.startswith("meta:"),
            "partition_key_count": len(cfg.stream.key), "gap_s": cfg.stream.gap_s,
            "max_span_s": cfg.stream.session_max_span_s, "global_rule_rows": len(cfg.generate_stream.rules),
            "class_rule_override_count": sum(view.rules is not None for view in class_views),
            "global_window_rows": len(cfg.generate_stream.windows), "class_window_override_count": sum(
                view.windows is not None for view in class_views),
            "time_profile_rows": (sum(len(view.generate.time_profiles) for view in class_views)
                                  if class_views else len(cfg.generate.time_profiles)),
        },
        "risk_flags": risk_flags,
    }


def _bounded_names(names: list[str]) -> tuple[list[str], bool]:
    values = [name[:128] for name in names[:_PROFILE_LIMIT]]
    return values, len(names) > _PROFILE_LIMIT or any(
        len(name) > 128 for name in names)


__all__ = [
    "INSPECT_DATASET_BUDGET_RULE", "INSPECT_DATASET_PATH_RULE", "InputProfiler",
    "INSPECT_PROJECT_BUDGET_RULE", "INSPECT_PROJECT_PATH_RULE",
    "inspect_dataset_spec", "inspect_project_spec", "register_inspect_dataset",
    "register_inspect_project",
]
