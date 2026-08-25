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

## 7. P2.2 Registry and Router

`ToolRegistry` starts empty. Production code registers no real LabelKit tool in
P2.2; tests inject local executors. Registration is atomic and rejects:

- names outside `^[a-z][a-z0-9_]{0,63}$` or duplicate names;
- R4 declarations and non-callable executors;
- schemas whose root is not `type: object` with
  `additionalProperties: false`;
- any `$ref` until a bounded reference-resolution policy is specified;
- schemas that fail Draft 2020-12 meta-validation.

`specs()` returns a name-sorted immutable tuple. `get_spec()` exposes metadata
only; executors remain package-private and are resolvable only by `ToolRouter`.
Registration owns JSON copies of both schemas, and every public spec read returns
new copies, so caller mutation cannot drift Planner metadata from compiled gates.

Routing order is frozen:

1. resolve the code-registered tool name;
2. make a strict JSON round-trip copy of arguments;
3. validate arguments against `arguments_schema`;
4. apply configured policies in declaration order, JSON-isolate and validate each
   normalized call before passing it to the next policy;
5. call the executor exactly once with the isolated, policy-approved copy;
6. make a strict JSON round-trip copy of its output;
7. validate the output against `result_schema`;
8. construct the sole boundary-owned `ToolResult` with echoed identity and
   elapsed time.

Strict JSON copying rejects NaN/infinity, non-string object keys, non-object
roots, unsupported values, and cycles. It prevents an executor from mutating the
original `ToolCall` and prevents later executor mutation from changing a returned
result.

Unknown tools and invalid arguments never reach an executor. Argument/result
validation diagnostics contain only RFC 6901 paths and validator keyword names,
never rejected instance values; at most 20 violations are returned with an
explicit truncation flag. Executor exceptions become `execution_failed` with only
the exception class name, never the exception message. Invalid outputs become
`invalid_result` and are discarded. `KeyboardInterrupt`, `SystemExit`, and other
`BaseException` control-flow signals are not swallowed.

Router-generated errors are non-retryable in P2.2. P2.3–P2.4 add the policies
below; approval decisions, persistence, asynchronous execution, and real tool
registration remain out of scope.

## 8. P2.3 Path Policy

`ToolRouter` accepts an ordered tuple of `ToolPolicy` objects. A policy receives a
defensive `ToolSpec` copy and a JSON-isolated `ToolCall`, then returns either a
normalized `ToolCall` or a structured `ToolError`. Policies run only after the
original arguments pass their schema and immediately before the executor. The
Router rejects policy exceptions, non-JSON/schema-invalid normalized arguments,
and any attempt to change the call, tool, or idempotency identities. A denial or
broken policy never reaches the executor.

`PathPolicy` is configured per run with:

- a base directory used to resolve relative arguments;
- explicitly allowed read paths;
- an optional single write root; when writes are allowed P2.5 requires it to be
  the verified `AgentWorkspace.root` at `out/agent/<run_id>`;
- one `ToolPathRule` for every registered tool, including an explicit empty rule
  for pathless tools.

Rules declare top-level scalar string arguments as reads or writes. A missing
tool rule denies the entire call. An existing directory read grant authorizes its
subtree; a file or non-existing read grant authorizes only that exact path. A
write is authorized only at or below the write root. Authorized arguments are
replaced with canonical absolute paths before execution.

The following conditions produce `policy_denied` without returning the rejected
path value:

- any explicit `..` component, even if lexical normalization would remain inside
  a grant;
- any path outside its read or write grant;
- any existing symbolic-link or Windows reparse-point component;
- `.git`, `.ssh`, `.aws`, `.env*`, `mytips.md`, common credential/secret names,
  private-key names, and `.pem|.key|.p12|.pfx` paths.

Policy configuration itself rejects parent traversal, linked roots, and sensitive
grants. The implementation performs metadata checks immediately before dispatch,
but it is not an operating-system sandbox: future real file executors must retain
the canonical path, avoid link-following races, and confine writes to the
P2.5-owned workspace. P2.3 registers no real LabelKit tool and performs no file
write through an Agent executor.

## 9. P2.4 Budget and Duplicate-Action Policy

`BudgetPolicy` is a stateful terminal policy. `ToolRouter` rejects a policy chain
that places a terminal policy before another policy. Consequently path and future
stateless authorization gates complete first, and only then may the final policy
atomically claim scarce resources. Every successful policy transformation is
JSON-isolated and schema-validated before the next policy sees it.

`BudgetLimits` defines exact per-run limits for USD cost, tool calls, pilot runs,
one-based current iteration, and monotonic elapsed time. It also defines a cost
soft-threshold ratio in `(0, 1]`. `ToolBudgetRule` is code-owned metadata for one
tool and declares either a fixed `Decimal` or code-owned dynamic cost estimator,
plus whether the call is a pilot. Planner arguments never self-declare trusted
cost. Every tool needs an explicit rule. An unknown estimate is represented by
`None` and returns `approval_required`; it is never silently treated as zero.
All money arithmetic uses finite non-negative `Decimal` values.

For a schema-valid call, checks occur deterministically in this order:

1. declared tool rule;
2. previously claimed idempotency-key digest;
3. previously claimed normalized action digest (`tool + arguments`);
4. iteration and monotonic wall-clock limits;
5. tool-call and pilot-run limits;
6. known cost;
7. current hard/soft cost state and proposed reservation.

Only a call that passes every check is claimed. Cost, counters, idempotency digest,
and action digest are updated together under one lock immediately before executor
dispatch. This prevents concurrent overspend and concurrent duplicate execution.
Raw idempotency keys and arguments are not retained by the duplicate detector.

Reaching a soft threshold does not cancel the call that crosses it, but blocks the
next dispatch. An estimate above the hard remainder is denied. A zero-cost tool
may run when `max_cost_usd` is zero, subject to the other limits. Exact cost can be
reported once with `settle(idempotency_key, actual_cost_usd)`, replacing the
reservation; zero releases the monetary reservation, while an actual overrun
causes later calls to fail closed. `BudgetSnapshot` exposes exact aggregate totals
and counters without identities or arguments.

Claims survive executor exceptions and invalid results. This is conservative by
design: an uncertain paid action must not be automatically replayed. A caller may
settle it to zero only after deterministically proving that no paid request was
made; the action remains a duplicate. Tools whose result can change must include
immutable input/candidate/manifest versions in their arguments and idempotency
derivation, so a genuinely new state has a new semantic action.

P2.4 keeps the ledger in memory and registers no real paid tool. Persistent claim
restoration belongs to P5, Controller iteration ownership belongs to P3, and
actual provider usage extraction plus dual pilot limits belong to P4. Approval of
unknown costs is not implemented by returning `approval_required`; it remains a
separate future state transition.

## 10. P2.5 Agent Workspace Lifecycle

`AgentWorkspace.create(project_root, agent_run_id)` derives its only shared parent
as `<project_root>/out/agent` and publishes the run at
`out/agent/<agent_run_id>`. Callers cannot supply an arbitrary output root. Run
identities match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`; candidate identities match
`candidate-[0-9]{3,6}`. Traversal, separators, dot names, and unsupported names
are rejected before any write. Project paths containing `.git`, `.ssh`, or `.aws`
components are forbidden.

Creation uses this visibility protocol for both runs and candidates:

1. verify the canonical parent is a real directory with no symbolic-link or
   Windows reparse-point component;
2. exclusively create a same-parent hidden claim file;
3. build all required directories and a bounded ownership marker in a random
   same-parent `.part` directory;
4. flush and `fsync` the marker;
5. confirm the final identity is still absent and rename the completed directory
   once into its final name;
6. remove the claim; on failure, remove only the validated staging directory and
   claim, never a caller-owned path.

The claim makes publication single-winner across cooperating processes. The
second final-name check prevents overwriting a target introduced while staging.
The final workspace is invisible until its required layout is complete. This is
an atomic visibility contract, not yet P5 crash durability: a process killed
without running cleanup may leave a claim or staging directory, and this batch
does not guess whether such a claim is live or delete it automatically.

A run initially owns only:

```text
out/agent/<run_id>/
  .workspace.json
  source/
  candidates/
```

`create_candidate(candidate_id)` atomically adds one directory containing
`.candidate.json` and `run/`. The marker has exact `schema_version`, kind, run,
and candidate identities. Reusing an identity never overwrites its directory.
`open()` and `open_candidate()` are read-only: they do not create missing parents
and reject missing, oversized, malformed, foreign, linked, or structurally
incomplete workspaces. Public path records are frozen, and every mutating method
revalidates ownership so altered records cannot redirect writes.

P2.5 creates no goal, state, events, manifest, candidate configuration, or LabelKit
output file. Their serialization and recovery belong to later batches. Like the
path policy, this API is not an OS sandbox against a privileged process changing
directories after validation; real tools must continue to use the owned canonical
paths and the execution-time `PathPolicy`.

## 11. P2.6 Read-only Dataset Inspection Tool

`inspect_dataset` is the first real LabelKit Agent tool. It remains absent from a
new `ToolRegistry` until `register_inspect_dataset(registry, base_config)` is
called explicitly. Registration binds one caller-supplied, already validated,
process-mode `ResolvedConfig`; loading occurs before R0 registration and candidate
validation remains a separate P2.8 responsibility.

The R0 argument object has exactly three required fields: `input_path`, `modality`
(`text|ui`), and `sample_limit` (integer `1..10000`). The code-owned path rule
declares `input_path` read-only and the budget rule declares zero monetary cost
and no pilot. A production Controller must install both rules in a `PathPolicy →
BudgetPolicy` chain. Thus the executor receives a canonical, authorized path and
an invalid or denied call cannot reach profiling or consume a budget claim.

At execution, the requested modality must equal the bound config modality. The
executor creates immutable config replacements for only the authorized input and
modality, clears CLI `limit` and `dry_run`, and calls the public P1
`profile_input(cfg, sample_limit=...)` entry exactly once. It does not copy ingest,
pairing, stream-session, duplicate, time-range, or sensitive-signal logic.

Success has exactly `profile_type` (`text|ui|stream`) and `profile`. The profile
is the JSON form of the corresponding exact frozen P1 dataclass. The result
Schema derives and closes the allowed top-level profile field names from those
contracts, preventing an alternate profile implementation from adding an
undeclared output channel. P1 owns the nested aggregate shapes and vocabulary.

The tool returns filenames, counts, distributions, field names, pairing/session
summaries, time coverage, duplicate rate, and sensitive-pattern hit counts only.
It returns no record text, original JSON, UI tree text, matched sensitive value,
record/session identity, per-record location, or hash. It constructs no LLM,
Emitter, trace, report, or formal output. Router result validation remains the
last boundary; profiler exceptions expose only their exception type, unsupported
profile types fail execution, and non-finite/non-JSON results are discarded as
`invalid_result`.

## 12. P2.7 Deidentified Project Inspection Tool

`inspect_project` is an explicitly registered R0 view over one already validated
`ResolvedConfig`. Registration snapshots the deidentified summary and canonical
config/project identities. Its strict arguments are exactly `config_path` and
`project_path`; both are code-declared read paths. After `PathPolicy` authorization,
both canonical paths must equal the bound snapshot or execution fails without
returning either path. The zero-cost, non-pilot budget rule remains terminal.

The tool deliberately does not reload TOML. M1 validation may import and execute
configured Python validators, so loading a Planner-selected file inside an R0
executor would violate the permanent R4 code-execution boundary. A trusted startup
path loads and validates the source configuration before Agent registration. P2.8
will own candidate validation under the candidate lifecycle and patch whitelist.

The result root is strict and returns only:

- config/project digests, mode, modality, and enabled operator names;
- bounded referenced profile names (at most 64 names of at most 128 characters per
  LLM/embedding list) plus an explicit truncation bit;
- schema shape counts/booleans, selected numeric thresholds, aggregate generation
  quotas, and aggregate stream/time-rule counts;
- the closed risk flags `custom_code_hooks`, `content_capturing_trace`,
  `full_rejects`, `passthrough_fields`, and `unknown_llm_pricing`.

The four nested summary objects also reject undeclared fields. Output never contains
paths, API keys, API-key environment names, endpoints, models, prompt/instruction or
example text, schema property names/content, class/rubric names, validator references,
trace locations, or output locations. Profile names are the sole user-named references
because subsequent planning must identify enabled profiles; they remain untrusted data
and are bounded before entering Planner context. The snapshot is computed at registration,
so later mutation of caller-owned mappings cannot alter a registered tool result. The tool
constructs no loader, callback, LLM client, Emitter, trace, report, or output channel.
