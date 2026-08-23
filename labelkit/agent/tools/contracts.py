"""Agent 工具边界的纯数据契约。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Mapping, TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"]
    | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)
JsonObject: TypeAlias = Mapping[str, JsonValue]
ToolRiskLevel = Literal["R0", "R1", "R2", "R3", "R4"]
ToolResultStatus = Literal["success", "error"]
ToolErrorKind = Literal[
    "unknown_tool",
    "invalid_arguments",
    "invalid_result",
    "policy_denied",
    "approval_required",
    "budget_exceeded",
    "duplicate_call",
    "execution_failed",
    "internal_error",
]

_RISK_LEVELS = ("R0", "R1", "R2", "R3", "R4")
_ERROR_KINDS = (
    "unknown_tool", "invalid_arguments", "invalid_result", "policy_denied",
    "approval_required", "budget_exceeded", "duplicate_call",
    "execution_failed", "internal_error",
)


def _require_text(value: str, field_name: str) -> None:
    """要求边界身份和用户可见文案为非空文本。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True)
class ToolSpec:
    """Planner 可见的工具声明；执行器不进入 spec。"""

    name: str
    description: str
    risk: ToolRiskLevel
    arguments_schema: JsonObject
    result_schema: JsonObject

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.description, "description")
        if self.risk not in _RISK_LEVELS:
            raise ValueError(f"unknown tool risk level: {self.risk}")


@dataclass(frozen=True)
class ToolCall:
    """一次工具请求，同时携带发生身份和语义幂等身份。"""

    call_id: str
    tool: str
    arguments: JsonObject
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_text(self.call_id, "call_id")
        _require_text(self.tool, "tool")
        _require_text(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True)
class ToolError:
    """跨越工具边界返回的内容安全结构化错误。"""

    kind: ToolErrorKind
    message: str
    retryable: bool = False
    details: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.message, "message")
        if self.kind not in _ERROR_KINDS:
            raise ValueError(f"unknown tool error kind: {self.kind}")


@dataclass(frozen=True)
class ToolResult:
    """一次 ToolCall 恰好返回成功输出或结构化错误之一。"""

    call_id: str
    tool: str
    status: ToolResultStatus
    output: JsonObject | None
    error: ToolError | None
    elapsed_s: float

    def __post_init__(self) -> None:
        _require_text(self.call_id, "call_id")
        _require_text(self.tool, "tool")
        if self.status not in ("success", "error"):
            raise ValueError(f"unknown tool result status: {self.status}")
        if not math.isfinite(self.elapsed_s) or self.elapsed_s < 0:
            raise ValueError("elapsed_s must be finite and non-negative")
        success_shape = self.output is not None and self.error is None
        error_shape = self.output is None and self.error is not None
        if (self.status == "success" and not success_shape
                or self.status == "error" and not error_shape):
            raise ValueError("status must select exactly one of output or error")


__all__ = [
    "JsonObject", "JsonValue", "ToolCall", "ToolError", "ToolErrorKind",
    "ToolResult", "ToolResultStatus", "ToolRiskLevel", "ToolSpec",
]
