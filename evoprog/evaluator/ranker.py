"""
evoprog.evaluator.ranker: 策略排名模块

提供：
- compute_absolute_fitness: 单结果绝对 fitness（用于 scheduler 的单策略多场景评估）
- normalize_and_score: 多策略相对归一化 fitness（用于跨策略对比）
- batch_normalize_and_score: 跨策略批量归一化（daemon 主循环使用）
- rank_strategies: 确定性 top-k 排名
- generalization_score: 泛化性聚合（取最差场景，强调鲁棒性）
"""
from __future__ import annotations

from typing import Optional

from evoprog.config import EvaluatorConfig
from evoprog.evaluator.runner import EvaluationResult


def compute_absolute_fitness(
    result: EvaluationResult,
    config: EvaluatorConfig,
) -> float:
    """
    对单个评估结果计算绝对 fitness 分数（不依赖跨策略归一化）。

    直接线性加权：delay/queue 越低 → fitness 越高，throughput 越高 → fitness 越高。
    值域不限于 [0, 1]，可跨代比较（相同场景下绝对值有意义）。

    Args:
        result: 单个评估结果
        config: EvaluatorConfig（含权重参数）

    Returns:
        绝对 fitness 分数（越高越好）
    """
    return (
        -config.weight_delay * result.avg_delay
        - config.weight_queue * result.avg_queue
        + config.weight_throughput * result.avg_throughput
    )


def normalize_and_score(
    results: list[EvaluationResult],
    config: EvaluatorConfig,
) -> list[float]:
    """
    对多策略评估结果进行指标归一化并计算加权 fitness。

    归一化规则：
    - delay/queue：反转归一化 1 - (x - min) / (max - min)（越低越好）
    - throughput：正常归一化 (x - min) / (max - min)（越高越好）
    - 零除保护：max == min 时得分为 0.5

    Args:
        results: 评估结果列表
        config: EvaluatorConfig（含权重参数）

    Returns:
        fitness 列表（与 results 一一对应）
    """
    if not results:
        return []

    delays = [r.avg_delay for r in results]
    queues = [r.avg_queue for r in results]
    throughputs = [r.avg_throughput for r in results]

    def _normalize_inverted(values: list[float]) -> list[float]:
        """反转归一化（越低越好）。"""
        mn, mx = min(values), max(values)
        if mx == mn:
            return [0.5] * len(values)
        return [1.0 - (v - mn) / (mx - mn) for v in values]

    def _normalize_normal(values: list[float]) -> list[float]:
        """正常归一化（越高越好）。"""
        mn, mx = min(values), max(values)
        if mx == mn:
            return [0.5] * len(values)
        return [(v - mn) / (mx - mn) for v in values]

    norm_delays = _normalize_inverted(delays)
    norm_queues = _normalize_inverted(queues)
    norm_throughputs = _normalize_normal(throughputs)

    w_d = config.weight_delay
    w_q = config.weight_queue
    w_t = config.weight_throughput

    fitness_scores = [
        w_d * nd + w_q * nq + w_t * nt
        for nd, nq, nt in zip(norm_delays, norm_queues, norm_throughputs)
    ]

    return fitness_scores


def batch_normalize_and_score(
    all_results: list[list[Optional[EvaluationResult]]],
    config: EvaluatorConfig,
) -> list[float]:
    """
    跨策略批量归一化：按场景维度归一化后，取每个策略的泛化性分数。

    对每个场景，收集所有策略的结果，调用 normalize_and_score 跨策略归一化。
    然后对每个策略，调用 generalization_score 取最差场景 fitness。

    Args:
        all_results: all_results[strategy_idx][scenario_idx] = EvaluationResult or None
        config: EvaluatorConfig（含权重参数）

    Returns:
        每个策略的 fitness 列表（与 all_results 一一对应）
    """
    num_strategies = len(all_results)
    if num_strategies == 0:
        return []

    num_scenarios = max(len(r) for r in all_results) if all_results else 0

    # strategy_scenario_fitness[i][j] = strategy i 在 scenario j 的归一化 fitness（或 None）
    strategy_scenario_fitness: list[list[Optional[float]]] = [
        [None] * num_scenarios for _ in range(num_strategies)
    ]

    for s in range(num_scenarios):
        # 收集本场景中所有成功的策略结果
        scenario_results: list[EvaluationResult] = []
        strategy_indices: list[int] = []
        for i in range(num_strategies):
            if s < len(all_results[i]) and all_results[i][s] is not None:
                scenario_results.append(all_results[i][s])
                strategy_indices.append(i)

        if not scenario_results:
            continue

        # 跨策略归一化（同一场景内比较所有策略）
        scores = normalize_and_score(scenario_results, config)
        for idx, score in zip(strategy_indices, scores):
            strategy_scenario_fitness[idx][s] = score

    # 每个策略取跨场景的泛化性分数
    return [generalization_score(sf) for sf in strategy_scenario_fitness]


def rank_strategies(
    strategies: list,
    scores: list[float],
    k: int,
) -> list[tuple]:
    """
    按 fitness 降序对策略进行确定性排名，返回 top-k。

    使用 Python 内置 sorted()（稳定排序），确保：
    1. 相同输入多次调用产生相同排名（确定性）
    2. 同分时保持原始顺序（稳定性）

    Args:
        strategies: 策略列表
        scores: 对应 fitness 分数列表
        k: 返回的 top-k 数量

    Returns:
        top-k (strategy, score) 元组列表，按 fitness 降序
    """
    paired = list(zip(strategies, scores))
    ranked = sorted(paired, key=lambda x: x[1], reverse=True)
    return ranked[:k]


def generalization_score(
    scenario_fitnesses: list[Optional[float]],
) -> float:
    """
    聚合多场景 fitness，强调鲁棒性（取最差场景）。

    规则：
    - 过滤 None（失败场景）
    - 有效场景少于总数的一半 → 返回 -inf（多数失败）
    - 否则 → 返回最差（最低）有效 fitness

    Args:
        scenario_fitnesses: 各场景 fitness 列表（失败场景为 None）

    Returns:
        泛化性得分（最差场景 fitness），多数失败时为 -inf
    """
    total = len(scenario_fitnesses)
    valid = [f for f in scenario_fitnesses if f is not None]
    valid_count = len(valid)

    # 多数失败：有效场景少于总数的一半
    if valid_count < total / 2:
        return float("-inf")

    # 有效场景为空（极端情况，理论上被上面覆盖）
    if not valid:
        return float("-inf")

    return min(valid)
