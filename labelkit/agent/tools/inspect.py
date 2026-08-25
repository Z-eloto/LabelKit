"""Explicitly registered, read-only Agent inspection tools."""

from __future__ import annotations

from dataclasses import asdict, fields, replace
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
)

INSPECT_DATASET_PATH_RULE = ToolPathRule(read_arguments=("input_path",))
INSPECT_DATASET_BUDGET_RULE = ToolBudgetRule()


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


__all__ = [
    "INSPECT_DATASET_BUDGET_RULE", "INSPECT_DATASET_PATH_RULE", "InputProfiler",
    "inspect_dataset_spec", "register_inspect_dataset",
]
