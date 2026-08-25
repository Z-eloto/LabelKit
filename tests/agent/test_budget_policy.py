"""Offline tests for atomic Agent budget and duplicate-action policy."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from labelkit.agent import (
    BudgetLimits,
    BudgetPolicy,
    PathPolicy,
    ToolBudgetRule,
    ToolCall,
    ToolPathRule,
    ToolRegistry,
    ToolRouter,
    ToolSpec,
)

ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"const": True}},
    "required": ["ok"],
    "additionalProperties": False,
}


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _spec(name: str = "paid_tool") -> ToolSpec:
    return ToolSpec(
        name, f"Offline test tool {name}.", "R2",
        ARGUMENTS_SCHEMA, RESULT_SCHEMA)


def _limits(**overrides) -> BudgetLimits:
    values = {
        "max_cost_usd": Decimal("10"),
        "max_tool_calls": 10,
        "max_pilot_runs": 10,
        "max_iterations": 10,
        "max_elapsed_s": 100.0,
        "soft_cost_ratio": Decimal("0.9"),
    }
    values.update(overrides)
    return BudgetLimits(**values)


def _policy(*, limits=None, rule=None, rules=None, iteration=lambda: 1, clock=None):
    selected_rules = {"paid_tool": rule or ToolBudgetRule(Decimal("1"), pilot=True)}
    return BudgetPolicy(
        limits or _limits(),
        rules=selected_rules if rules is None else rules,
        iteration=iteration,
        clock=clock or Clock(),
    )


def _router(policy, *, executor=None, name: str = "paid_tool"):
    calls = []
    registry = ToolRegistry()

    def default_executor(arguments):
        calls.append(arguments)
        return {"ok": True}

    registry.register(_spec(name), executor or default_executor)
    return ToolRouter(registry, policies=(policy,)), calls


def _call(value: int = 1, *, key: str = "idem-1") -> ToolCall:
    return ToolCall(f"call-{key}", "paid_tool", {"value": value}, key)


def test_successful_claim_reserves_exact_cost_and_counters():
    policy = _policy(rule=ToolBudgetRule(Decimal("2.50"), pilot=True))
    router, calls = _router(policy)

    result = router.route(_call())
    snapshot = policy.snapshot()

    assert result.status == "success"
    assert calls == [{"value": 1}]
    assert snapshot.committed_cost_usd == Decimal("2.50")
    assert snapshot.remaining_cost_usd == Decimal("7.50")
    assert snapshot.tool_calls == snapshot.pilot_runs == 1
    assert snapshot.settled_calls == 0


def test_settlement_replaces_reservation_once_and_can_release_cost():
    policy = _policy(rule=ToolBudgetRule(Decimal("2.50")))
    router, _ = _router(policy)
    assert router.route(_call()).status == "success"

    snapshot = policy.settle("idem-1", Decimal("0"))

    assert snapshot.committed_cost_usd == Decimal("0")
    assert snapshot.remaining_cost_usd == Decimal("10")
    assert snapshot.settled_calls == 1
    with pytest.raises(ValueError, match="already settled"):
        policy.settle("idem-1", Decimal("1"))
    with pytest.raises(ValueError, match="unknown"):
        policy.settle("missing", Decimal("1"))


def test_actual_overrun_blocks_all_later_dispatch():
    policy = _policy(rule=ToolBudgetRule(Decimal("1")))
    router, calls = _router(policy)
    assert router.route(_call()).status == "success"
    policy.settle("idem-1", Decimal("11"))

    result = router.route(_call(2, key="idem-2"))

    assert result.error.kind == "budget_exceeded"
    assert result.error.details == {"rule": "cost_hard_limit"}
    assert calls == [{"value": 1}]


def test_estimate_over_remaining_budget_never_reaches_executor():
    policy = _policy(rule=ToolBudgetRule(Decimal("10.01")))
    router, calls = _router(policy)

    result = router.route(_call())

    assert result.error.details == {"rule": "estimated_cost"}
    assert calls == []
    assert policy.snapshot().tool_calls == 0


def test_code_owned_dynamic_estimate_is_checked_for_each_call():
    policy = _policy(rule=ToolBudgetRule(
        lambda call: Decimal(call.arguments["value"])))
    router, calls = _router(policy)

    result = router.route(_call(11))

    assert result.error.details == {"rule": "estimated_cost"}
    assert calls == []
    assert policy.snapshot().committed_cost_usd == Decimal("0")


def test_invalid_dynamic_estimate_fails_closed_without_leaking_or_claiming():
    policy = _policy(rule=ToolBudgetRule(
        lambda call: Decimal("NaN")))
    router, calls = _router(policy)

    result = router.route(_call())

    assert result.error.kind == "internal_error"
    assert result.error.details == {"exception_type": "ValueError"}
    assert calls == []
    assert policy.snapshot().tool_calls == 0


def test_zero_cost_tools_remain_available_with_a_zero_cost_budget():
    policy = _policy(
        limits=_limits(max_cost_usd=Decimal("0")),
        rule=ToolBudgetRule(Decimal("0")),
    )
    router, calls = _router(policy)

    assert router.route(_call()).status == "success"
    assert calls == [{"value": 1}]
    assert policy.snapshot().committed_cost_usd == Decimal("0")


def test_call_may_cross_soft_threshold_but_next_call_is_stopped():
    policy = _policy(
        limits=_limits(soft_cost_ratio=Decimal("0.5")),
        rule=ToolBudgetRule(Decimal("6")),
    )
    router, calls = _router(policy)
    assert router.route(_call()).status == "success"

    result = router.route(_call(2, key="idem-2"))

    assert result.error.details == {"rule": "cost_soft_limit"}
    assert calls == [{"value": 1}]


@pytest.mark.parametrize("limits, rule, iteration, advance, expected", [
    (_limits(max_tool_calls=0), ToolBudgetRule(), lambda: 1, 0, "tool_calls"),
    (_limits(max_pilot_runs=0), ToolBudgetRule(pilot=True), lambda: 1, 0, "pilot_runs"),
    (_limits(max_iterations=1), ToolBudgetRule(), lambda: 2, 0, "iterations"),
    (_limits(max_elapsed_s=5), ToolBudgetRule(), lambda: 1, 5, "elapsed_time"),
])
def test_non_cost_limits_are_checked_before_execution(
    limits, rule, iteration, advance, expected,
):
    clock = Clock()
    policy = _policy(limits=limits, rule=rule, iteration=iteration, clock=clock)
    clock.value = advance
    router, calls = _router(policy)

    result = router.route(_call())

    assert result.error.kind == "budget_exceeded"
    assert result.error.details == {"rule": expected}
    assert calls == []


def test_unknown_cost_requires_approval_without_claiming_identity():
    policy = _policy(rule=ToolBudgetRule(None, pilot=True))
    router, calls = _router(policy)

    first = router.route(_call())
    second = router.route(_call())

    assert first.error.kind == second.error.kind == "approval_required"
    assert first.error.details == {"rule": "cost_unknown"}
    assert calls == []
    assert policy.snapshot().tool_calls == 0


def test_missing_tool_rule_is_fail_closed_without_claiming_budget():
    policy = _policy(rules={})
    router, calls = _router(policy)

    result = router.route(_call())

    assert result.error.kind == "policy_denied"
    assert result.error.details == {"rule": "undeclared_tool"}
    assert calls == []


def test_reused_idempotency_key_is_rejected_even_when_arguments_change():
    policy = _policy()
    router, calls = _router(policy)
    assert router.route(_call(1)).status == "success"

    result = router.route(_call(2))

    assert result.error.kind == "duplicate_call"
    assert result.error.details == {"rule": "idempotency_key"}
    assert calls == [{"value": 1}]


def test_same_semantic_action_is_rejected_with_a_different_key():
    policy = _policy()
    router, calls = _router(policy)
    assert router.route(_call(1, key="idem-1")).status == "success"

    result = router.route(_call(1, key="idem-2"))

    assert result.error.kind == "duplicate_call"
    assert result.error.details == {"rule": "repeated_action"}
    assert calls == [{"value": 1}]


def test_executor_failure_remains_claimed_and_cannot_be_retried():
    executed = []

    def failing(arguments):
        executed.append(arguments)
        raise RuntimeError("uncertain paid request")

    policy = _policy()
    router, _ = _router(policy, executor=failing)
    first = router.route(_call())
    second = router.route(_call())

    assert first.error.kind == "execution_failed"
    assert second.error.kind == "duplicate_call"
    assert executed == [{"value": 1}]
    assert policy.snapshot().committed_cost_usd == Decimal("1")


def test_schema_failure_occurs_before_budget_claim():
    policy = _policy()
    router, calls = _router(policy)

    result = router.route(ToolCall("call-1", "paid_tool", {"value": "bad"}, "idem-1"))

    assert result.error.kind == "invalid_arguments"
    assert calls == []
    assert policy.snapshot().tool_calls == 0


def test_path_denial_before_terminal_budget_policy_does_not_charge(tmp_path):
    path_schema = {
        "type": "object",
        "properties": {"input_path": {"type": "string"}},
        "required": ["input_path"],
        "additionalProperties": False,
    }
    spec = ToolSpec("paid_tool", "Path and budget ordering test.", "R2",
                    path_schema, RESULT_SCHEMA)
    registry = ToolRegistry()
    calls = []
    registry.register(spec, lambda arguments: calls.append(arguments) or {"ok": True})
    path_policy = PathPolicy(
        base_dir=tmp_path,
        allowed_reads=(),
        rules={"paid_tool": ToolPathRule(read_arguments=("input_path",))},
    )
    budget_policy = _policy()
    router = ToolRouter(registry, policies=(path_policy, budget_policy))

    result = router.route(ToolCall(
        "call-1", "paid_tool", {"input_path": str(tmp_path / "outside")}, "idem-1"))

    assert result.error.details == {"rule": "read_scope"}
    assert calls == []
    assert budget_policy.snapshot().tool_calls == 0


def test_terminal_budget_policy_must_be_last():
    class LaterPolicy:
        terminal = False

        def evaluate(self, spec, call):
            return call

    with pytest.raises(ValueError, match="must be last"):
        ToolRouter(ToolRegistry(), policies=(_policy(), LaterPolicy()))


def test_invalid_iteration_fails_closed_without_execution():
    policy = _policy(iteration=lambda: 0)
    router, calls = _router(policy)

    result = router.route(_call())

    assert result.error.kind == "internal_error"
    assert calls == []
    assert policy.snapshot().tool_calls == 0


def test_backwards_budget_clock_fails_closed_without_execution():
    clock = Clock(5)
    policy = _policy(clock=clock)
    clock.value = 4
    router, calls = _router(policy)

    result = router.route(_call())
    clock.value = 5

    assert result.error.kind == "internal_error"
    assert calls == []
    assert policy.snapshot().tool_calls == 0


def test_concurrent_duplicate_actions_execute_exactly_once():
    lock = threading.Lock()
    calls = []

    def executor(arguments):
        with lock:
            calls.append(arguments)
        return {"ok": True}

    policy = _policy()
    router, _ = _router(policy, executor=executor)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda number: router.route(_call(1, key=f"idem-{number}")), range(20)))

    assert sum(result.status == "success" for result in results) == 1
    assert sum(result.error is not None and result.error.kind == "duplicate_call"
               for result in results) == 19
    assert calls == [{"value": 1}]


def test_concurrent_distinct_actions_cannot_overspend():
    policy = _policy(
        limits=_limits(max_cost_usd=Decimal("5"), soft_cost_ratio=Decimal("1")),
        rule=ToolBudgetRule(Decimal("1")),
    )
    router, calls = _router(policy)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda number: router.route(_call(number, key=f"idem-{number}")), range(20)))

    assert sum(result.status == "success" for result in results) == 5
    assert len(calls) == 5
    assert policy.snapshot().committed_cost_usd == Decimal("5")
    assert policy.snapshot().tool_calls == 5


@pytest.mark.parametrize("factory", [
    lambda: _limits(max_cost_usd=Decimal("-1")),
    lambda: _limits(max_cost_usd=Decimal("NaN")),
    lambda: _limits(max_tool_calls=-1),
    lambda: _limits(max_pilot_runs=True),
    lambda: _limits(max_elapsed_s=float("inf")),
    lambda: _limits(soft_cost_ratio=Decimal("0")),
    lambda: ToolBudgetRule(Decimal("-0.1")),
    lambda: ToolBudgetRule(Decimal("Infinity")),
    lambda: ToolBudgetRule(Decimal("1"), pilot=1),
    lambda: BudgetPolicy(None, rules={}),
])
def test_budget_configuration_rejects_invalid_values(factory):
    with pytest.raises((TypeError, ValueError)):
        factory()
