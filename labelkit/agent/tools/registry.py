"""Agent 工具的内存注册表与确定性路由器。"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from labelkit.agent.policies import ToolPolicy
from labelkit.agent.tools.contracts import (
    JsonObject,
    JsonValue,
    ToolCall,
    ToolError,
    ToolErrorKind,
    ToolResult,
    ToolSpec,
)

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_VIOLATIONS = 20


class ToolExecutor(Protocol):
    """工具执行器只返回成功域对象，边界结果由 Router 组装。"""

    def __call__(self, arguments: JsonObject) -> JsonObject: ...


@dataclass(frozen=True)
class _Registration:
    """已通过注册门的 spec、预编译校验器和执行器。"""

    spec: ToolSpec
    arguments_validator: Draft202012Validator
    result_validator: Draft202012Validator
    executor: ToolExecutor


class ToolRegistry:
    """注册代码声明的工具；不接受 Planner 动态扩展。"""

    def __init__(self) -> None:
        self._items: dict[str, _Registration] = {}

    def register(self, spec: ToolSpec, executor: ToolExecutor) -> None:
        """校验并注册一个工具；失败时不改变已有表。"""
        if not _TOOL_NAME_RE.fullmatch(spec.name):
            raise ValueError("tool name must match ^[a-z][a-z0-9_]{0,63}$")
        if spec.name in self._items:
            raise ValueError(f"tool is already registered: {spec.name}")
        if spec.risk == "R4":
            raise ValueError("R4 tools are permanently forbidden")
        if not callable(executor):
            raise TypeError("tool executor must be callable")
        try:
            owned = _copy_spec(spec)
        except Exception as exc:
            raise ValueError("tool schemas must be JSON-compatible objects") from exc
        arguments_validator = _compile_schema(spec.name, "arguments", owned.arguments_schema)
        result_validator = _compile_schema(spec.name, "result", owned.result_schema)
        self._items[spec.name] = _Registration(
            owned, arguments_validator, result_validator, executor)

    def specs(self) -> tuple[ToolSpec, ...]:
        """按工具名返回稳定的 Planner 可见快照。"""
        return tuple(_copy_spec(self._items[name].spec) for name in sorted(self._items))

    def get_spec(self, name: str) -> ToolSpec | None:
        """只返回 spec，不暴露执行器。"""
        item = self._items.get(name)
        return None if item is None else _copy_spec(item.spec)

    def _resolve(self, name: str) -> _Registration | None:
        """仅供同包 Router 使用的执行解析面。"""
        return self._items.get(name)


class ToolRouter:
    """在执行前后完成 Schema 门禁，并统一翻译错误。"""

    def __init__(self, registry: ToolRegistry, *,
                 policies: tuple[ToolPolicy, ...] = (),
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self._registry = registry
        self._policies = tuple(policies)
        self._clock = clock

    def route(self, call: ToolCall) -> ToolResult:
        """分派一次调用；预期的边界失败一律结构化返回。"""
        started = self._clock()
        item = self._registry._resolve(call.tool)
        if item is None:
            return self._error(call, started, "unknown_tool", "tool is not registered")
        try:
            arguments = _json_object_copy(call.arguments)
        except Exception:
            return self._error(
                call, started, "invalid_arguments", "tool arguments are not JSON-compatible")
        details = _validation_details(item.arguments_validator, arguments)
        if details is not None:
            return self._error(
                call, started, "invalid_arguments", "tool arguments failed schema validation",
                details=details)

        active_call = ToolCall(
            call.call_id, call.tool, arguments, call.idempotency_key)
        for policy in self._policies:
            try:
                outcome = policy.evaluate(_copy_spec(item.spec), active_call)
            except Exception as exc:
                return self._error(
                    call, started, "internal_error", "tool policy failed",
                    details={"exception_type": type(exc).__name__})
            if isinstance(outcome, ToolError):
                return self._policy_error(call, started, outcome)
            if not isinstance(outcome, ToolCall) or (
                    outcome.call_id != call.call_id
                    or outcome.tool != call.tool
                    or outcome.idempotency_key != call.idempotency_key):
                return self._error(
                    call, started, "internal_error",
                    "tool policy returned an invalid call")
            active_call = outcome

        try:
            arguments = _json_object_copy(active_call.arguments)
        except Exception:
            return self._error(
                call, started, "internal_error",
                "tool policy returned invalid arguments")
        if _validation_details(item.arguments_validator, arguments) is not None:
            return self._error(
                call, started, "internal_error",
                "tool policy violated the argument schema")
        try:
            raw_output = item.executor(arguments)
        except Exception as exc:
            return self._error(
                call, started, "execution_failed", "tool execution failed",
                details={"exception_type": type(exc).__name__})
        try:
            output = _json_object_copy(raw_output)
        except Exception:
            return self._error(
                call, started, "invalid_result", "tool result is not a JSON object")
        details = _validation_details(item.result_validator, output)
        if details is not None:
            return self._error(
                call, started, "invalid_result", "tool result failed schema validation",
                details=details)
        return ToolResult(
            call.call_id, call.tool, "success", output, None, self._elapsed(started))

    def _policy_error(
            self, call: ToolCall, started: float, error: ToolError) -> ToolResult:
        """Copy policy errors across the JSON boundary before returning them."""
        try:
            details = _json_object_copy(error.details)
        except Exception:
            return self._error(
                call, started, "internal_error", "tool policy returned an invalid error")
        return ToolResult(
            call.call_id, call.tool, "error", None,
            ToolError(error.kind, error.message, error.retryable, details),
            self._elapsed(started))

    def _error(self, call: ToolCall, started: float, kind: ToolErrorKind,
               message: str, *, details: JsonObject | None = None) -> ToolResult:
        """保留调用身份，且不携带原始异常或数据值。"""
        return ToolResult(
            call.call_id, call.tool, "error", None,
            ToolError(kind, message, details=details or {}), self._elapsed(started))

    def _elapsed(self, started: float) -> float:
        """时钟异常时收敛为 0，不让观测值破坏结果契约。"""
        elapsed = self._clock() - started
        return elapsed if math.isfinite(elapsed) and elapsed >= 0 else 0.0


def _compile_schema(tool: str, face: str, schema: JsonObject) -> Draft202012Validator:
    """注册时只接受严格、无引用的对象根 Schema。"""
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError(f"{tool} {face}_schema must be a strict object schema")
    if _contains_ref(schema):
        raise ValueError(f"{tool} {face}_schema must not contain $ref")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"{tool} {face}_schema is not a valid JSON Schema") from exc
    return Draft202012Validator(schema)


def _copy_spec(spec: ToolSpec) -> ToolSpec:
    """隔离内部校验器与 Planner 可见 Schema 的可变所有权。"""
    return ToolSpec(
        spec.name, spec.description, spec.risk,
        _json_object_copy(spec.arguments_schema), _json_object_copy(spec.result_schema))


def _contains_ref(value: JsonValue) -> bool:
    """递归拒绝尚未引入解析策略的 $ref。"""
    if isinstance(value, Mapping):
        return "$ref" in value or any(_contains_ref(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_ref(child) for child in value)
    return False


def _json_object_copy(value: JsonObject) -> dict[str, JsonValue]:
    """用严格 JSON 往返同时完成类型门禁和隔离拷贝。"""
    if _has_non_string_key(value):
        raise TypeError("JSON object keys must be strings")
    copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    if not isinstance(copied, dict):
        raise TypeError("tool boundary value must be an object")
    return copied


def _has_non_string_key(value: JsonValue) -> bool:
    """防止 json.dumps 把非字符串键静默强制转换后穿过边界。"""
    if isinstance(value, Mapping):
        return (any(not isinstance(key, str) for key in value)
                or any(_has_non_string_key(child) for child in value.values()))
    if isinstance(value, (list, tuple)):
        return any(_has_non_string_key(child) for child in value)
    return False


def _validation_details(
        validator: Draft202012Validator, instance: JsonObject) -> JsonObject | None:
    """只返回路径与规则，不回传可能敏感的实例值。"""
    violations: list[JsonValue] = []
    truncated = False
    for error in validator.iter_errors(instance):
        if len(violations) >= _MAX_VIOLATIONS:
            truncated = True
            break
        violations.append({
            "path": _json_pointer(error),
            "rule": str(error.validator),
        })
    if not violations:
        return None
    return {"violations": violations, "truncated": truncated}


def _json_pointer(error: ValidationError) -> str:
    """把违规路径渲染为 RFC 6901，不包含实例值。"""
    tokens = (str(token).replace("~", "~0").replace("/", "~1")
              for token in error.absolute_path)
    return "".join(f"/{token}" for token in tokens)


__all__ = ["ToolExecutor", "ToolRegistry", "ToolRouter"]
