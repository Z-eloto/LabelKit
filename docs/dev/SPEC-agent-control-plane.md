# LabelKit Agent Control Plane Specification

Status: normative, implemented incrementally from P2.1. Later batches may add
behavior around these contracts but must not silently change their wire shape or
closed vocabularies.

## 1. Boundary

The Agent control plane chooses and governs actions. Existing orchestration and
operators remain the data plane. Planner output cannot access executors, files,
network clients, or policy state directly; every action crosses the tool boundary.

P2.1 defines data only. It does not register, route, approve, execute, persist, or
retry a tool call. Those behaviors belong to later batches and must consume these
contracts rather than invent parallel dictionaries.

## 2. Risk vocabulary

`ToolRiskLevel` is the exact ordered closed set `R0|R1|R2|R3|R4`:

- R0: read-only inspection or static analysis;
- R1: reversible writes confined to the Agent workspace;
- R2: bounded paid pilot execution;
- R3: full execution or formal configuration/output delivery;
- R4: secrets, code execution, endpoint mutation, or access outside allowed roots;
  permanently forbidden and not made safe by approval.

Risk is declared by code in `ToolSpec`, never supplied by Planner arguments.

## 3. Frozen contracts

`ToolSpec` fields, in order:

1. `name`: stable registry/wire name;
2. `description`: content-safe Planner-facing purpose;
3. `risk`: one `ToolRiskLevel`;
4. `arguments_schema`: Draft 2020-12 JSON Schema for the arguments object;
5. `result_schema`: Draft 2020-12 JSON Schema for a successful output object.

The spec contains no callable. Registry entries associate a spec with an executor
in P2.2 so Planner-visible metadata cannot become an execution capability.

`ToolCall` fields, in order: `call_id`, `tool`, `arguments`,
`idempotency_key`. `call_id` identifies one occurrence for event correlation;
`idempotency_key` identifies the semantic action for replay/duplicate policy. They
are deliberately separate and both are mandatory.

`ToolError` fields, in order: `kind`, `message`, `retryable`, `details`.
Messages and details must not contain credentials, authorization headers, raw
dataset content, or full prompts.

`ToolResult` fields, in order: `call_id`, `tool`, `status`, `output`, `error`,
`elapsed_s`. Status is exactly `success|error`. Success requires a non-null output
and null error; error requires null output and one `ToolError`. Elapsed time is a
finite non-negative number. A result always echoes the call and tool identities.

All four records are frozen dataclasses. Nested JSON mappings remain data supplied
by their owner; the registry/router must validate them before crossing trust
boundaries.

## 4. Error vocabulary

`ToolErrorKind` is the exact ordered closed set:

1. `unknown_tool`;
2. `invalid_arguments`;
3. `invalid_result`;
4. `policy_denied`;
5. `approval_required`;
6. `budget_exceeded`;
7. `duplicate_call`;
8. `execution_failed`;
9. `internal_error`.

Tool-specific expected negative outcomes belong in a successful, schema-valid
domain result when they are business observations. `execution_failed` is for an
executor that could not complete its operation; `internal_error` is reserved for
broken invariants or unexpected router failures. Policy, approval, budget, and
duplicate decisions stay distinct so the future Controller can terminate or pause
deterministically without parsing messages.

## 5. Schema and trust rules

- Tool argument and result schemas use object roots and should reject unknown keys.
- P2.2 validates schemas when registering tools, arguments before dispatch, and
  successful outputs after execution.
- Model-supplied risk, status, error, timing, call identity, or idempotency identity
  is never trusted merely because a JSON object contains those fields.
- Unknown tool names and invalid arguments never reach an executor.
- The Router returns one structured `ToolResult`; it does not throw raw executor
  exceptions across the Controller boundary.
- Schema validation cannot replace Policy. Valid paths, fields, budgets, approvals,
  and duplicate actions are checked again immediately before execution.

## 6. P2.1 acceptance

- exact dataclass field order and closed vocabularies are contract-tested;
- valid strict parameter/result schemas round-trip through Draft 2020-12 validation;
- invalid risk/error/status and invalid result unions are rejected;
- call/result data serialize to JSON without provider-specific function-calling
  objects;
- no executor, registry, policy, I/O, LLM, CLI, or persistence behavior exists.
