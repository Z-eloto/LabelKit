"""run、validate 与 estimate 的运行期对象图装配。

本模块只管装配：不解析 argparse 命名空间、不打印面向用户的文本、不把异常映射为退出码、
也不实现任何算子行为。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from labelkit.common.config import load, load_with_diagnostics
from labelkit.common.config.model import CliOverrides, ResolvedConfig
from labelkit.common.observability.obslog import EventLog, MetricsSink, setup_logging
from labelkit.common.runtime.llm_client import LLMClient
from labelkit.common.runtime.schema_engine import SchemaEngine
from labelkit.orchestration.factory import build_stages
from labelkit.orchestration.orchestrator import (
    Orchestrator,
    RunServices,
    estimate_assumptions,
    estimate_run,
)
from labelkit.orchestration.profile_usage import referenced_profiles
from labelkit.orchestration.results import RunEstimate, ValidationResult
from labelkit.operators.emitter import Emitter

if TYPE_CHECKING:
    from labelkit.common.observability.obslog import ProgressListener
    from labelkit.common.runtime.llm_client import ProbeResult
    from labelkit.common.config.model import TraceConfig
    from labelkit.operators.ingest import Ingestor

__all__ = [
    "execute_run",
    "estimate_project",
    "probe_referenced_profiles",
    "validate_project",
    "validate_project_result",
]

_log = logging.getLogger("labelkit.runtime")


def estimate_project(cfg: ResolvedConfig) -> RunEstimate:
    """只读扫描已校验工程并返回结构化静态估算。

    process 模式执行一次 M2 ``scan(estimate=True)``；generate_only 模式不读取输入。
    本入口不构造 LLMClient、Emitter 或 trace/report 通道，不打印控制台文本。

    @param cfg: 经 M1 校验的冻结配置，可直接取自 ValidationResult.config
    @return: 带配置摘要、调用明细和估算口径的结构化结果
    @raises InputError: process 模式输入缺失、不可读或配对策略要求失败
    """
    plan = None
    if cfg.run.mode == "process":
        from labelkit.operators.ingest import Ingestor
        plan = Ingestor(cfg).scan(estimate=True)
    raw = estimate_run(cfg, plan)
    calls = {
        key: int(value)
        for key, value in raw.items()
        if key not in ("records", "batches", "total_calls")
    }
    return RunEstimate(
        config_digest=cfg.config_digest,
        project_digest=cfg.project_digest,
        mode=cfg.run.mode,
        modality=cfg.run.modality,
        records=int(raw["records"]),
        batches=int(raw["batches"]),
        calls=calls,
        total_calls=int(raw["total_calls"]),
        assumptions=estimate_assumptions(cfg),
    )


def _activate_listener(listener: "ProgressListener", cfg: ResolvedConfig,
                       llm: LLMClient, metrics: MetricsSink) -> None:
    """v1.10（U19 调用时序）：把懒壳渲染器激活恰一次——在对象图装配完成之后、
    ``asyncio.run`` 之前，交给它 ResolvedConfig 与渲染 tick 用的三个只读拉取闭包
    （``LLMClient.snapshot``、MetricsSink 计数器、熔断连击数）。

    U23 的失败纪律：一条 WARN，然后把汇上的 listener 引用整轮置 None（``_listener``
    是既定的 Wave-1 存放位——``MetricsSink._forward`` 在自己转发失败时清的也是同一属性）。
    渲染器的 bug 永远影响不到退出码与输出。

    @param listener: 控制台面板的进度监听器
    @param cfg: 已解析配置
    @param llm: M9 LLM 客户端（提供 snapshot 拉取面）
    @param metrics: M12 计数与事件汇
    """
    try:
        listener.on_run_context(cfg, llm.snapshot,
                                lambda: dict(metrics.counters),
                                lambda: metrics.fatal_streak)
    except Exception as exc:  # noqa: BLE001 — bypass isolation (U7/U23)
        metrics._listener = None
        _log.warning("console listener failed, panel bypass disabled: %s", exc,
                     extra={"stage": "run", "batch": 0})


def _trace_config(cfg: ResolvedConfig) -> "TraceConfig":
    """取本轮实际使用的 trace 配置：dry-run 时把路径改道到 ``<name>.dryrun<suffix>``
    （P2-4——空跑绝不覆盖上一轮的 trace 文件）。

    @param cfg: 已解析配置
    @return: trace 配置（非 dry-run 时原样返回）
    """
    trace_cfg = cfg.trace
    if cfg.dry_run and trace_cfg.enabled and trace_cfg.path:
        path = Path(trace_cfg.path)
        trace_cfg = replace(
            trace_cfg,
            path=str(path.with_name(path.stem + ".dryrun" + path.suffix)),
        )
    return trace_cfg


def _build_ingestor(cfg: ResolvedConfig, metrics: MetricsSink) -> "Ingestor | None":
    """构造 M2 摄取器并接上 trace 通路；generate_only 模式没有摄取器。

    @param cfg: 已解析配置
    @param metrics: M12 计数与事件汇
    @return: 摄取器实例；generate_only 模式返回 None
    """
    if cfg.run.mode != "process":
        return None
    from labelkit.operators.ingest import Ingestor

    ingestor = Ingestor(cfg)
    ingestor.metrics = metrics
    return ingestor


def execute_run(
    config_path: str | Path,
    project_path: str | Path,
    overrides: CliOverrides,
    listener: "ProgressListener | None" = None,
) -> int:
    """加载配置、装配运行期对象图、执行一轮运行。

    v1.10（U19）：``listener`` 是控制台面板的进程内旁路——构造 MetricsSink 时接入，事件
    循环启动前经 ``on_run_context`` 激活；传 None（v1.10 之前的全部调用方）与 v1.9 逐字节
    一致。

    @param config_path: 工具级 config.toml 路径
    @param project_path: 工程级 project.toml 路径
    @param overrides: CLI 覆盖项
    @param listener: 控制台面板进度监听器；None 表示不挂面板
    @return: 进程退出码
    """
    cfg = load(Path(config_path), Path(project_path), overrides)
    setup_logging(cfg)
    run_id = secrets.token_hex(6)
    run_started_at = datetime.now().astimezone()

    event_log = EventLog(_trace_config(cfg), run_id)
    metrics = MetricsSink(cfg, run_id, event_log, listener=listener)
    llm = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, metrics)
    schema_engine = SchemaEngine(dict(cfg.user_schema), llm, cfg.output, metrics)
    services = RunServices(llm=llm, schema_engine=schema_engine, metrics=metrics,
                           run_id=run_id, run_started_at=run_started_at)
    orchestrator = Orchestrator(
        cfg,
        build_stages(cfg),
        _build_ingestor(cfg, metrics),
        Emitter(cfg, schema_engine, run_id, run_started_at),
        services,
    )
    if listener is not None:
        _activate_listener(listener, cfg, llm, metrics)
    try:
        summary = asyncio.run(orchestrator.run())
    finally:
        event_log.close()
    return summary.exit_code


def validate_project(
    config_path: str | Path,
    project_path: str | Path,
    overrides: CliOverrides = CliOverrides(),
) -> ResolvedConfig:
    """加载并完整校验一对工具/工程配置。

    v1.10（U27）：CLI 把它解析出的覆盖项一并传进来，好让 ``--console`` 在 validate 路径上
    也抵达 M1（jsonl × 显式 rich 的 WARN 在这里同样会响）；既有调用方保持零覆盖的默认值。

    @param config_path: 工具级 config.toml 路径
    @param project_path: 工程级 project.toml 路径
    @param overrides: CLI 覆盖项
    @return: 已解析配置
    """
    return load(Path(config_path), Path(project_path), overrides)


def validate_project_result(
    config_path: str | Path,
    project_path: str | Path,
    overrides: CliOverrides = CliOverrides(),
) -> ValidationResult:
    """完整校验配置并返回不含控制台渲染的结构化结果。

    此入口不打印 warning，也不把配置错误转换为异常；调用方可直接读取完整的 errors 与
    warnings。文件读取等未归类为配置诊断的内部异常仍正常传播。

    @return: 成功时携带 ResolvedConfig；失败时 config 为 None 且 errors 非空
    """
    cfg, errors, warnings = load_with_diagnostics(
        Path(config_path), Path(project_path), overrides,
    )
    return ValidationResult(
        valid=not errors,
        config=cfg,
        errors=errors,
        warnings=warnings,
    )


def probe_referenced_profiles(cfg: ResolvedConfig) -> tuple["ProbeResult", ...]:
    """探测被启用阶段实际引用的每一个 profile。

    @param cfg: 已解析配置
    @return: 按 (LLM, embedding) 顺序排布的探测结果元组
    """
    llm_names, emb_names = referenced_profiles(cfg)
    client = LLMClient(cfg.llm_profiles, cfg.embedding_profiles, None)

    async def _probe_all() -> list[ProbeResult]:
        """依次探测全部被引用 profile。

        @return: 探测结果列表
        """
        results: list[ProbeResult] = []
        for name in (*llm_names, *emb_names):
            results.extend(await client.probe_all(name))
        return results

    return tuple(asyncio.run(_probe_all()))
