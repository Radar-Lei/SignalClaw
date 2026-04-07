"""进化信号提取：百分位动态阈值 + 停滞检测 + 信号到自然语言转换。"""

import statistics
from dataclasses import dataclass


@dataclass
class EvolutionSignals:
    """结构化进化信号，用于引导 LLM 生成方向。

    Attributes:
        high_queue_detected: 当代排队长度超过历史 P75
        low_throughput: 当代吞吐量低于历史 P25
        high_delay: 当代延误超过历史 P75
        performance_gain: 当代 best_fitness 优于前代
        performance_decline: 当代 best_fitness 低于前代
        force_innovation: 连续停滞代数达到阈值，强制创新
    """

    high_queue_detected: bool = False
    low_throughput: bool = False
    high_delay: bool = False
    performance_gain: bool = False
    performance_decline: bool = False
    force_innovation: bool = False


def extract_signals(
    current_metrics: dict,
    history: list[dict],
    stagnation_count: int,
    stagnation_threshold: int,
) -> EvolutionSignals:
    """从当代指标和历史数据中提取结构化进化信号。

    Args:
        current_metrics: 当代指标字典，包含 avg_queue/avg_throughput/avg_delay/best_fitness
        history: 历史代指标列表（同结构 dict），按时间升序排列
        stagnation_count: 当前连续停滞代数
        stagnation_threshold: 触发 force_innovation 的停滞阈值

    Returns:
        EvolutionSignals：包含各信号状态的结构化数据

    Notes:
        百分位判断仅在 len(history) >= 4 时执行，避免 statistics.quantiles 数据不足错误。
        statistics.quantiles(data, n=4) 返回 [P25, P50, P75]（索引 0/1/2）。
    """
    signals = EvolutionSignals()

    # 1. force_innovation：连续停滞达到阈值
    signals.force_innovation = stagnation_count >= stagnation_threshold

    # 2. 百分位信号（需要 >= 4 条历史记录）
    if len(history) >= 4:
        queues = [h["avg_queue"] for h in history]
        throughputs = [h["avg_throughput"] for h in history]
        delays = [h["avg_delay"] for h in history]

        # statistics.quantiles(data, n=4) 返回 [P25, P50, P75]
        q_queues = statistics.quantiles(queues, n=4)
        q_throughputs = statistics.quantiles(throughputs, n=4)
        q_delays = statistics.quantiles(delays, n=4)

        p75_queue = q_queues[2]
        p25_throughput = q_throughputs[0]
        p75_delay = q_delays[2]

        signals.high_queue_detected = current_metrics["avg_queue"] > p75_queue
        signals.low_throughput = current_metrics["avg_throughput"] < p25_throughput
        signals.high_delay = current_metrics["avg_delay"] > p75_delay

    # 3. 性能对比（与前代比较）
    if history:
        prev_fitness = history[-1]["best_fitness"]
        curr_fitness = current_metrics["best_fitness"]
        if curr_fitness > prev_fitness:
            signals.performance_gain = True
        elif curr_fitness < prev_fitness:
            signals.performance_decline = True

    return signals


# 信号到中文自然语言文本的映射
_SIGNAL_MESSAGES: list[tuple[str, str]] = [
    ("force_innovation", "连续多代停滞，必须尝试完全不同的策略结构！不要只调系数，请使用 if 条件分支、多变量乘积、max/min 非线性变换、或者完全不同的变量组合"),
    ("high_queue_detected", "排队长度超过历史 P75，请重点优化排队管理"),
    ("low_throughput", "吞吐量低于历史 P25，请优化通行效率"),
    ("high_delay", "延误超过历史 P75，请减少车辆等待时间"),
    ("performance_gain", "性能有所提升，继续优化当前方向"),
    ("performance_decline", "性能下降，请尝试不同的策略思路"),
]

_DEFAULT_DIRECTION = "持续优化当前策略性能"


def signals_to_direction(signals: EvolutionSignals) -> str:
    """将结构化信号转换为自然语言 direction 字符串，注入 LLM prompt。

    多个信号用中文分号「；」连接。无信号时返回默认 direction。

    Args:
        signals: EvolutionSignals 对象

    Returns:
        中文自然语言进化方向字符串
    """
    active_messages = [
        msg
        for field_name, msg in _SIGNAL_MESSAGES
        if getattr(signals, field_name, False)
    ]

    if not active_messages:
        return _DEFAULT_DIRECTION

    return "；".join(active_messages)
