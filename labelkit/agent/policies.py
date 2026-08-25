"""Fail-closed policies applied before an Agent tool is executed."""

from __future__ import annotations

import hashlib
import json
import math
import stat
import threading
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Protocol

from labelkit.agent.tools.contracts import ToolCall, ToolError, ToolSpec

class ToolPolicy(Protocol):
    """A policy may normalize a call or deny it with a structured error."""

    terminal: bool

    def evaluate(self, spec: ToolSpec, call: ToolCall) -> ToolCall | ToolError: ...


@dataclass(frozen=True, slots=True)
class ToolPathRule:
    """Declare which top-level arguments contain read and write paths."""

    read_arguments: tuple[str, ...] = ()
    write_arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        read = _argument_names(self.read_arguments, "read_arguments")
        write = _argument_names(self.write_arguments, "write_arguments")
        if set(read) & set(write):
            raise ValueError("a path argument cannot be both read and write")
        object.__setattr__(self, "read_arguments", read)
        object.__setattr__(self, "write_arguments", write)


class PathPolicy:
    """Authorize declared paths and return canonical arguments.

    Directory read grants cover their subtree; other grants cover one exact path.
    Every registered tool needs an explicit rule, including pathless tools.
    """

    terminal = False

    def __init__(
        self,
        *,
        base_dir: str | Path,
        allowed_reads: tuple[str | Path, ...] = (),
        allowed_write_root: str | Path | None = None,
        rules: Mapping[str, ToolPathRule],
    ) -> None:
        self._base_dir = _configuration_path(base_dir, Path.cwd())
        self._rules = dict(rules)
        if any(not isinstance(name, str) or not name for name in self._rules):
            raise ValueError("policy tool names must be non-empty strings")
        if any(not isinstance(rule, ToolPathRule) for rule in self._rules.values()):
            raise TypeError("path policy rules must be ToolPathRule instances")

        grants: list[tuple[Path, bool]] = []
        for value in allowed_reads:
            path = _configuration_path(value, self._base_dir)
            if _is_sensitive(path):
                raise ValueError("sensitive paths cannot be read grants")
            grants.append((path, path.is_dir()))
        self._allowed_reads = tuple(grants)

        if allowed_write_root is None:
            self._allowed_write_root = None
        else:
            write_root = _configuration_path(allowed_write_root, self._base_dir)
            if _is_sensitive(write_root):
                raise ValueError("sensitive paths cannot be write roots")
            self._allowed_write_root = write_root

    def evaluate(self, spec: ToolSpec, call: ToolCall) -> ToolCall | ToolError:
        rule = self._rules.get(spec.name)
        if rule is None:
            return _denied("undeclared_tool")

        prepared: dict[str, object] = dict(call.arguments)
        for argument in rule.read_arguments:
            error = self._prepare_argument(prepared, argument, write=False)
            if error is not None:
                return error
        for argument in rule.write_arguments:
            error = self._prepare_argument(prepared, argument, write=True)
            if error is not None:
                return error
        return replace(call, arguments=prepared)

    def _prepare_argument(
        self,
        arguments: dict[str, object],
        name: str,
        *,
        write: bool,
    ) -> ToolError | None:
        if name not in arguments:
            return None
        value = arguments[name]
        if not isinstance(value, str):
            return _denied("invalid_path_value")
        if _contains_parent_reference(value):
            return _denied("parent_traversal")

        try:
            raw = Path(value)
            absolute = raw if raw.is_absolute() else self._base_dir / raw
            if _has_link_component(absolute):
                return _denied("linked_path")
            canonical = absolute.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return _denied("invalid_path")

        if _is_sensitive(canonical):
            return _denied("sensitive_path")
        if write:
            if self._allowed_write_root is None or not _within(
                canonical, self._allowed_write_root
            ):
                return _denied("write_scope")
        elif not any(
            canonical == grant or (tree and _within(canonical, grant))
            for grant, tree in self._allowed_reads
        ):
            return _denied("read_scope")

        arguments[name] = str(canonical)
        return None


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Hard per-run limits plus the cost threshold that stops new dispatch."""

    max_cost_usd: Decimal
    max_tool_calls: int
    max_pilot_runs: int
    max_iterations: int
    max_elapsed_s: float
    soft_cost_ratio: Decimal = Decimal("0.9")

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_cost_usd", _money(self.max_cost_usd, "max_cost_usd"))
        object.__setattr__(
            self, "soft_cost_ratio", _money(self.soft_cost_ratio, "soft_cost_ratio"))
        for name in ("max_tool_calls", "max_pilot_runs", "max_iterations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (isinstance(self.max_elapsed_s, bool)
                or not isinstance(self.max_elapsed_s, (int, float))
                or not math.isfinite(self.max_elapsed_s) or self.max_elapsed_s < 0):
            raise ValueError("max_elapsed_s must be finite and non-negative")
        if not Decimal("0") < self.soft_cost_ratio <= Decimal("1"):
            raise ValueError("soft_cost_ratio must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class ToolBudgetRule:
    """Declare a conservative per-call cost reservation and pilot identity."""

    cost_estimate: Decimal | Callable[[ToolCall], Decimal | None] | None = Decimal("0")
    pilot: bool = False

    def __post_init__(self) -> None:
        if self.cost_estimate is not None and not callable(self.cost_estimate):
            object.__setattr__(
                self, "cost_estimate", _money(self.cost_estimate, "cost_estimate"))
        if not isinstance(self.pilot, bool):
            raise ValueError("pilot must be a boolean")

    def estimate(self, call: ToolCall) -> Decimal | None:
        value = self.cost_estimate(call) if callable(self.cost_estimate) else self.cost_estimate
        return None if value is None else _money(value, "cost_estimate")


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Content-safe, exact view of the in-memory run budget ledger."""

    committed_cost_usd: Decimal
    remaining_cost_usd: Decimal
    tool_calls: int
    pilot_runs: int
    settled_calls: int
    elapsed_s: float


class BudgetPolicy:
    """Atomically claim budget and semantic identities immediately before execution."""

    terminal = True

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        rules: Mapping[str, ToolBudgetRule],
        iteration: Callable[[], int] = lambda: 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(limits, BudgetLimits):
            raise TypeError("limits must be a BudgetLimits instance")
        if not callable(iteration) or not callable(clock):
            raise TypeError("iteration and clock must be callable")
        self._limits = limits
        self._rules = dict(rules)
        if any(not isinstance(name, str) or not name for name in self._rules):
            raise ValueError("budget policy tool names must be non-empty strings")
        if any(not isinstance(rule, ToolBudgetRule) for rule in self._rules.values()):
            raise TypeError("budget policy rules must be ToolBudgetRule instances")
        self._iteration = iteration
        self._clock = clock
        self._started = _policy_time(clock)
        self._lock = threading.Lock()
        self._cost = Decimal("0")
        self._tool_calls = 0
        self._pilot_runs = 0
        self._idempotency_digests: set[str] = set()
        self._action_digests: set[str] = set()
        self._reservations: dict[str, tuple[Decimal, Decimal | None]] = {}

    def evaluate(self, spec: ToolSpec, call: ToolCall) -> ToolCall | ToolError:
        rule = self._rules.get(spec.name)
        if rule is None:
            return _policy_error("policy_denied", "undeclared_tool")
        estimate = rule.estimate(call)
        key_digest = _digest(call.idempotency_key)
        action_digest = _action_digest(call)

        with self._lock:
            if key_digest in self._idempotency_digests:
                return _policy_error("duplicate_call", "idempotency_key")
            if action_digest in self._action_digests:
                return _policy_error("duplicate_call", "repeated_action")

            current_iteration = self._iteration()
            if (isinstance(current_iteration, bool) or not isinstance(current_iteration, int)
                    or current_iteration < 1):
                raise RuntimeError("iteration supplier returned an invalid value")
            if current_iteration > self._limits.max_iterations:
                return _policy_error("budget_exceeded", "iterations")
            elapsed = _policy_time(self._clock) - self._started
            if elapsed < 0:
                raise RuntimeError("budget clock moved backwards")
            if elapsed >= self._limits.max_elapsed_s:
                return _policy_error("budget_exceeded", "elapsed_time")
            if self._tool_calls >= self._limits.max_tool_calls:
                return _policy_error("budget_exceeded", "tool_calls")
            if rule.pilot and self._pilot_runs >= self._limits.max_pilot_runs:
                return _policy_error("budget_exceeded", "pilot_runs")
            if estimate is None:
                return _policy_error("approval_required", "cost_unknown")

            maximum = self._limits.max_cost_usd
            if maximum > 0 and self._cost >= maximum:
                return _policy_error("budget_exceeded", "cost_hard_limit")
            if maximum > 0 and self._cost >= maximum * self._limits.soft_cost_ratio:
                return _policy_error("budget_exceeded", "cost_soft_limit")
            if self._cost + estimate > maximum:
                return _policy_error("budget_exceeded", "estimated_cost")

            self._cost += estimate
            self._tool_calls += 1
            self._pilot_runs += int(rule.pilot)
            self._idempotency_digests.add(key_digest)
            self._action_digests.add(action_digest)
            self._reservations[key_digest] = (estimate, None)
        return call

    def settle(self, idempotency_key: str, actual_cost_usd: Decimal) -> BudgetSnapshot:
        """Replace one conservative reservation with its exact non-negative cost."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-empty string")
        actual = _money(actual_cost_usd, "actual_cost_usd")
        key_digest = _digest(idempotency_key)
        with self._lock:
            reservation = self._reservations.get(key_digest)
            if reservation is None:
                raise ValueError("cannot settle an unknown idempotency key")
            estimated, previous = reservation
            if previous is not None:
                raise ValueError("idempotency key is already settled")
            self._cost += actual - estimated
            self._reservations[key_digest] = (estimated, actual)
            return self._snapshot_locked()

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> BudgetSnapshot:
        remaining = max(Decimal("0"), self._limits.max_cost_usd - self._cost)
        settled = sum(actual is not None for _, actual in self._reservations.values())
        elapsed = _policy_time(self._clock) - self._started
        if elapsed < 0:
            raise RuntimeError("budget clock moved backwards")
        return BudgetSnapshot(
            self._cost, remaining, self._tool_calls, self._pilot_runs, settled, elapsed)


def _argument_names(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _configuration_path(value: str | Path, base_dir: Path) -> Path:
    raw_value = str(value)
    if _contains_parent_reference(raw_value):
        raise ValueError("policy configuration paths cannot contain '..'")
    raw = Path(value)
    absolute = raw if raw.is_absolute() else base_dir / raw
    if _has_link_component(absolute):
        raise ValueError("policy configuration paths cannot contain links")
    try:
        return absolute.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("invalid policy configuration path") from exc


def _contains_parent_reference(value: str) -> bool:
    return ".." in value.replace("\\", "/").split("/")


def _has_link_component(path: Path) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            return True
        if getattr(metadata, "st_file_attributes", 0) & reparse_flag:
            return True
    return False


def _within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _is_sensitive(path: Path) -> bool:
    exact_names = {
        ".git", ".ssh", ".aws", ".env", "mytips.md", "credentials.json", "secrets.json",
        "secrets.toml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    }
    secret_suffixes = (".pem", ".key", ".p12", ".pfx")
    for component in path.parts:
        name = component.casefold().rstrip(" .").split(":", 1)[0]
        if (
            name in exact_names
            or name.startswith(".env.")
            or name.startswith("credentials.")
            or name.startswith("secrets.")
            or name.endswith(secret_suffixes)
        ):
            return True
    return False


def _denied(rule: str) -> ToolError:
    return ToolError(
        kind="policy_denied",
        message="path access denied by policy",
        details={"rule": rule},
    )


def _money(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite non-negative decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite non-negative decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be a finite non-negative decimal")
    return result


def _policy_time(clock: Callable[[], float]) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("policy clock must return a finite number")
    return float(value)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _action_digest(call: ToolCall) -> str:
    payload = json.dumps(
        {"tool": call.tool, "arguments": call.arguments},
        ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return _digest(payload)


def _policy_error(kind: str, rule: str) -> ToolError:
    messages = {
        "policy_denied": "tool is not declared in execution policy",
        "approval_required": "tool cost is unknown and requires approval",
        "budget_exceeded": "tool execution exceeds the run budget",
        "duplicate_call": "tool action has already been claimed",
    }
    return ToolError(kind=kind, message=messages[kind], details={"rule": rule})


__all__ = [
    "BudgetLimits", "BudgetPolicy", "BudgetSnapshot", "PathPolicy",
    "ToolBudgetRule", "ToolPathRule", "ToolPolicy",
]
