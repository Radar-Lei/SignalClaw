"""
evoprog.daemon: 进化守护进程主入口。

用法: python -m evoprog.daemon [--generations N] [--pop-size N] [--config path]

实现 "生成 -> 测试 -> 进化 -> 固化" 完整循环，支持多控制模式（单模式和组合模式）。
"""

import csv
import json
import logging
import os
import random
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout

from evoprog.config import ExecutorConfig, EvaluatorConfig
from evoprog.config_loader import DaemonConfig, load_config, parse_args
from evoprog.evaluator.control_modes import ModeControlFnFactory, ComboControlFnFactory
from evoprog.evaluator.obs_builder import build_obs_from_traci, extract_lane_links, extract_phase_move_map
from evoprog.evaluator.ranker import batch_normalize_and_score, compute_absolute_fitness, generalization_score, rank_strategies
from evoprog.evaluator.runner import EvaluationResult, make_phase_selection_fn
from evoprog.evaluator.scheduler import evaluate_strategy_multi_scenario
from evoprog.evolution.diagnostics import compute_cmr
from evoprog.evolution.population import create_seed_population, generate_next_population
from evoprog.evolution.signals import EvolutionSignals, extract_signals, signals_to_direction
from evoprog.llm.client import StrategyLLMClient
from evoprog.llm.prompt import SYSTEM_PROMPT
from evoprog.store import AssetStore, Capsule, Gene

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局信号标志
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    """SIGINT/SIGHUP 处理：只设标志位，不做 I/O（signal handler 内 I/O 不安全）。"""
    global _shutdown_requested
    _shutdown_requested = True
    sig_name = signal.Signals(signum).name
    print(f"\n[Daemon] 收到 {sig_name}，将在当代完成后优雅退出...")


# ---------------------------------------------------------------------------
# Checkpoint 管理
# ---------------------------------------------------------------------------

def _load_checkpoint(store_dir: Path) -> dict:
    """加载 checkpoint.json，不存在时返回初始状态。"""
    cp_path = store_dir / "checkpoint.json"
    if cp_path.exists():
        try:
            with open(cp_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "completed_generations": -1,
        "best_fitness_history": [],
        "stagnation_count": 0,
        "best_gene_id": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_checkpoint(store_dir: Path, checkpoint: dict) -> None:
    """原子写入 checkpoint.json（先写 .tmp 再 os.replace）。"""
    checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
    cp_path = store_dir / "checkpoint.json"
    tmp_path = cp_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp_path), str(cp_path))


# ---------------------------------------------------------------------------
# CSV 输出
# ---------------------------------------------------------------------------

def _append_csv(store_dir: Path, generation: int, best_fitness: float, avg_fitness: float, cmr: float = 0.0) -> None:
    """追加一行到 fitness_history.csv（首次写入时添加 header）。"""
    csv_path = store_dir / "fitness_history.csv"
    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["generation", "best_fitness", "avg_fitness", "cmr"])
        writer.writerow([generation, f"{best_fitness:.6f}", f"{avg_fitness:.6f}", f"{cmr:.4f}"])


def _append_trace_log(store_dir: Path, generation: int, population: list, absolute_scores: list, all_avg_metrics: list) -> None:
    """追加当代所有候选的轨迹日志到 trace_log.jsonl。

    记录每个候选的 gene_id、parent_id、代码、fitness 和指标，
    供后续提取 DPO 偏好对和分析 CMR 使用。
    """
    trace_path = store_dir / "trace_log.jsonl"
    with open(trace_path, "a", encoding="utf-8") as f:
        for i, gene in enumerate(population):
            fitness = absolute_scores[i] if i < len(absolute_scores) else float("-inf")
            metrics = all_avg_metrics[i] if i < len(all_avg_metrics) else {}
            entry = {
                "generation": generation,
                "gene_id": gene.id,
                "parent_id": gene.parent_id,
                "inlane_code": gene.inlane_code,
                "outlane_code": gene.outlane_code,
                "fitness": fitness,
                "metrics": metrics,
                "control_mode": gene.control_mode,
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 控制台摘要
# ---------------------------------------------------------------------------

def _log_gen_summary(gen: int, best: float, avg: float, signals: EvolutionSignals, solidified: bool, unique_count: int = 0, pop_size: int = 0, cmr: float = 0.0) -> None:
    """打印每代摘要到控制台。

    格式: [Gen N] best=X.XXXX avg=X.XXXX cmr=X.XX% unique=M/N signals=[...] solidified=True/False
    """
    active = []
    for field_name in [
        "high_queue_detected",
        "low_throughput",
        "high_delay",
        "performance_gain",
        "performance_decline",
        "force_innovation",
    ]:
        if getattr(signals, field_name):
            active.append(field_name)
    signal_str = str(active) if active else "[]"
    unique_str = f" unique={unique_count}/{pop_size}" if pop_size > 0 else ""
    cmr_str = f" cmr={cmr*100:.1f}%"
    print(f"[Gen {gen}] best={best:.4f} avg={avg:.4f}{cmr_str}{unique_str} signals={signal_str} solidified={solidified}")


# ---------------------------------------------------------------------------
# ControlFnFactory（可 pickle 的控制函数工厂，保留向后兼容）
# ---------------------------------------------------------------------------

class ControlFnFactory:
    """可 pickle 的控制函数工厂，用于 ProcessPoolExecutor 跨进程传递。

    在子进程内被调用（不是被 pickle 传递 control_fn），
    因此工厂本身必须可 pickle（类实现，避免 closure）。

    CTRL-01 端到端连接：内部使用 build_obs_from_traci 构建观测向量。

    注意：新代码应使用 ModeControlFnFactory。保留此类仅为向后兼容。
    """

    def __init__(self, gene: Gene, executor_config: ExecutorConfig):
        self.gene = gene
        self.executor_config = executor_config

    def __call__(self):
        """被 evaluate_one_scenario 在子进程内调用，返回 control_fn。"""
        gene = self.gene
        # 缓存每个信号灯 ID 的 lane_links/phase_move_map/controlled_lanes
        lane_links_cache = {}

        def control_fn(traci_module):
            tl_ids = traci_module.trafficlight.getIDList()
            for tl_id in tl_ids:
                if tl_id not in lane_links_cache:
                    ll, unique_lanes = extract_lane_links(traci_module, tl_id)
                    pm = extract_phase_move_map(traci_module, tl_id)
                    lane_links_cache[tl_id] = (ll, pm, unique_lanes)

                ll, pm, ul = lane_links_cache[tl_id]
                obs = build_obs_from_traci(traci_module, tl_id, ul)

                from evoprog.executor.runner import compute_phase_values
                phase_values = compute_phase_values(
                    inlane_code=gene.inlane_code,
                    outlane_code=gene.outlane_code,
                    obs=obs,
                    lane_links_per_move=ll,
                    phase_move_map=pm,
                )
                if phase_values:
                    best_phase = int(phase_values.index(max(phase_values)))
                    traci_module.trafficlight.setPhase(tl_id, best_phase)

        return control_fn


# ---------------------------------------------------------------------------
# 单模式一代执行
# ---------------------------------------------------------------------------

def _run_single_mode_generation(
    mode: str,
    gen: int,
    config: DaemonConfig,
    stores: dict,
    checkpoints: dict,
    populations: dict,
    elites: dict,
    metrics_histories: dict,
    stagnation_counts: dict,
    historical_bests: dict,
    llm_client: StrategyLLMClient,
    executor_config: ExecutorConfig,
) -> tuple[float, float, EvolutionSignals, bool, float]:
    """执行单模式一代的完整进化逻辑。

    Returns:
        (best_fitness, avg_fitness, signals, solidified, cmr) 元组
    """
    store = stores[mode]
    mode_dir = Path(config.store.store_dir) / mode

    # 生成种群
    if gen == 0 and not elites[mode]:
        population = create_seed_population(config.evolution.pop_size, store, control_mode=mode)
    else:
        capsules = store.read_capsules()
        pre_signals = extract_signals(
            current_metrics=metrics_histories[mode][-1] if metrics_histories[mode] else {},
            history=metrics_histories[mode][:-1] if metrics_histories[mode] else [],
            stagnation_count=stagnation_counts[mode],
            stagnation_threshold=config.evolution.stagnation_threshold,
        )
        population = generate_next_population(
            current_elite=elites[mode],
            capsules=capsules,
            signals=pre_signals,
            pop_size=config.evolution.pop_size,
            elite_count=config.evolution.elite_count,
            llm_client=llm_client,
            store=store,
            generation=gen,
            metrics=metrics_histories[mode][-1] if metrics_histories[mode] else {},
            control_mode=mode,
            target_event_type=getattr(config.evolution, "target_event_type", ""),
        )

    populations[mode] = population

    # 评估每个策略 — 并行评估所有 (gene × scenario) 组合
    from evoprog.evaluator.scheduler import evaluate_one_scenario, _resolve_sumocfg_path
    from concurrent.futures import ProcessPoolExecutor

    # 构建所有 factory
    factories = []
    for gene in population:
        if config.evolution.dispatcher_context and config.fixed_skills:
            from evoprog.evaluator.event_dispatcher import DispatcherContextControlFnFactory
            fixed_genes = {}
            for evt, skill_cfg in config.fixed_skills.items():
                fixed_genes[evt] = Gene(
                    id=f"fixed_{evt}",
                    inlane_code=skill_cfg.inlane_code,
                    outlane_code=skill_cfg.outlane_code,
                    control_mode="phase_selection",
                )
            factories.append(DispatcherContextControlFnFactory(
                fixed_skills=fixed_genes,
                candidate_gene=gene,
                candidate_event_type=config.evolution.target_event_type,
                executor_config=executor_config,
            ))
        else:
            factories.append(ModeControlFnFactory(gene, executor_config))

    # 解析场景路径
    scenario_paths = [_resolve_sumocfg_path(p) for p in config.scenario_dirs]
    scenario_paths = [p for p in scenario_paths if p]
    n_scenarios = len(scenario_paths)

    # 扁平化：所有 (factory, scenario) 组合
    eval_args = []
    for factory in factories:
        for spath in scenario_paths:
            eval_args.append((spath, factory, config.evaluator))

    # 并行评估（所有 gene × scenario 一起跑）
    n_workers = min(len(eval_args), max(config.evolution.pop_size, n_scenarios))
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        flat_results = list(executor.map(evaluate_one_scenario, eval_args))

    # 重组结果：flat -> [gene_idx][scenario_idx]
    all_raw_results: list[list] = []
    all_avg_metrics: list[dict] = []
    for gi in range(len(population)):
        results_list = flat_results[gi * n_scenarios : (gi + 1) * n_scenarios]
        all_raw_results.append(results_list)

        successful_results = [r for r in results_list if r is not None]
        avg_metrics: dict = {}
        if successful_results:
            avg_metrics = {
                "avg_delay": sum(r.avg_delay for r in successful_results) / len(successful_results),
                "avg_queue": sum(r.avg_queue for r in successful_results) / len(successful_results),
                "avg_throughput": sum(r.avg_throughput for r in successful_results) / len(successful_results),
            }
        all_avg_metrics.append(avg_metrics)

    # 跨策略批量归一化（代内排名用）
    ranking_scores = batch_normalize_and_score(all_raw_results, config.evaluator)

    # 绝对 fitness（跨代比较用：固化、停滞检测）
    absolute_scores = []
    for raw_results in all_raw_results:
        scenario_abs = [
            compute_absolute_fitness(r, config.evaluator) if r is not None else None
            for r in raw_results
        ]
        absolute_scores.append(generalization_score(scenario_abs))

    # 记录评估日志 + 轨迹日志
    for i, gene in enumerate(population):
        success = absolute_scores[i] > float("-inf")
        store.log_evaluated(gene.id, gen, success, all_avg_metrics[i])

    _append_trace_log(mode_dir, gen, population, absolute_scores, all_avg_metrics)

    # 计算 CMR（子代严格优于父代的比例）
    candidate_fitnesses = absolute_scores
    parent_fitnesses: list[Optional[float]] = []
    # 构建 gene_id -> 最佳历史 fitness 映射（从 trace_log 或 metrics_history）
    gene_fitness_cache: dict[str, float] = {}
    # 先从当代以前的历史中收集
    trace_path = mode_dir / "trace_log.jsonl"
    if trace_path.exists():
        with open(trace_path, encoding="utf-8") as tf:
            for line in tf:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry["generation"] < gen:  # 只看历史代
                    gid = entry["gene_id"]
                    f = entry.get("fitness", float("-inf"))
                    if f > float("-inf"):
                        if gid not in gene_fitness_cache or f > gene_fitness_cache[gid]:
                            gene_fitness_cache[gid] = f

    for gene in population:
        if gene.parent_id and gene.parent_id in gene_fitness_cache:
            parent_fitnesses.append(gene_fitness_cache[gene.parent_id])
        else:
            parent_fitnesses.append(None)

    gen_cmr = compute_cmr(candidate_fitnesses, parent_fitnesses)

    # 代内排名（用归一化分数）
    ranked = rank_strategies(population, ranking_scores, k=config.evolution.pop_size)
    elites[mode] = [g for g, _ in ranked]

    # 绝对 fitness 统计（用于跨代比较）
    valid_abs = [s for s in absolute_scores if s > float("-inf")]
    if valid_abs:
        best_fitness = max(valid_abs)
        avg_fitness = sum(valid_abs) / len(valid_abs)
    else:
        best_fitness = 0.0
        avg_fitness = 0.0

    # 当代最佳策略的指标
    best_idx = absolute_scores.index(best_fitness) if absolute_scores and best_fitness != 0.0 else 0
    best_eval_metrics = all_avg_metrics[best_idx] if all_avg_metrics else {}

    current_gen_metrics = {
        "avg_queue": best_eval_metrics.get("avg_queue", 0.0),
        "avg_throughput": best_eval_metrics.get("avg_throughput", 0.0),
        "avg_delay": best_eval_metrics.get("avg_delay", 0.0),
        "best_fitness": best_fitness,
    }
    metrics_histories[mode].append(current_gen_metrics)

    # 进化信号提取
    signals = extract_signals(
        current_metrics=current_gen_metrics,
        history=metrics_histories[mode][:-1],
        stagnation_count=stagnation_counts[mode],
        stagnation_threshold=config.evolution.stagnation_threshold,
    )

    # 固化判断（使用绝对 fitness 跨代比较）
    solidified = False
    best_fitness_history = checkpoints[mode].get("best_fitness_history", [])
    prev_best = max(best_fitness_history) if best_fitness_history else float("-inf")
    if best_fitness > prev_best:
        if elites[mode]:
            best_gene = elites[mode][0]
            capsule = Capsule(
                gene_id=best_gene.id,
                metrics=current_gen_metrics,
                solidified_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                generation=gen,
            )
            store.append_capsule(capsule)
            store.log_solidified(best_gene.id, gen)
            solidified = True

    # 更新历史和停滞计数
    best_fitness_history_list = list(best_fitness_history)
    best_fitness_history_list.append(best_fitness)
    historical_best = historical_bests[mode]
    if best_fitness > historical_best:
        stagnation_counts[mode] = 0
        historical_bests[mode] = best_fitness
    else:
        stagnation_counts[mode] += 1

    # 更新 checkpoint 数据
    checkpoints[mode]["best_fitness_history"] = best_fitness_history_list
    checkpoints[mode]["stagnation_count"] = stagnation_counts[mode]
    checkpoints[mode]["best_gene_id"] = elites[mode][0].id if elites[mode] else None
    checkpoints[mode]["completed_generations"] = gen

    # 保存 checkpoint 和 CSV（含 CMR）
    _append_csv(mode_dir, gen, best_fitness, avg_fitness, gen_cmr)
    _save_checkpoint(mode_dir, checkpoints[mode])

    return best_fitness, avg_fitness, signals, solidified, gen_cmr


# ---------------------------------------------------------------------------
# 组合模式一代执行
# ---------------------------------------------------------------------------

def _run_combo_mode_generation(
    modes: list[str],
    gen: int,
    config: DaemonConfig,
    stores: dict,
    checkpoints: dict,
    populations: dict,
    elites: dict,
    metrics_histories: dict,
    stagnation_counts: dict,
    historical_bests: dict,
    llm_client: StrategyLLMClient,
    executor_config: ExecutorConfig,
) -> dict[str, tuple[float, float, EvolutionSignals, bool]]:
    """执行组合模式一代的完整进化逻辑。

    每个模式独立维护种群，随机采样组合评估，fitness 归属到参与组合的所有 Gene。

    Returns:
        dict[mode, (best_fitness, avg_fitness, signals, solidified)]
    """
    # 为各模式生成种群
    for mode in modes:
        store = stores[mode]
        if gen == 0 and not elites[mode]:
            populations[mode] = create_seed_population(config.evolution.pop_size, store, control_mode=mode)
        else:
            capsules = store.read_capsules()
            pre_signals = extract_signals(
                current_metrics=metrics_histories[mode][-1] if metrics_histories[mode] else {},
                history=metrics_histories[mode][:-1] if metrics_histories[mode] else [],
                stagnation_count=stagnation_counts[mode],
                stagnation_threshold=config.evolution.stagnation_threshold,
            )
            populations[mode] = generate_next_population(
                current_elite=elites[mode],
                capsules=capsules,
                signals=pre_signals,
                pop_size=config.evolution.pop_size,
                elite_count=config.evolution.elite_count,
                llm_client=llm_client,
                store=store,
                generation=gen,
                metrics=metrics_histories[mode][-1] if metrics_histories[mode] else {},
                control_mode=mode,
                target_event_type=getattr(config.evolution, "target_event_type", ""),
            )

    # 笛卡尔采样：随机生成 pop_size 个组合
    combos = []
    for _ in range(config.evolution.pop_size):
        combo = {mode: random.choice(populations[mode]) for mode in modes}
        combos.append(combo)

    # 评估各组合 — 收集原始结果
    all_combo_raw_results: list[list] = []

    for combo in combos:
        factory = ComboControlFnFactory(combo, executor_config)
        results_list = evaluate_strategy_multi_scenario(
            scenario_paths=config.scenario_dirs,
            control_fn_factory=factory,
            evaluator_config=config.evaluator,
        )
        all_combo_raw_results.append(results_list)

    # 跨组合批量归一化
    combo_scores = batch_normalize_and_score(all_combo_raw_results, config.evaluator)

    # 将 fitness 归属到参与组合的所有 Gene（联合 fitness 归属）
    gene_fitnesses: dict[str, dict[str, list[float]]] = {mode: {} for mode in modes}
    for ci, combo in enumerate(combos):
        gen_fitness = combo_scores[ci]
        for mode in modes:
            gene = combo[mode]
            gene_id = gene.id
            if gene_id not in gene_fitnesses[mode]:
                gene_fitnesses[mode][gene_id] = []
            if gen_fitness > float("-inf"):
                gene_fitnesses[mode][gene_id].append(gen_fitness)

    # 各模式独立处理排名和固化
    results = {}
    for mode in modes:
        store = stores[mode]
        mode_dir = Path(config.store.store_dir) / mode

        # 聚合各 Gene 的平均 fitness
        mode_population = populations[mode]
        mode_scores: list[float] = []
        mode_eval_metrics: dict = {}

        for gene in mode_population:
            gene_id = gene.id
            fitnesses = gene_fitnesses[mode].get(gene_id, [])
            if fitnesses:
                avg_f = sum(fitnesses) / len(fitnesses)
            else:
                avg_f = float("-inf")
            mode_scores.append(avg_f)
            # 记录评估日志
            store.log_evaluated(gene.id, gen, avg_f > float("-inf"), {})

        valid_scores = [s for s in mode_scores if s > float("-inf")]
        if valid_scores:
            best_fitness = max(valid_scores)
            avg_fitness = sum(valid_scores) / len(valid_scores)
        else:
            best_fitness = 0.0
            avg_fitness = 0.0

        # 排名
        ranked = rank_strategies(mode_population, mode_scores, k=config.evolution.pop_size)
        elites[mode] = [g for g, _ in ranked]

        # 当代指标
        current_gen_metrics = {
            "avg_queue": 0.0,
            "avg_throughput": 0.0,
            "avg_delay": 0.0,
            "best_fitness": best_fitness,
        }
        metrics_histories[mode].append(current_gen_metrics)

        # 进化信号
        signals = extract_signals(
            current_metrics=current_gen_metrics,
            history=metrics_histories[mode][:-1],
            stagnation_count=stagnation_counts[mode],
            stagnation_threshold=config.evolution.stagnation_threshold,
        )

        # 固化判断
        solidified = False
        best_fitness_history = checkpoints[mode].get("best_fitness_history", [])
        prev_best = max(best_fitness_history) if best_fitness_history else float("-inf")
        if best_fitness > prev_best:
            if elites[mode]:
                best_gene = elites[mode][0]
                capsule = Capsule(
                    gene_id=best_gene.id,
                    metrics=current_gen_metrics,
                    solidified_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    generation=gen,
                )
                store.append_capsule(capsule)
                store.log_solidified(best_gene.id, gen)
                solidified = True

        # 更新历史和停滞计数
        best_fitness_history_list = list(best_fitness_history)
        best_fitness_history_list.append(best_fitness)
        if best_fitness > historical_bests[mode]:
            stagnation_counts[mode] = 0
            historical_bests[mode] = best_fitness
        else:
            stagnation_counts[mode] += 1

        # 更新 checkpoint 数据
        checkpoints[mode]["best_fitness_history"] = best_fitness_history_list
        checkpoints[mode]["stagnation_count"] = stagnation_counts[mode]
        checkpoints[mode]["best_gene_id"] = elites[mode][0].id if elites[mode] else None
        checkpoints[mode]["completed_generations"] = gen

        # 保存 checkpoint 和 CSV
        _append_csv(mode_dir, gen, best_fitness, avg_fitness)
        _save_checkpoint(mode_dir, checkpoints[mode])

        results[mode] = (best_fitness, avg_fitness, signals, solidified)

    return results


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def main():
    """进化守护进程主入口。

    实现 "生成 -> 测试 -> 进化 -> 固化" 完整循环，支持多控制模式：
    1. 配置加载（TOML + CLI 覆盖）
    2. 单例锁保护（FileLock timeout=0）
    3. 为每个 control_mode 创建独立 store 子目录和 checkpoint
    4. 主循环（单模式或组合模式）：
       a. 第 0 代：create_seed_population（按模式选种子）
       b. 后续代：generate_next_population（含 LLM 生成）
       c. 单模式：ModeControlFnFactory 评估；组合模式：ComboControlFnFactory 评估
       d. 各模式独立排名、固化、checkpoint 保存
    5. SIGINT 优雅退出（当代完成后保存 checkpoint）
    6. 最终 fitness 趋势总结
    """
    global _shutdown_requested
    _shutdown_requested = False

    # 1. 配置加载
    args = parse_args()
    config = load_config(args)

    # 2. 日志配置
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    # 3. 信号处理（只设标志位，优雅退出）
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGHUP, _handle_shutdown)

    # 4. Store 初始化：为每个 control_mode 创建独立子目录
    store_dir = Path(config.store.store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)

    stores: dict[str, AssetStore] = {}
    checkpoints: dict[str, dict] = {}
    for mode in config.control_modes:
        mode_store_dir = store_dir / mode
        mode_store_dir.mkdir(parents=True, exist_ok=True)
        stores[mode] = AssetStore(str(mode_store_dir))
        checkpoints[mode] = _load_checkpoint(mode_store_dir)

    # 5. 单例锁（FileLock timeout=0 立即失败）
    daemon_lock = FileLock(str(store_dir / "daemon.lock"))
    try:
        daemon_lock.acquire(timeout=0)
    except Timeout:
        print("[Daemon] 错误：已有实例在运行（daemon.lock 已被占用）")
        sys.exit(1)

    try:
        # 6. LLM 客户端
        llm_client = StrategyLLMClient(
            base_url=config.llm.base_url,
            api_key=config.llm.api_key,
            timeout_seconds=config.llm.timeout_seconds,
            max_retries=config.llm.max_retries,
            model=config.llm.model,
            temperature=config.llm.temperature,
        )

        # 7. 各模式状态初始化
        populations: dict[str, list[Gene]] = {mode: [] for mode in config.control_modes}
        elites: dict[str, list[Gene]] = {mode: [] for mode in config.control_modes}
        metrics_histories: dict[str, list[dict]] = {mode: [] for mode in config.control_modes}
        stagnation_counts: dict[str, int] = {}
        historical_bests: dict[str, float] = {}

        for mode in config.control_modes:
            cp = checkpoints[mode]
            stagnation_counts[mode] = cp.get("stagnation_count", 0)
            best_fitness_history = cp.get("best_fitness_history", [])
            historical_bests[mode] = max(best_fitness_history) if best_fitness_history else float("-inf")

            # 从 store 恢复精英
            best_gene_id = cp.get("best_gene_id")
            if best_gene_id:
                genes = stores[mode].read_genes()
                for g in genes:
                    if g.id == best_gene_id:
                        elites[mode] = [g]
                        break

        executor_config = ExecutorConfig()

        # 计算起始代（所有模式取最小值）
        start_gen = min(
            cp["completed_generations"] + 1
            for cp in checkpoints.values()
        )

        is_combo = len(config.control_modes) > 1

        print(
            f"[Daemon] 启动进化循环 (generations={config.evolution.generations}, "
            f"pop_size={config.evolution.pop_size}, start_gen={start_gen}, "
            f"modes={config.control_modes})"
        )

        # 8. 主循环
        for gen in range(start_gen, config.evolution.generations):
            # 8a. 优雅退出检查
            if _shutdown_requested:
                for mode in config.control_modes:
                    mode_dir = store_dir / mode
                    _save_checkpoint(mode_dir, checkpoints[mode])
                print("[Daemon] 优雅退出，已保存 checkpoint")
                break

            if is_combo:
                # 8b. 组合模式
                mode_results = _run_combo_mode_generation(
                    modes=config.control_modes,
                    gen=gen,
                    config=config,
                    stores=stores,
                    checkpoints=checkpoints,
                    populations=populations,
                    elites=elites,
                    metrics_histories=metrics_histories,
                    stagnation_counts=stagnation_counts,
                    historical_bests=historical_bests,
                    llm_client=llm_client,
                    executor_config=executor_config,
                )
                # 打印各模式摘要
                for mode, (best_fitness, avg_fitness, signals, solidified) in mode_results.items():
                    unique_count = len({g.id for g in populations[mode]})
                    print(f"[{mode}]", end=" ")
                    _log_gen_summary(gen, best_fitness, avg_fitness, signals, solidified, unique_count, config.evolution.pop_size)
            else:
                # 8c. 单模式
                mode = config.control_modes[0]
                best_fitness, avg_fitness, signals, solidified, gen_cmr = _run_single_mode_generation(
                    mode=mode,
                    gen=gen,
                    config=config,
                    stores=stores,
                    checkpoints=checkpoints,
                    populations=populations,
                    elites=elites,
                    metrics_histories=metrics_histories,
                    stagnation_counts=stagnation_counts,
                    historical_bests=historical_bests,
                    llm_client=llm_client,
                    executor_config=executor_config,
                )
                unique_count = len({g.id for g in populations[mode]})
                _log_gen_summary(gen, best_fitness, avg_fitness, signals, solidified, unique_count, config.evolution.pop_size, gen_cmr)

        # 9. 最终总结
        for mode in config.control_modes:
            best_fitness_history = checkpoints[mode].get("best_fitness_history", [])
            if best_fitness_history:
                first_best = best_fitness_history[0]
                last_best = best_fitness_history[-1]
                if abs(first_best) > 1e-9:
                    improvement = ((last_best - first_best) / abs(first_best)) * 100
                else:
                    improvement = 0.0
                print(f"\n[Daemon] 进化完成 [{mode}]")
                print(f"  初代 best: {first_best:.4f}")
                print(f"  末代 best: {last_best:.4f}")
                print(f"  提升: {improvement:+.1f}%")
            else:
                print(f"\n[Daemon] 无代际数据 [{mode}]")

    finally:
        daemon_lock.release()


if __name__ == "__main__":
    main()
