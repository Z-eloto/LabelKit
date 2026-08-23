"""Contract tests for the provider-neutral Agent tool boundary."""
from __future__ import annotations

import dataclasses
import json
from typing import get_args

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from labelkit.agent import (
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolResultStatus,
    ToolRiskLevel,
    ToolSpec,
)


ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {"sample_limit": {"type": "integer", "minimum": 1}},
    "required": ["sample_limit"],
    "additionalProperties": False,
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"sampled_records": {"type": "integer", "minimum": 0}},
    "required": ["sampled_records"],
    "additionalProperties": False,
}


def _spec() -> ToolSpec:
    return ToolSpec(
        name="inspect_dataset",
        description="Return content-free input statistics.",
        risk="R0",
        arguments_schema=ARGUMENTS_SCHEMA,
        result_schema=RESULT_SCHEMA,
    )


def test_tool_contract_vocabularies_are_closed_and_ordered():
    assert get_args(ToolRiskLevel) == ("R0", "R1", "R2", "R3", "R4")
    assert get_args(ToolResultStatus) == ("success", "error")
    assert get_args(ToolErrorKind) == (
        "unknown_tool",
        "invalid_arguments",
        "invalid_result",
        "policy_denied",
        "approval_required",
        "budget_exceeded",
        "duplicate_call",
        "execution_failed",
        "internal_error",
    )


def test_tool_contract_field_shapes_are_frozen():
    assert [field.name for field in dataclasses.fields(ToolSpec)] == [
        "name", "description", "risk", "arguments_schema", "result_schema",
    ]
    assert [field.name for field in dataclasses.fields(ToolCall)] == [
        "call_id", "tool", "arguments", "idempotency_key",
    ]
    assert [field.name for field in dataclasses.fields(ToolError)] == [
        "kind", "message", "retryable", "details",
    ]
    assert [field.name for field in dataclasses.fields(ToolResult)] == [
        "call_id", "tool", "status", "output", "error", "elapsed_s",
    ]


def test_tool_spec_carries_valid_strict_argument_and_result_schemas():
    spec = _spec()
    Draft202012Validator.check_schema(spec.arguments_schema)
    Draft202012Validator.check_schema(spec.result_schema)

    Draft202012Validator(spec.arguments_schema).validate({"sample_limit": 10})
    Draft202012Validator(spec.result_schema).validate({"sampled_records": 8})
    with pytest.raises(ValidationError):
        Draft202012Validator(spec.arguments_schema).validate({"sample_limit": 0})
    with pytest.raises(ValidationError):
        Draft202012Validator(spec.result_schema).validate({"sampled_records": 8, "raw": "x"})


def test_risk_and_error_vocabularies_are_enforced_at_runtime():
    with pytest.raises(ValueError, match="risk level"):
        dataclasses.replace(_spec(), risk="R5")
    with pytest.raises(ValueError, match="error kind"):
        ToolError(kind="not_closed", message="safe")


@pytest.mark.parametrize("factory, field_name", [
    (lambda: dataclasses.replace(_spec(), name=" "), "name"),
    (lambda: ToolCall("", "inspect_dataset", {}, "idem-1"), "call_id"),
    (lambda: ToolCall("call-1", "inspect_dataset", {}, ""), "idempotency_key"),
    (lambda: ToolError("internal_error", ""), "message"),
])
def test_boundary_identity_and_message_text_must_be_nonempty(factory, field_name):
    with pytest.raises(ValueError, match=field_name):
        factory()


def test_tool_result_enforces_success_and_error_union():
    success = ToolResult(
        call_id="call-1", tool="inspect_dataset", status="success",
        output={"sampled_records": 8}, error=None, elapsed_s=0.25,
    )
    failure = ToolResult(
        call_id="call-2", tool="inspect_dataset", status="error", output=None,
        error=ToolError("invalid_arguments", "sample_limit is required"),
        elapsed_s=0.0,
    )
    assert success.output == {"sampled_records": 8}
    assert failure.error.kind == "invalid_arguments"

    with pytest.raises(ValueError, match="exactly one"):
        dataclasses.replace(success, error=ToolError("internal_error", "broken"))
    with pytest.raises(ValueError, match="exactly one"):
        dataclasses.replace(failure, error=None)
    with pytest.raises(ValueError, match="result status"):
        dataclasses.replace(success, status="partial")


@pytest.mark.parametrize("elapsed_s", [-0.01, float("inf"), float("nan")])
def test_tool_result_rejects_invalid_elapsed_time(elapsed_s):
    with pytest.raises(ValueError, match="elapsed_s"):
        ToolResult("call-1", "inspect_dataset", "success", {}, None, elapsed_s)


def test_tool_call_and_results_have_json_round_trip_shapes():
    call = ToolCall("call-1", "inspect_dataset", {"sample_limit": 10}, "idem-1")
    result = ToolResult(
        "call-1", "inspect_dataset", "error", None,
        ToolError("policy_denied", "path is outside the allowed root", details={"rule": "path"}),
        0.01,
    )

    assert json.loads(json.dumps(dataclasses.asdict(call))) == {
        "call_id": "call-1",
        "tool": "inspect_dataset",
        "arguments": {"sample_limit": 10},
        "idempotency_key": "idem-1",
    }
    assert json.loads(json.dumps(dataclasses.asdict(result)))["error"]["kind"] == "policy_denied"


@pytest.mark.parametrize("value", [
    _spec(),
    ToolCall("call-1", "inspect_dataset", {}, "idem-1"),
    ToolError("execution_failed", "safe failure"),
    ToolResult("call-1", "inspect_dataset", "success", {}, None, 0.0),
])
def test_tool_contracts_are_frozen(value):
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.unexpected = True
