"""进化诊断指标：CMR、血统集中度、行为多样性。

用于 Search-Trace Self-Improvement 实验的进化过程诊断。
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Optional


def compute_cmr(
    candidate_fitnesses: list[float],
    parent_fitnesses: list[Optional[float]],
) -> float:
    """计算构造性变异率 (Constructive Mutation Rate)。

    CMR = (严格优于父代的子代数) / (有父代的子代总数)

    Args:
        candidate_fitnesses: 本代所有候选的 fitness 列表
        parent_fitnesses: 对应的父代 fitness 列表（种子代为 None）

    Returns:
        CMR 值 [0.0, 1.0]，无有效父代时返回 0.0
    """
    improved = 0
    total_with_parent = 0

    for child_f, parent_f in zip(candidate_fitnesses, parent_fitnesses):
        if parent_f is not None:
            total_with_parent += 1
            if child_f > parent_f:
                improved += 1

    if total_with_parent == 0:
        return 0.0
    return improved / total_with_parent


def compute_ancestry_concentration(
    parent_ids: list[Optional[str]],
    grandparent_map: dict[str, Optional[str]],
) -> float:
    """计算血统集中度：共享同一祖父的种群比例。

    ancestry_concentration = max(count(grandparent_id)) / total_with_grandparent

    Args:
        parent_ids: 本代所有候选的 parent_id 列表
        grandparent_map: gene_id -> parent_id 映射（用于查找祖父）

    Returns:
        血统集中度 [0.0, 1.0]，无有效祖父时返回 0.0
    """
    grandparent_ids: list[str] = []

    for pid in parent_ids:
        if pid is not None:
            gp = grandparent_map.get(pid)
            if gp is not None:
                grandparent_ids.append(gp)

    if not grandparent_ids:
        return 0.0

    counter = Counter(grandparent_ids)
    max_count = counter.most_common(1)[0][1]
    return max_count / len(grandparent_ids)


def compute_fitness_diversity(fitnesses: list[float]) -> float:
    """计算 fitness 多样性（标准差 / |均值|）。

    作为行为多样性的代理指标。完整的行为多样性（JSD of phase-switch distributions）
    需要在 SUMO 评估过程中记录相位决策序列，将在后续版本实现。

    Args:
        fitnesses: 本代所有候选的 fitness 列表

    Returns:
        变异系数 (CV)，均值为 0 时返回 0.0
    """
    if len(fitnesses) < 2:
        return 0.0

    mean = sum(fitnesses) / len(fitnesses)
    if abs(mean) < 1e-12:
        return 0.0

    variance = sum((f - mean) ** 2 for f in fitnesses) / len(fitnesses)
    std = math.sqrt(variance)
    return std / abs(mean)
