"""Offline tests for the in-memory Agent ToolRegistry and ToolRouter."""
from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable

import pytest

from labelkit.agent import ToolCall, ToolRegistry, ToolRouter, ToolSpec


ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"doubled": {"type": "integer"}},
    "required": ["doubled"],
    "additionalProperties": False,
}


def _spec(name: str = "double", *, risk: str = "R0",
          arguments_schema=ARGUMENTS_SCHEMA, result_schema=RESULT_SCHEMA) -> ToolSpec:
    return ToolSpec(
        name, f"Test tool {name}.", risk,
        copy.deepcopy(arguments_schema), copy.deepcopy(result_schema))


def _call(tool: str = "double", arguments=None) -> ToolCall:
    payload = {"value": 4} if arguments is None else arguments
    return ToolCall("call-1", tool, payload, "idem-1")


def _clock(*ticks: float) -> Callable[[], float]:
    values = iter(ticks)
    return lambda: next(values)


def test_registry_starts_empty_and_exposes_only_sorted_specs():
    registry = ToolRegistry()
    registry.register(_spec("z_tool"), lambda arguments: {"doubled": 2})
    registry.register(_spec("a_tool"), lambda arguments: {"doubled": 2})

    assert [spec.name for spec in registry.specs()] == ["a_tool", "z_tool"]
    assert registry.get_spec("a_tool").risk == "R0"
    assert registry.get_spec("missing") is None
    assert not hasattr(registry.get_spec("a_tool"), "executor")


@pytest.mark.parametrize("spec, executor, message", [
    (_spec("Bad-Name"), lambda arguments: {}, "tool name"),
    (_spec("forbidden", risk="R4"), lambda arguments: {}, "permanently forbidden"),
    (_spec("not_callable"), None, "callable"),
    (_spec("open_args", arguments_schema={"type": "object", "additionalProperties": True}),
     lambda arguments: {}, "strict object"),
    (_spec("array_result", result_schema={"type": "array", "additionalProperties": False}),
     lambda arguments: {}, "strict object"),
    (_spec("with_ref", arguments_schema={
        "type": "object", "$ref": "#/$defs/item", "additionalProperties": False,
        "$defs": {"item": {"type": "object"}},
    }), lambda arguments: {}, "must not contain \\$ref"),
    (_spec("bad_schema", result_schema={
        "type": "object", "properties": {"x": {"type": "not-a-type"}},
        "additionalProperties": False,
    }), lambda arguments: {}, "valid JSON Schema"),
    (_spec("non_json_schema", arguments_schema={
        "type": "object", "properties": {"x": {"enum": {1, 2}}},
        "additionalProperties": False,
    }), lambda arguments: {}, "JSON-compatible"),
])
def test_registration_rejects_unsafe_declarations_atomically(spec, executor, message):
    registry = ToolRegistry()
    with pytest.raises((TypeError, ValueError), match=message):
        registry.register(spec, executor)
    assert registry.specs() == ()


def test_duplicate_registration_does_not_replace_the_executor():
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: {"doubled": 8})
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec(), lambda arguments: {"doubled": 999})

    result = ToolRouter(registry).route(_call())
    assert result.output == {"doubled": 8}


def test_registry_owns_schemas_and_returns_defensive_spec_copies():
    spec = _spec()
    registry = ToolRegistry()
    registry.register(spec, lambda arguments: {"doubled": 8})

    spec.arguments_schema["additionalProperties"] = True
    exposed = registry.get_spec("double")
    exposed.result_schema["additionalProperties"] = True

    assert registry.get_spec("double").arguments_schema["additionalProperties"] is False
    assert registry.get_spec("double").result_schema["additionalProperties"] is False
    result = ToolRouter(registry).route(_call(arguments={"value": 4, "extra": 1}))
    assert result.error.kind == "invalid_arguments"


def test_router_returns_schema_valid_success_with_owned_identity_and_timing():
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: {"doubled": arguments["value"] * 2})
    router = ToolRouter(registry, clock=_clock(10.0, 10.25))

    result = router.route(_call())

    assert result.call_id == "call-1" and result.tool == "double"
    assert result.status == "success"
    assert result.output == {"doubled": 8} and result.error is None
    assert result.elapsed_s == pytest.approx(0.25)


def test_unknown_tool_never_reaches_an_executor():
    calls = 0

    def executor(arguments):
        nonlocal calls
        calls += 1
        return {"doubled": 8}

    registry = ToolRegistry()
    registry.register(_spec(), executor)
    result = ToolRouter(registry).route(_call("missing"))

    assert calls == 0
    assert result.status == "error" and result.error.kind == "unknown_tool"


def test_invalid_arguments_never_reach_executor_and_do_not_leak_values():
    calls = 0

    def executor(arguments):
        nonlocal calls
        calls += 1
        return {"doubled": 8}

    registry = ToolRegistry()
    registry.register(_spec(), executor)
    result = ToolRouter(registry).route(_call(arguments={"value": "private-secret"}))

    assert calls == 0
    assert result.error.kind == "invalid_arguments"
    assert result.error.details == {
        "violations": [{"path": "/value", "rule": "type"}],
        "truncated": False,
    }
    assert "private-secret" not in repr(dataclasses.asdict(result))


@pytest.mark.parametrize("arguments", [
    {"value": float("nan")},
    {"value": {1: "non-string-key"}},
    {"value": object()},
])
def test_non_json_arguments_are_rejected_before_execution(arguments):
    calls = 0

    def executor(payload):
        nonlocal calls
        calls += 1
        return {"doubled": 8}

    registry = ToolRegistry()
    registry.register(_spec(), executor)
    result = ToolRouter(registry).route(_call(arguments=arguments))

    assert calls == 0
    assert result.error.kind == "invalid_arguments"


def test_circular_arguments_are_structured_as_invalid_without_execution():
    arguments = {"value": []}
    arguments["value"].append(arguments)
    calls = 0

    def executor(payload):
        nonlocal calls
        calls += 1
        return {"doubled": 8}

    registry = ToolRegistry()
    registry.register(_spec(), executor)
    result = ToolRouter(registry).route(_call(arguments=arguments))
    assert calls == 0 and result.error.kind == "invalid_arguments"


def test_executor_receives_an_isolated_json_copy_of_arguments():
    arguments = {"items": [1, 2]}
    spec = _spec(arguments_schema={
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
        "required": ["items"],
        "additionalProperties": False,
    })

    def executor(payload):
        payload["items"].append(3)
        return {"doubled": len(payload["items"]) * 2}

    registry = ToolRegistry()
    registry.register(spec, executor)
    result = ToolRouter(registry).route(_call(arguments=arguments))

    assert result.output == {"doubled": 6}
    assert arguments == {"items": [1, 2]}


def test_executor_exception_is_sanitized_into_execution_failed():
    def executor(arguments):
        raise RuntimeError("private-secret-from-executor")

    registry = ToolRegistry()
    registry.register(_spec(), executor)
    result = ToolRouter(registry).route(_call())

    assert result.error.kind == "execution_failed"
    assert result.error.details == {"exception_type": "RuntimeError"}
    assert "private-secret" not in repr(dataclasses.asdict(result))


@pytest.mark.parametrize("output", [
    [1, 2],
    {"doubled": {1, 2}},
    {"doubled": float("inf")},
    {"doubled": {1: "non-string-key"}},
])
def test_non_json_object_results_are_rejected(output):
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: output)
    result = ToolRouter(registry).route(_call())
    assert result.error.kind == "invalid_result" and result.output is None


def test_circular_result_is_structured_as_invalid_result():
    output = {"doubled": []}
    output["doubled"].append(output)
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: output)
    result = ToolRouter(registry).route(_call())
    assert result.error.kind == "invalid_result" and result.output is None


def test_schema_invalid_result_is_blocked_without_leaking_output():
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: {"doubled": "private-result"})
    result = ToolRouter(registry).route(_call())

    assert result.error.kind == "invalid_result"
    assert result.error.details["violations"] == [{"path": "/doubled", "rule": "type"}]
    assert "private-result" not in repr(dataclasses.asdict(result))


def test_router_copies_success_output_before_returning_it():
    owned = {"doubled": 8}
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: owned)
    result = ToolRouter(registry).route(_call())
    owned["doubled"] = 999
    assert result.output == {"doubled": 8}


def test_validation_details_are_bounded():
    names = [f"field_{index}" for index in range(25)]
    schema = {
        "type": "object",
        "properties": {name: {"type": "integer"} for name in names},
        "required": names,
        "additionalProperties": False,
    }
    registry = ToolRegistry()
    registry.register(_spec(arguments_schema=schema), lambda arguments: {"doubled": 8})
    result = ToolRouter(registry).route(_call(arguments={}))

    assert len(result.error.details["violations"]) == 20
    assert result.error.details["truncated"] is True


def test_non_monotonic_or_nonfinite_clock_cannot_break_result_contract():
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: {"doubled": 8})
    assert ToolRouter(registry, clock=_clock(2.0, 1.0)).route(_call()).elapsed_s == 0
    assert ToolRouter(registry, clock=_clock(1.0, float("nan"))).route(_call()).elapsed_s == 0


def test_control_flow_base_exceptions_are_not_swallowed():
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        ToolRouter(registry).route(_call())
