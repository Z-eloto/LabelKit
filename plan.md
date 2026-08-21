# LabelKit Agent 化实施计划

> 工作分支：`feat/all-day-stream-generation-zj`  
> 文档状态：实施前规划  
> 目标版本：Agent MVP → 可评测的 DatasetOps Agent  
> 核心原则：保留 LabelKit 的确定性数据流水线，在其上增加可审计、受约束、可暂停恢复的 Agent 控制层。

## 1. 背景与目标

当前 LabelKit 是一条配置驱动的 LLM 数据加工流水线。算子顺序、启用项、阈值和停止条件都由代码与 TOML 预先确定；LLM 负责分类、评分、生成、标注和校验，但不会自主决定下一步。因此它属于 LLM Workflow，而不是严格意义上的 Agent。

本次改造的目标，是把它升级为一个 **DatasetOps Agent**：用户只需要描述数据集目标、质量约束、预算和授权范围，Agent 即可自主完成以下闭环：

1. 检查输入数据、现有配置和可用模型能力；
2. 制定本轮加工或合成计划；
3. 选择并调用受控工具；
4. 小规模试跑；
5. 读取报告、拒绝统计和 trace 摘要；
6. 判断目标是否达成；
7. 必要时生成候选配置并重新试跑；
8. 达标后请求用户批准全量运行；
9. 输出最终数据、决策轨迹和效果对比报告。

这次改造不是简单增加聊天入口，也不是让 LLM 直接执行 shell。Agent 必须具有真实的“观察—规划—行动—评估—重规划”循环，同时继续服从 LabelKit 原有的 Schema、预算、审计和记录级隔离机制。

## 2. 非目标

第一阶段明确不做以下事项：

- 不重写现有算子，不把确定性流水线替换成自由执行的 Agent；
- 不让模型执行任意 shell、Python 或文件系统命令；
- 不允许 Agent 读取或输出 API 密钥值；
- 不允许 Agent 未经批准执行无上限的全量任务；
- 不在 MVP 中引入多个互相对话的 Agent；
- 不构建网页标注平台、模型训练平台或数据版本管理系统；
- 不让 Agent 自动修改工具级 `config.toml` 中的端点和密钥配置；
- 不追求跨项目永久记忆，首版记忆仅属于一次 Agent run。

## 3. 完成标准

只有同时满足下列条件，项目才可以对外称为 Agent：

- Planner 根据当前观察自主选择下一项工具，而非固定顺序调用全部工具；
- 工具执行结果会进入下一轮上下文，并能导致计划变化；
- 至少存在一次可验证的试跑—评估—调参—复跑闭环；
- Agent 能主动结束、请求人工输入或因预算耗尽而停止；
- Agent 状态可落盘，可在人工批准后恢复；
- 所有工具输入、输出和 Planner 决策均有结构化 Schema；
- 高成本或不可逆动作存在人工审批门；
- 相同目标、输入、seed 和工具结果可以重放决策过程；
- 有静态配置基线与 Agent 优化结果的自动对照评测；
- 现有 `labelkit run/validate/rubric` 行为保持向后兼容。

## 4. 目标使用场景

### 4.1 普通数据治理 Agent

用户目标示例：

> 把 `data/input.jsonl` 加工成中文意图分类数据集。Schema 合法率必须为 100%，失败率低于 3%，有效产出率至少 75%，总试跑成本不超过 5 美元。

Agent 应能够检查字段、评估重复与质量分布、生成候选 `project.toml`、固定样本试跑、调整质量阈值或提示词，并在满足目标后请求全量运行。

### 4.2 全天合成时间流 Agent

这是当前 fork 分支最有辨识度的演示场景。用户目标示例：

> 生成覆盖通勤、打卡、咖啡、订餐和买菜场景的全天个人活动时间流；每类达到指定配额，时间规则与帧类构成全部合法，重复率低于 2%，总成本不超过 10 美元。

Agent 应能够：

- 分析 `generate.stream` 类别、tier、时间窗口和规则；
- 检查配额可满足性与 dry-run 调用量；
- 小规模生成并读取 `report.generate.stream` 与时间流工件；
- 分析规则失败、类别缺口、重复、噪声和时间覆盖；
- 生成候选配置，调整权重、配额、窗口、生成指令或有限规则；
- 在固定 seed 下复跑并比较；
- 达标后请求执行正式全天生成。

## 5. 总体架构

```text
用户目标 / agent.toml
          │
          ▼
┌──────────────────────── Agent Control Plane ────────────────────────┐
│ Goal Parser → Controller → Planner → Policy/Approval → Tool Router │
│                     ▲                    │             │            │
│                     │                    │             ▼            │
│              Evaluator/Reflector ← Observation Builder             │
│                     ▲                                               │
│                     └──── State Store + Agent Event Log ────────────┤
└───────────────────────────────────────┬──────────────────────────────┘
                                        │ 结构化工具调用
                                        ▼
┌──────────────────────── LabelKit Data Plane ────────────────────────┐
│ inspect / validate / estimate / pilot / run / report / artifacts   │
│                 现有 Orchestrator + Operators                       │
│ LLMClient + SchemaEngine + MetricsSink + Emitter                   │
└───────────────────────────────────────┬──────────────────────────────┘
                                        ▼
                          JSONL / rejects / report / trace
```

架构边界如下：

- **控制平面**负责目标、计划、工具选择、迭代、审批和候选配置；
- **数据平面**仍由现有 LabelKit 确定性执行；
- Planner 不能直接访问文件和网络，只能通过 Tool Router；
- Policy 在每次工具执行前做权限、路径、预算和重复动作检查；
- Evaluator 以代码指标为主，LLM 反思为辅；
- 正式配置与输入数据默认只读，Agent 只写独立运行目录。

## 6. 单 Agent 决策模型

MVP 使用单个 Manager Agent，不使用多 Agent。Planner 每轮只输出一个结构化决策：

```json
{
  "kind": "call_tool",
  "tool": "run_pilot",
  "arguments": {
    "candidate_id": "candidate-002",
    "limit": 64
  },
  "rationale": "上一轮 Schema 已合法，但 writing 类产出率偏低，需要验证放宽门槛后的影响。",
  "expected_effect": {
    "emission_rate": ">=0.75",
    "failure_rate": "<=0.03"
  }
}
```

`kind` 使用闭集：

- `call_tool`：调用一个工具；
- `request_approval`：申请执行需要审批的工具；
- `ask_human`：缺少决定性信息；
- `finish`：目标达成或已得到当前约束下的最佳结果；
- `abort`：安全策略、预算或基础条件不允许继续。

所有决策通过内部 JSON Schema 验证。为保持模型供应商中立，MVP 不依赖厂商原生 function calling：Planner 通过现有 `LLMClient` 生成 `AgentDecision`，再由 `SchemaEngine` 完成结构修复和校验，Controller 根据 `tool` 字段确定性分派。

## 7. Agent 状态机

```text
created
  ↓
observing ↔ planning → executing → evaluating
                 ↑          │          │
                 └──────────┴──────────┘  未达标且仍可改进
                            │
                            ├→ awaiting_approval → executing
                            ├→ awaiting_human → planning
                            ├→ completed
                            ├→ budget_exhausted
                            ├→ policy_blocked
                            ├→ failed
                            └→ cancelled
```

状态迁移必须由 Controller 代码控制，模型只能提出决策，不能直接写状态。

每次 Agent run 至少记录：

- `agent_run_id`；
- 目标与硬约束；
- 源配置摘要和输入摘要；
- 当前 iteration、状态和剩余预算；
- 所有候选配置及其父候选；
- Planner 决策、理由和预期效果；
- 工具调用输入摘要、结果摘要和耗时；
- 每轮评估指标；
- 审批请求与人工决定；
- 最终选择、停止原因和交付路径。

## 8. 工具设计

### 8.1 工具分级

| 等级 | 含义 | 默认审批 |
|---|---|---|
| R0 | 纯读取、静态分析 | 不需要 |
| R1 | 写 Agent 独立工作区，可完全回滚 | 不需要 |
| R2 | 发起受限 LLM 试跑，会产生费用 | 首次可审批，之后受预算策略控制 |
| R3 | 全量运行、替换正式配置或写正式输出 | 必须审批 |
| R4 | 修改密钥、端点、执行代码、访问工作区外路径 | 永久禁止 |

### 8.2 MVP 工具清单

#### `inspect_dataset`（R0）

输入：输入路径、模态、采样上限。  
输出：记录数估计、字段、空值、长度分布、模态、UI 配对情况、基础重复率、时间字段概览、敏感内容风险摘要。  
约束：默认只返回统计和脱敏样本摘要，不把整批原文塞入 Planner 上下文。

#### `inspect_project`（R0）

输入：`config.toml` 与 `project.toml` 路径。  
输出：已启用算子、模型 profile 引用、输出 Schema 摘要、阈值、生成配额、时间流规则和静态风险。

#### `validate_candidate`（R0）

输入：候选配置 ID。  
实现：复用 `validate_project()`。  
输出：是否有效、聚合配置错误、警告、解析后的安全摘要。  
约束：候选配置未通过验证时禁止进入 dry-run 或 pilot。

#### `estimate_candidate`（R0）

输入：候选配置 ID、可选 limit。  
实现：复用或抽取当前 `estimate_run()` 与 dry-run 逻辑。  
输出：记录数、批次数、各阶段预计调用数、token/费用估计、预算风险和估算置信级别。

#### `create_candidate`（R1）

输入：父候选 ID、结构化补丁、修改理由。  
输出：新候选 ID、配置 diff、安全检查结果。  
约束：只允许修改白名单中的工程配置键；永不原地覆盖用户的 `project.toml`。

首版允许的修改面：

- `quality.threshold/top_ratio/rubric`；
- `class.*.quality` 条件化阈值；
- `annotate/generate/verify` 指令和有限参数；
- `generate` 数量、样式和温度；
- `generate.stream` 配额、tier 权重、窗口与已支持规则；
- `run.batch_size/seed`；
- 启用算子，但必须通过现有配置约束。

首版禁止修改：

- 输入源和正式输出路径；
- API endpoint、provider、密钥环境变量；
- validator/hook Python 引用；
- 任意脚本路径；
- trace 的全文敏感内容等级；
- Agent 自身的预算和审批策略。

#### `run_pilot`（R2）

输入：候选 ID、固定 pilot manifest、limit。  
输出：退出码、报告路径、产物摘要、实际费用、错误摘要。  
约束：

- 只能写 `out/agent/<run_id>/<candidate_id>/`；
- 单轮和全局预算双重限制；
- 所有候选使用同一批样本、seed 和顺序；
- 失败时保留报告和 trace，不自动无限重试；
- 相同候选摘要与相同 pilot manifest 不重复执行。

#### `analyze_run`（R0）

输入：pilot run ID。  
输出：统一 `EvaluationMetrics`，包括产出率、失败率、拒绝构成、Schema 合法率、重复率、类别分布、生成配额、规则合法率、token 和成本。

#### `compare_candidates`（R0）

输入：两个或多个候选 ID。  
输出：逐指标差异、硬约束违反项、Pareto 支配关系和推荐候选。  
约束：只比较相同 pilot manifest 的结果。

#### `run_full`（R3）

输入：已验证且已试跑的候选 ID。  
输出：正式运行摘要。  
约束：必须人工批准；批准内容冻结候选 digest、预计费用上限和输出路径，任何一项变化都使批准失效。

#### `promote_candidate`（R3）

输入：候选 ID、目标配置路径。  
输出：配置 diff 和备份路径。  
约束：必须人工批准；默认只输出 `final-proposal.toml`，不覆盖源配置。

## 9. 目标、约束与评价模型

新增 `agent.toml`，与工具级 `config.toml`、项目级 `project.toml` 分离：

```toml
schema_version = 1

[agent]
llm = "default"
max_iterations = 5
max_tool_calls = 30
max_pilot_runs = 4
max_wall_time_s = 3600
max_cost_usd = 10.0
approval = "full_run"

[goal]
description = "构建高质量中文意图分类数据集"
min_schema_valid_rate = 1.0
min_emission_rate = 0.75
max_failure_rate = 0.03
max_duplicate_rate = 0.02
max_cost_per_emitted = 0.05

[goal.class_balance]
writing = [0.15, 0.40]
qa = [0.15, 0.40]
translation = [0.10, 0.30]

[policy]
allow_prompt_edits = true
allow_threshold_edits = true
allow_stage_toggle = false
allow_full_run = true
retain_sensitive_trace = false
```

### 9.1 硬约束

硬约束不参与加权，任何一项失败即不得宣告完成：

- Schema 合法率；
- 总费用与单轮费用；
- 最大失败率；
- 路径与权限策略；
- 时间流规则合法率；
- 必需类别或帧类覆盖；
- 人工审批要求。

### 9.2 软目标

软目标用于比较多个均满足硬约束的候选：

- 有效产出率更高；
- 单条有效产物成本更低；
- 类别分布更接近目标；
- 重复率更低；
- 质量分更高；
- token 和墙钟更少；
- 相对基线的配置改动更小。

候选选择默认采用“先硬门控，再 Pareto 比较，最后按用户优先级打破平局”，避免一个不透明的总分掩盖关键退化。

## 10. Planner 上下文与反思

Planner 每轮只接收压缩后的 `AgentObservation`：

- 用户目标与约束；
- 当前候选配置摘要；
- 已执行动作摘要；
- 最近一次评估；
- 与基线/最佳候选的差异；
- 剩余预算、轮数和允许工具；
- Policy 给出的禁止项；
- 最近错误的去敏摘要。

不直接发送：

- API 密钥值；
- 完整 rejects 原文；
- 完整 trace；
- 大批输入数据；
- 工作区任意文件内容；
- 未经数据分类的个人敏感信息。

每次 pilot 后强制生成一份结构化反思：

```json
{
  "hypothesis_result": "partially_supported",
  "improved": ["emission_rate"],
  "regressed": ["class_balance.translation"],
  "root_causes": ["translation threshold remains too strict"],
  "recommended_next_action": "create_candidate",
  "confidence": 0.78
}
```

Reflector 可与 Planner 使用同一模型，但其输出只作为观察，不直接触发动作。关键指标与停止判断由确定性 Evaluator 决定。

## 11. 持久化与可恢复执行

Agent 控制层允许有状态，但不能破坏核心流水线的无状态定位。建议采用版本化 JSON 快照加 JSONL 事件流：

```text
out/agent/<agent_run_id>/
  goal.toml
  state.json
  events.jsonl
  source/
    config.digest.json
    project.toml
  pilot-manifest.json
  candidates/
    candidate-000/
      project.toml
      patch.json
      decision.json
      evaluation.json
      run/
        labels.jsonl
        labels.rejects.jsonl
        labels.report.json
  final-proposal.toml
  final-report.md
```

持久化要求：

- `state.json.part → fsync → replace` 原子更新；
- `events.jsonl` 追加写，每个事件带序号和时间；
- state 保存最近已提交事件序号，用于检测半写；
- 恢复时校验源配置、候选配置和输入摘要；
- 输入变化后旧批准自动失效；
- 工具调用使用幂等键，防止恢复后重复收费；
- 不把密钥、Authorization header 或完整敏感 prompt 写入状态。

## 12. 审批与安全策略

### 12.1 审批对象

审批不是简单的 yes/no，而是冻结以下内容：

- 工具名；
- 候选配置 digest；
- 输入摘要；
- 输出路径；
- 最大费用；
- 最大记录数；
- 过期时间。

### 12.2 路径策略

- 读取范围仅限用户显式给出的输入与项目文件；
- 写入范围默认仅限 `out/agent/<run_id>`；
- 所有路径先解析为绝对路径并校验仍在允许根目录；
- 拒绝 `..` 逃逸、符号链接逃逸和工作区外输出；
- Agent 不得修改 `.git`、`.env`、`mytips.md` 或密钥文件；
- 正式输出路径必须在审批中显式出现。

### 12.3 预算策略

- 计划前检查 `max_cost_usd`；
- 每次调用前检查剩余预算；
- dry-run 估算超过剩余预算时禁止 pilot；
- 实际用量到达软阈值时停止派发新调用；
- 对未配置模型单价的 profile 标记 `cost_unknown`，默认需要人工审批；
- 限制最大 iteration、tool call、pilot run 和墙钟；
- 相同候选失败不得由 Planner 反复重试。

### 12.4 Prompt injection 防护

- 输入数据永远标记为“不可信数据”，不能作为 Agent 指令；
- Planner 只接收统计摘要和固定数量的转义样本；
- 工具名和参数 Schema 由代码提供，数据内容不能新增工具；
- 配置补丁只走白名单字段；
- Tool Router 忽略模型输出中非 Schema 字段；
- 任何要求泄露密钥、扩大路径或取消预算的样本内容都作为数据处理。

## 13. CLI 设计

新增顶层子命令：

```bash
labelkit agent run \
  --config config.toml \
  --project project.toml \
  --goal agent.toml

labelkit agent resume --run-id <id>
labelkit agent status --run-id <id>
labelkit agent approve --run-id <id> --request <request-id>
labelkit agent reject --run-id <id> --request <request-id> --reason "..."
labelkit agent inspect --run-id <id>
```

建议退出码：

- `0`：Agent 已完成目标；
- `1`：已安全停止，但有拒绝记录或 strict 失败；
- `2`：配置或目标无效；
- `3`：输入无效；
- `4`：运行期致命错误；
- `5`：等待人工批准或输入；
- `6`：预算/策略阻止继续；
- `130`：用户取消。

原有命令与退出码不变。

## 14. 代码模块规划

```text
labelkit/agent/
  __init__.py
  model.py          # Goal、State、Decision、Observation、Evaluation 数据契约
  loader.py         # agent.toml 解析与全量错误聚合
  controller.py     # 状态机与主循环
  planner.py        # LLM Planner，输出 AgentDecision
  reflector.py      # 试跑后的结构化反思
  evaluator.py      # 确定性目标检查与候选比较
  policies.py       # 路径、预算、审批、重复动作和字段白名单
  approvals.py      # 审批请求、冻结与失效规则
  state_store.py    # state.json + events.jsonl 原子持久化
  workspace.py      # Agent 独立目录和候选配置生命周期
  tools/
    __init__.py
    registry.py     # 工具 Schema、风险等级与分派
    inspect.py      # inspect_dataset / inspect_project
    candidate.py    # create / validate / compare candidate
    run.py          # estimate / pilot / full run
    report.py       # analyze_run

labelkit/cli/
  agent_commands.py
  agent_parser.py
```

现有模块预计需要的最小调整：

- `labelkit/orchestration/runtime.py`：增加返回结构化 `RunSummary/RunArtifacts` 的库级入口，CLI 薄封装继续返回退出码；
- `labelkit/orchestration/orchestrator.py`：公开稳定的估算入口，避免 Agent 解析 stderr；
- `labelkit/operators/ingest.py`：提供只读数据 profile 或采样接口；
- `labelkit/cli/parser.py`、`commands.py`、`main.py`：挂接 `agent` 子命令；
- `labelkit/common/observability/obslog.py`：增加 Agent 事件类型或独立事件汇；
- `pyproject.toml`：仅在确实需要保留 TOML 注释时评估加入 TOML round-trip 依赖；MVP 优先避免不必要依赖。

## 15. 分阶段实施

### 15.1 正确性优先的改造粒度

以下 Phase 是里程碑，不应作为一次提交整体实现。为了让错误能够被快速定位，每次改造必须形成一个可独立验证的纵向小切片。

单次改造默认遵守以下上限：

- 只解决一个行为目标；
- 只新增一个公开契约、一个工具，或一组紧密相关的状态迁移；
- 修改不超过 5 个生产文件；
- 生产代码通常为 50–250 行，复杂算法最多约 350 行；
- 除文档、测试夹具和自动生成文件外，总 diff 超过约 800 行时必须拆分；
- 测试代码允许达到生产代码的 1–2 倍，不因追求小 diff 而减少边界测试；
- 一次改造不得同时跨越配置解析、Agent 控制器、持久化和 CLI 四层；
- 任何付费或写正式产物的能力，必须在只读契约和 Policy 已稳定后单独接入。

每个原子改造统一经过四道门：

1. **局部门**：新增模块和直接依赖模块的单元/契约测试通过；
2. **回归门**：全部离线测试通过，原有 CLI golden 不变；
3. **跨平台门**：涉及路径、文件交付、信号或终端时必须在 Windows/Linux 验证；
4. **真实端点门**：新增 LLM 决策语义时运行对应 integration case，网络错误与产品错误分开记录。

### 15.2 原子实施批次

| 批次 | 单一目标 | 预计生产代码 | 关键验收 |
|---|---|---:|---|
| P0.1 | 盘点并移除当前树明文密钥，增加防误提交规则 | 0–100 行 | tracked tree 无真实密钥候选 |
| P0.2 | 决定并执行 fork 与 upstream 的基线整合 | 0–200 行冲突修复 | 全部既有测试基线可复现 |
| P0.3 | 修复 Windows TOML 临时路径转义 | 50–150 行 | 配置测试跨平台一致 |
| P0.4 | 修复 Windows 原子覆盖交付 | 30–100 行 | 重跑覆盖与故障路径通过 |
| P0.5 | 增加 Linux/Windows 与 secret scan CI 基线 | 0–150 行 | 两平台 CI 可重复 |
| P1.1 | 定义结构化运行结果契约 | 50–120 行 | 只有数据类型，无行为变化 |
| P1.2 | 暴露结构化 validate API | 80–180 行 | CLI 输出逐字节不变 |
| P1.3 | 暴露结构化 estimate API | 100–220 行 | Agent 不解析 stderr |
| P1.4 | 暴露结构化 execute API | 120–250 行 | 返回 summary 与产物路径 |
| P1.5 | 实现文本输入只读 profile | 100–250 行 | 不触发 LLM、不泄露全文 |
| P1.6 | 扩展 UI/stream 输入 profile | 100–300 行 | 配对、会话和时间摘要正确 |
| P2.1 | 定义 ToolSpec/Call/Result 契约 | 80–180 行 | Schema 与错误闭集冻结 |
| P2.2 | 建立只含测试工具的 Registry/Router | 100–220 行 | 非法工具永不到执行器 |
| P2.3 | 实现路径与敏感文件 Policy | 100–250 行 | 逃逸、链接、密钥路径均拒绝 |
| P2.4 | 实现预算与重复动作 Policy | 100–250 行 | 预算/幂等边界可确定测试 |
| P2.5 | 建立 Agent workspace 生命周期 | 120–280 行 | 只写独立目录、原子创建 |
| P2.6 | 实现 inspect_dataset 工具 | 80–180 行 | 调用 P1 profile，不复制逻辑 |
| P2.7 | 实现 inspect_project 工具 | 80–180 行 | 只返回去敏配置摘要 |
| P2.8 | 实现 validate_candidate 工具 | 80–180 行 | 无效候选不能进入后续工具 |
| P2.9 | 实现 estimate_candidate 工具 | 80–180 行 | 估算与 CLI dry-run 同源 |
| P2.10 | 实现 create_candidate 白名单补丁 | 150–350 行 | 源 TOML 不被覆盖，diff 可审计 |
| P3.1 | 定义 Goal/Decision/Observation/State | 100–250 行 | 纯数据契约与 Schema 测试 |
| P3.2 | 实现 agent.toml Loader | 120–280 行 | 全量错误聚合、无 Controller 依赖 |
| P3.3 | 实现 Planner 单轮结构化决策 | 120–280 行 | 非法 JSON 修复或安全失败 |
| P3.4 | 实现最小 Controller 状态机 | 120–300 行 | 单个 R0 工具后可正常完成 |
| P3.5 | 接入真实 Tool Router | 80–200 行 | Planner 不能绕过 Policy |
| P3.6 | 增加 finish/abort/ask_human 路径 | 80–220 行 | 每种终态有单独测试 |
| P4.1 | 实现固定 pilot manifest | 100–250 行 | 候选使用相同样本与 seed |
| P4.2 | 实现 run_pilot 受限付费工具 | 150–350 行 | 双预算、独立输出、幂等 |
| P4.3 | 定义 EvaluationMetrics 并解析报告 | 120–300 行 | 指标可从产物复算 |
| P4.4 | 实现硬约束 Evaluator | 100–250 行 | 任何硬门失败都不能完成 |
| P4.5 | 实现候选 Pareto 比较 | 100–250 行 | 同 manifest 才允许比较 |
| P4.6 | 实现结构化 Reflector | 100–250 行 | 反思不能直接触发工具 |
| P4.7 | 打通第二候选与第二次 pilot | 120–300 行 | 形成首条真实闭环 |
| P4.8 | 增加无改善、振荡和预算停止 | 100–250 行 | 不会无限循环或重复收费 |
| P4.9 | 逐项开放按类阈值、生成与 stream 调参 | 每项 80–220 行 | 每种配置面单独基准，不合并开放 |
| P5.1 | 实现 append-only Agent events | 100–220 行 | 序号连续、敏感字段去除 |
| P5.2 | 实现原子 state snapshot | 120–280 行 | 半写检测与恢复测试 |
| P5.3 | 实现只读 resume | 80–200 行 | 恢复不执行任何工具 |
| P5.4 | 定义审批冻结和失效契约 | 100–250 行 | digest/输入/预算变化即失效 |
| P5.5 | 实现 approve/reject CLI | 100–250 行 | 未批准不能进入 R3 |
| P5.6 | 允许恢复后继续执行 | 120–300 行 | 已完成付费调用不重复 |
| P5.7 | 实现 run_full | 120–280 行 | 审批内容与执行参数逐项一致 |
| P5.8 | 实现 promote_candidate | 80–200 行 | 默认只交付 proposal，不覆盖源文件 |
| P6.1 | 建立固定 Planner 控制流基准 | 测试为主 | 离线可重复、零网络费用 |
| P6.2 | 增加文本两轮优化 E2E | 测试为主 | 真实指标对比 baseline |
| P6.3 | 增加全天时间流优化 E2E | 测试为主 | 配额、规则、成本均对账 |
| P6.4 | 增加注入、429、溢出与崩溃案例 | 测试为主 | 硬约束违反率为 0 |
| P6.5 | 生成可复算 benchmark 报告 | 0–200 行 | 履历数字有原始产物支撑 |
| P7.1 | 编写 quickstart 与架构文档 | 文档为主 | 15 分钟可跑 dry-run 演示 |
| P7.2 | 增加三分钟演示与样例 trace | 文档/脚本为主 | 无密钥、无真实个人数据 |
| P7.3 | 完成 CI、发布与限制说明 | 0–200 行 | Windows/Linux 全绿 |

上述批次不是必须一一对应 PR，但建议一一对应原子提交。每个验收批次最多聚合 3–6 个连续原子提交，生产代码控制在约 300–800 行，并且只交付一个可描述的纵向能力。

### Phase 0：基线、分支与安全整理

任务：

- 固定当前分支 HEAD、上游基线和本分支自定义功能清单；
- 明确是否先吸收 upstream v1.15/v1.16，避免 Agent 开发期间反复解决同一区域冲突；
- 运行并记录 Linux/Windows 离线测试基线；
- 修复或隔离 Windows TOML 路径、原子覆盖和路径分隔符测试问题；
- 撤销 `mytips.md` 中已暴露的密钥，并从版本历史清理；
- 为 Agent 工作目录、状态与敏感文件更新 `.gitignore`；
- 建立 `docs/dev/SPEC-agent-control-plane.md`，冻结首批契约后再写实现。

验收：

- 工作区无明文密钥；
- 基线测试结果可复现；
- Agent 修改面与现有全天生成改动的冲突清单明确；
- `run/validate/rubric` 黄金输出基线已保存。

### Phase 1：把现有能力整理成可复用库 API

任务：

- 增加不会 `sys.exit`、不要求解析控制台文本的运行入口；
- 结构化暴露 validate、estimate、run summary 和 artifact paths；
- 实现报告读取器和统一指标模型；
- 实现输入 profile，且不触发 LLM；
- 所有 API 对 CLI 保持薄封装关系。

验收：

- Agent 工具无需调用 subprocess；
- 不解析 stderr/stdout 获取业务信息；
- 现有 CLI 黄金测试逐字节不变；
- 新 API 有单元测试和类型契约测试。

### Phase 2：Agent 工具层与策略层

任务：

- 定义 `ToolSpec/ToolCall/ToolResult`；
- 实现工具注册表、参数 Schema 和统一错误模型；
- 完成 R0/R1 工具；
- 实现 Agent workspace 与候选配置；
- 实现字段白名单、路径校验和 digest；
- 实现 pilot 幂等键和预算预检。

验收：

- 未经 Planner 也能从 Python 测试完整调用每个工具；
- 非法路径、禁止字段、重复调用和预算超限均被代码拒绝；
- 工具输出不含密钥和未经允许的数据正文；
- 候选配置永不覆盖源配置。

### Phase 3：Planner、Controller 与单轮执行

任务：

- 定义 `AgentDecision` Schema；
- 使用现有 `LLMClient + SchemaEngine` 实现供应商中立 Planner；
- 实现 Controller 状态机；
- 实现 Observation Builder 和工具分派；
- 增加 iteration、tool-call 和墙钟上限；
- 支持 `finish/abort/ask_human`。

验收：

- Agent 能从自然语言/agent.toml 目标选择第一个工具；
- 无效工具名或参数不能到达执行器；
- Planner 超时、Schema 修复耗尽或模型失败时状态正确收口；
- 使用预定义 Planner 响应可确定性重放整个控制流。

### Phase 4：试跑—评估—调参闭环

任务：

- 建立固定 pilot manifest；
- 实现 `run_pilot/analyze_run/compare_candidates`；
- 实现硬约束、Pareto 比较与停止条件；
- 实现结构化 Reflector；
- 允许 Agent 创建一个新候选并复跑；
- 阻止振荡、重复配置和无改善循环。

验收：

- 至少一个文本示例完成两轮优化；
- 至少一个全天时间流示例完成配额或规则导向的两轮优化；
- 每轮都有“假设—配置 diff—结果—结论”证据链；
- 达标、无改进、预算耗尽三种终止路径均有测试。

### Phase 5：持久化、审批与全量运行

任务：

- 实现原子 state store 与 append-only events；
- 实现 `resume/status/approve/reject`；
- 实现审批冻结与失效规则；
- 接入 R2/R3 工具审批；
- 实现优雅中断和恢复幂等；
- 输出 `final-proposal.toml` 与最终报告。

验收：

- 在 `awaiting_approval` 退出后可从新进程恢复；
- 修改候选、输入、预算或输出路径会使旧批准失效；
- 崩溃恢复不会重复执行已完成的付费调用；
- 未批准时绝不能执行 `run_full/promote_candidate`。

### Phase 6：Agent 评测体系

任务：

- 建立 `tests/agent/fixtures` 和小型基准数据；
- 记录静态配置 baseline；
- 增加 Agent 成功率、工具选择正确率和约束违反率；
- 对比产出率、失败率、重复率、类别覆盖、成本和调用数；
- 增加 prompt injection、错误报告、429、Schema 失败和预算耗尽案例；
- 真实端点测试继续使用 integration marker，离线控制流使用固定响应脚本而非伪造业务成功。

验收：

- 离线测试可重复且不产生网络费用；
- 真实端点 E2E 至少覆盖普通文本与全天合成流；
- Agent 不得在任何评测中违反硬约束；
- 最终对照报告能给出可复核的真实提升数据。

### Phase 7：作品化与发布准备

任务：

- 编写 Agent quickstart、架构说明、安全模型和故障排查；
- 增加三分钟演示脚本；
- 输出一份完整 Agent trace 示例；
- 增加 Linux/Windows CI；
- 生成 baseline vs Agent 对照图表；
- 补充限制、失败案例与成本说明；
- 决定是否增加 OpenAI Agents SDK/LangGraph 可选适配器，核心实现仍保持供应商中立。

验收：

- 新用户可在 15 分钟内跑通 dry-run Agent 演示；
- README 能清楚区分 Pipeline 与 Agent；
- CI 全绿且仓库不含密钥；
- 所有履历指标均可由评测产物复算。

## 16. 测试策略

### 16.1 单元测试

- agent.toml 全量错误聚合；
- 状态迁移合法性；
- AgentDecision Schema；
- 工具参数和结果 Schema；
- 预算扣减；
- 路径与补丁白名单；
- digest、幂等键和审批失效；
- Evaluator 硬门与 Pareto 比较；
- 状态原子写入与损坏恢复。

### 16.2 契约测试

- 每个工具的稳定输入输出字段；
- Agent event 的闭集名称与 payload；
- `state.json` schema_version 兼容策略；
- Planner prompt 与内部 Schema 快照；
- CLI 退出码与 stdout/stderr 职责。

### 16.3 集成测试

- Controller + 固定 Planner 响应 + 真实工具；
- 从 inspect 到 candidate validate；
- 从 pilot 到 evaluation 再到第二候选；
- pause → approve → resume；
- SIGINT 后恢复；
- Windows/Linux 文件交付。

### 16.4 真实 LLM E2E

- Planner 能根据真实失败报告选择合理工具；
- 文本数据两轮治理；
- 全天合成流配额/规则调优；
- 模型返回非法决策 JSON 时由 SchemaEngine 修复或安全失败；
- 429、鉴权错误、上下文溢出和输出截断时不突破预算与权限。

### 16.5 Agent 质量评测

- 目标完成率；
- 平均迭代数；
- 无效工具调用率；
- 重复动作率；
- 人工介入率；
- 硬约束违反率，目标必须为 0；
- 相对 baseline 的数据质量提升；
- 相对 baseline 的成本变化；
- 运行可重放率。

## 17. 可观测性

新增 Agent 事件建议使用闭集：

- `agent.run.start/end`；
- `agent.iteration.start/end`；
- `agent.observation.ready`；
- `agent.decision`；
- `agent.tool.start/end/fail/blocked`；
- `agent.candidate.created/validated`；
- `agent.evaluation`；
- `agent.approval.requested/approved/rejected/expired`；
- `agent.budget.warning/exhausted`；
- `agent.resume`；
- `agent.stop`。

Agent trace 默认只保存引用与摘要。若用户显式允许保存内容，也要先去除密钥、Authorization header 和已知敏感字段。

最终报告至少包含：

- 目标与约束；
- 源配置和最终配置摘要；
- 候选关系图；
- 每轮假设、动作和结果；
- baseline/final 指标对比；
- token、费用和墙钟；
- 审批记录；
- 未解决问题与人工建议；
- 产物路径与 digest。

## 18. 关键风险与应对

| 风险 | 后果 | 应对 |
|---|---|---|
| Agent 只是固定脚本 | 履历说服力不足 | 记录并测试真实动态工具选择和重规划 |
| LLM 反复调参振荡 | 浪费费用 | 候选 digest 去重、最小改善量、最大迭代 |
| 为了产出率降低质量门 | 指标投机 | 硬约束 + 多指标 Pareto + 人工抽检 |
| pilot 样本变化 | 候选不可比较 | 固定 manifest、seed、顺序和模型 profile |
| Agent 修改危险配置 | 数据或费用风险 | 白名单补丁、候选工作区、审批冻结 |
| 输入 prompt injection | 越权工具调用 | 数据/指令隔离、严格工具 Schema、Policy 二次检查 |
| 未知模型价格 | 无法控制预算 | `cost_unknown` 默认审批，不声明达标 |
| 恢复后重复付费 | 成本失控 | 幂等键、事件序号、调用前后持久化 |
| 分支落后 upstream | 后续冲突扩大 | Phase 0 冻结整合策略，再开始大规模开发 |
| 多 Agent 过早复杂化 | 难测试、收益不明 | MVP 坚持单 Agent，专家能力先实现为工具 |
| 明文密钥进入仓库 | 安全事故 | 立即轮换、历史清理、secret scan 和 CI |

## 19. 建议提交拆分

为降低回滚和评审成本，实际开发以 §15.2 的约 35–45 个原子批次为准。下面十项是里程碑提交组，不应各自压成一个超大提交：

1. `docs(agent): freeze control-plane design and contracts`
2. `refactor(runtime): expose structured validate estimate and run APIs`
3. `feat(agent): add goal config and state contracts`
4. `feat(agent): add guarded tool registry and workspace`
5. `feat(agent): add candidate lifecycle and deterministic evaluator`
6. `feat(agent): add planner and controller state machine`
7. `feat(agent): close pilot evaluate revise loop`
8. `feat(agent): add persistence approvals and resume`
9. `test(agent): add benchmark and adversarial suites`
10. `docs(agent): add quickstart demo and evaluation report`

每个提交都应满足：现有非 Agent CLI 行为不变、离线测试可运行、无密钥、无临时产物进入 Git。

## 20. 里程碑与时间建议

按单人兼职开发估算：

| 周期 | 目标 | 主要交付 |
|---|---|---|
| 第 1 周 | Phase 0–1 | 安全基线、结构化运行 API、输入与报告分析 |
| 第 2 周 | Phase 2–3 | 工具层、Policy、Planner、Controller 单轮运行 |
| 第 3 周 | Phase 4 | 两轮自动优化闭环、文本演示 |
| 第 4 周 | Phase 5 | 状态持久化、审批、恢复、全量运行 |
| 第 5 周 | Phase 6 | 全天时间流 Agent、基准与故障注入 |
| 第 6 周 | Phase 7 | CI、文档、演示、履历数据与发布整理 |

若时间不足，最低可展示版本应完成 Phase 0–4；没有评估闭环的版本不应宣传为自主 Agent。

## 21. MVP 验收演示

最终应能演示以下完整故事：

1. 用户提交自然语言目标与 `agent.toml`；
2. Agent 检查输入和当前配置；
3. Agent 发现静态配置的预计成本或质量风险；
4. Agent 创建并验证候选配置；
5. Agent 对固定样本执行 pilot；
6. Evaluator 指出未达标指标；
7. Agent 给出假设并创建第二候选；
8. 第二次 pilot 达到硬约束且优于 baseline；
9. Agent 请求批准全量运行；
10. 用户批准后从持久化状态恢复并执行；
11. 最终报告完整展示决策链、配置 diff、成本与质量变化。

## 22. 履历交付目标

完成后可将项目描述为：

> 设计并实现供应商中立的自主 DatasetOps Agent，将确定性 LLM 数据流水线封装为受约束工具集；支持数据剖析、配置规划、固定样本试跑、指标评估、自动迭代、预算控制、人工审批、暂停恢复及全链路审计，并通过文本与全天时间流基准对比静态配置和 Agent 优化效果。

履历中的提升百分比、成本下降和成功率必须来自 Phase 6 的真实评测产物，不预先编造。

## 23. 下一步执行顺序

计划通过后，建议立即按以下顺序开始：

1. 完成密钥轮换与 Git 历史清理；
2. 决定当前 fork 与 upstream v1.16 的整合方式；
3. 修复并记录 Windows 测试基线；
4. 编写 `SPEC-agent-control-plane.md`，冻结数据结构和工具契约；
5. 从 Phase 1 的结构化库 API 开始实现；
6. 在任何 Planner 代码之前先完成工具 Policy 与候选工作区；
7. 先跑通文本两轮闭环，再扩展到全天合成时间流；
8. 用真实评测数据决定是否值得增加多 Agent 或外部 Agent 框架。

## 24. 执行记录

### 2026-08-22：P0.1 当前树密钥治理

状态：**仓库内当前树检查完成；外部轮换与历史净化待人工授权。**

已完成：

- 当前工作分支确认为 `feat/all-day-stream-generation-zj`；
- 当前工作树中不存在 `mytips.md`；
- `.gitignore` 已包含 `mytips.md`，本地重新创建后不会被普通 `git add` 误收；
- 对当前 tracked tree 执行高置信度 `sk-...` 候选扫描；
- 唯一命中文件为 `tests/common/runtime/test_llm_client.py`，命中项是测试用 `sentinel` 假值，不是运行密钥；
- 已确认包含历史泄露的提交 `10ff733` 不是当前 Agent 分支 HEAD 的祖先，因此当前分支不会把该文件带入后续 Agent 提交。

仍需完成：

- 在相应模型服务后台撤销或轮换曾写入 `mytips.md` 的所有密钥；
- 明确是否对个人 `main`、`origin/main` 及其他包含 `10ff733` 的引用执行历史净化；
- 历史净化需要重写提交并强制更新远端，属于破坏性协作操作，必须在确认受影响分支、远端和协作者后单独执行；
- P0.5 增加自动 secret scan 后，才能把“防止再次提交”从 `.gitignore` 软防护提升为 CI 硬门。

判定：P0.1 的“当前 Agent 分支不携带明文密钥”验收已通过；整体密钥事件在外部轮换和历史净化完成前仍保持开放状态。

### 2026-08-22：P0.2 fork 与 upstream v1.16 基线整合

状态：**代码整合与核心验证完成；Windows 配置测试夹具问题转入 P0.3。**

已完成：

- 在 `feat/all-day-stream-generation-zj` 上提交本计划，提交为 `81e7ea7`；
- 创建本地恢复分支 `backup/pre-agent-upstream-merge-20260822`，固定合并前状态；
- 获取并合并 `upstream/main` 的 v1.16 基线 `9938032`；
- 冲突解决采用上游 v1.16 的模块拆分，并把 fork 的 `time_profiles`、毫秒时间字段与日历字段迁移到新模块；
- 保留上游 `tiers`、`rules`、`windows`、联合规划器和时序运行时实现；
- `tests/operators/test_generate_stream.py` 通过：78 passed；
- 五个带 `time_profiles` 的 synth-stream 示例均通过真实 CLI 配置验证；
- 排除配置测试目录后的离线回归为 1596 passed、1 skipped、25 failed；失败均已定位为合并前计划中列出的 Windows 路径、覆盖式 rename、控制台输入或冻结文件清单问题；
- 生产代码通过 `py_compile`，工作树通过 `git diff --check`，不存在冲突标记。

已知基线问题：

- `tests/common/config/test_loader_generate_stream.py` 在 Windows 下生成未转义的临时路径，TOML 在业务校验前报 `Invalid hex value`；本次观察到 170 failed、78 passed，失败具有同一根因；
- 其余 25 个失败主要来自 Windows 下目标文件已存在时 `Path.rename`/`os.rename` 不覆盖、路径分隔符断言、控制台按键轮询，以及 fork 新增测试未进入上游冻结文件清单；
- 这些问题按计划归入 P0.3/P0.4，不在 P0.2 合并中夹带跨平台修复。

判定：P0.2 已达到“上游能力与 fork 自定义能力同时保留、核心纯逻辑可复现”的合并验收条件；下一步执行 P0.3。
