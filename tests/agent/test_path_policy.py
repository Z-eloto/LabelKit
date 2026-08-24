"""Offline tests for execution-time Agent path policy gates."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from labelkit.agent import (
    PathPolicy,
    ToolCall,
    ToolError,
    ToolPathRule,
    ToolRegistry,
    ToolRouter,
    ToolSpec,
)

ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "input_path": {"type": "string"},
        "output_path": {"type": "string"},
    },
    "additionalProperties": False,
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"const": True}},
    "required": ["ok"],
    "additionalProperties": False,
}
PATH_RULE = ToolPathRule(
    read_arguments=("input_path",), write_arguments=("output_path",))


def _spec(name: str = "file_tool") -> ToolSpec:
    return ToolSpec(
        name, "Test path authorization without performing file I/O.", "R1",
        ARGUMENTS_SCHEMA, RESULT_SCHEMA)


def _route(policy, arguments, *, name: str = "file_tool"):
    calls = []
    registry = ToolRegistry()

    def executor(received):
        calls.append(received)
        return {"ok": True}

    registry.register(_spec(name), executor)
    result = ToolRouter(registry, policies=(policy,)).route(
        ToolCall("call-1", name, arguments, "idem-1"))
    return result, calls


def _policy(
    base: Path,
    *,
    allowed_reads=(),
    write_root: Path | None = None,
    rules=None,
) -> PathPolicy:
    return PathPolicy(
        base_dir=base,
        allowed_reads=tuple(allowed_reads),
        allowed_write_root=write_root,
        rules={"file_tool": PATH_RULE} if rules is None else rules,
    )


def test_allowed_paths_are_canonicalized_before_one_execution(tmp_path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    input_file = input_dir / "data.jsonl"
    input_file.write_text("{}\n", encoding="utf-8")
    write_root = tmp_path / "out" / "agent" / "run-1"
    write_root.mkdir(parents=True)
    policy = _policy(tmp_path, allowed_reads=(input_dir,), write_root=write_root)

    result, calls = _route(policy, {
        "input_path": str(input_file.relative_to(tmp_path)),
        "output_path": str((write_root / "nested" / "result.json").relative_to(tmp_path)),
    })

    assert result.status == "success"
    assert calls == [{
        "input_path": str(input_file.resolve()),
        "output_path": str((write_root / "nested" / "result.json").resolve()),
    }]


def test_exact_file_grant_does_not_authorize_its_sibling(tmp_path):
    granted = tmp_path / "granted.jsonl"
    granted.write_text("{}\n", encoding="utf-8")
    sibling = tmp_path / "sibling.jsonl"
    sibling.write_text("{}\n", encoding="utf-8")

    result, calls = _route(
        _policy(tmp_path, allowed_reads=(granted,)), {"input_path": str(sibling)})

    assert result.error.details == {"rule": "read_scope"}
    assert calls == []


def test_directory_grant_authorizes_descendants(tmp_path):
    input_dir = tmp_path / "inputs"
    child = input_dir / "nested" / "data.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text("{}\n", encoding="utf-8")

    result, calls = _route(
        _policy(tmp_path, allowed_reads=(input_dir,)), {"input_path": str(child)})

    assert result.status == "success"
    assert len(calls) == 1


@pytest.mark.parametrize("argument", [
    "inputs/../inputs/data.jsonl",
    "out/agent/run-1/nested/../result.json",
])
def test_parent_traversal_is_rejected_even_when_it_normalizes_inside_scope(
    tmp_path, argument,
):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    write_root = tmp_path / "out" / "agent" / "run-1"
    write_root.mkdir(parents=True)
    field = "input_path" if argument.startswith("inputs") else "output_path"

    result, calls = _route(
        _policy(tmp_path, allowed_reads=(input_dir,), write_root=write_root),
        {field: argument},
    )

    assert result.error.details == {"rule": "parent_traversal"}
    assert calls == []


def test_read_and_write_outside_explicit_scopes_are_rejected(tmp_path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    write_root = tmp_path / "out" / "agent" / "run-1"
    write_root.mkdir(parents=True)
    policy = _policy(tmp_path, allowed_reads=(input_dir,), write_root=write_root)

    read_result, read_calls = _route(policy, {"input_path": str(outside / "data")})
    write_result, write_calls = _route(policy, {"output_path": str(outside / "result")})

    assert read_result.error.details == {"rule": "read_scope"}
    assert write_result.error.details == {"rule": "write_scope"}
    assert read_calls == write_calls == []


def test_missing_write_grant_is_fail_closed(tmp_path):
    result, calls = _route(
        _policy(tmp_path), {"output_path": str(tmp_path / "result.json")})

    assert result.error.kind == "policy_denied"
    assert result.error.details == {"rule": "write_scope"}
    assert calls == []


@pytest.mark.parametrize("relative_path", [
    ".git/config",
    ".env",
    ".env.local",
    "mytips.md",
    "credentials.json",
    "secrets.toml",
    "private.pem",
    "id_ed25519",
])
def test_sensitive_paths_are_rejected_inside_an_allowed_tree(tmp_path, relative_path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()

    result, calls = _route(
        _policy(tmp_path, allowed_reads=(input_dir,)),
        {"input_path": str(input_dir / relative_path)},
    )

    assert result.error.details == {"rule": "sensitive_path"}
    assert calls == []


@pytest.mark.parametrize("mode", ["read", "write"])
def test_link_components_are_rejected_before_execution(tmp_path, mode):
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = tmp_path / "scope"
    scope.mkdir()
    link = scope / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    if mode == "read":
        policy = _policy(tmp_path, allowed_reads=(scope,))
        arguments = {"input_path": str(link / "data.jsonl")}
    else:
        policy = _policy(tmp_path, write_root=scope)
        arguments = {"output_path": str(link / "result.json")}
    result, calls = _route(policy, arguments)

    assert result.error.details == {"rule": "linked_path"}
    assert calls == []


def test_missing_tool_rule_is_fail_closed(tmp_path):
    result, calls = _route(_policy(tmp_path, rules={}), {})

    assert result.error.details == {"rule": "undeclared_tool"}
    assert calls == []


def test_explicit_empty_rule_allows_a_pathless_tool(tmp_path):
    policy = _policy(tmp_path, rules={"pathless": ToolPathRule()})

    result, calls = _route(policy, {}, name="pathless")

    assert result.status == "success"
    assert calls == [{}]


@pytest.mark.parametrize("kwargs", [
    {"read_arguments": ("path", "path")},
    {"write_arguments": ("",)},
    {"read_arguments": ("path",), "write_arguments": ("path",)},
])
def test_path_rule_rejects_ambiguous_declarations(kwargs):
    with pytest.raises(ValueError):
        ToolPathRule(**kwargs)


def test_sensitive_and_linked_policy_grants_are_rejected_at_construction(tmp_path):
    with pytest.raises(ValueError, match="sensitive"):
        _policy(tmp_path, allowed_reads=(tmp_path / ".env",))

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(ValueError, match="links"):
        _policy(tmp_path, write_root=link)


def test_schema_validation_runs_before_policy(tmp_path):
    class ExplodingPolicy:
        def evaluate(self, spec, call):
            raise AssertionError("policy must not see invalid arguments")

    result, calls = _route(ExplodingPolicy(), {"input_path": 42})

    assert result.error.kind == "invalid_arguments"
    assert calls == []


def test_policy_exception_is_sanitized_and_executor_is_not_called(tmp_path):
    class ExplodingPolicy:
        def evaluate(self, spec, call):
            raise RuntimeError("secret value")

    result, calls = _route(ExplodingPolicy(), {})

    assert result.error.kind == "internal_error"
    assert result.error.details == {"exception_type": "RuntimeError"}
    assert "secret" not in result.error.message
    assert calls == []


def test_policy_cannot_change_call_identity(tmp_path):
    class IdentityChangingPolicy:
        def evaluate(self, spec, call):
            return dataclasses.replace(call, tool="another_tool")

    result, calls = _route(IdentityChangingPolicy(), {})

    assert result.error.kind == "internal_error"
    assert calls == []


def test_policy_cannot_mutate_the_registry_owned_spec(tmp_path):
    registry = ToolRegistry()
    registry.register(_spec(), lambda arguments: {"ok": True})

    class MutatingPolicy:
        def evaluate(self, spec, call):
            spec.arguments_schema["additionalProperties"] = True
            return call

    result = ToolRouter(registry, policies=(MutatingPolicy(),)).route(
        ToolCall("call-1", "file_tool", {}, "idem-1"))

    assert result.status == "success"
    assert registry.get_spec("file_tool").arguments_schema["additionalProperties"] is False


def test_policy_denial_is_copied_to_a_structured_result(tmp_path):
    details = {"rule": "test"}

    class DenyingPolicy:
        def evaluate(self, spec, call):
            return ToolError("policy_denied", "denied", details=details)

    result, calls = _route(DenyingPolicy(), {})
    details["rule"] = "mutated"

    assert result.error == ToolError(
        "policy_denied", "denied", details={"rule": "test"})
    assert calls == []
