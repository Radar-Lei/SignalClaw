"""
evoprog.evaluator.event_metrics: 事件特化指标采集。

在仿真过程中跟踪特殊车辆（紧急车辆、公交车）的延误和通行时间，
用于计算事件特化 fitness。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EventMetrics:
    """事件特化评估指标。"""
    # Emergency vehicle metrics
    emergency_vehicle_count: int = 0
    emergency_total_delay: float = 0.0
    emergency_total_travel_time: float = 0.0
    emergency_avg_delay: float = 0.0

    # Bus/transit metrics
    bus_count: int = 0
    bus_total_delay: float = 0.0
    bus_total_travel_time: float = 0.0
    bus_avg_delay: float = 0.0

    # Normal vehicle metrics (for comparison)
    normal_vehicle_count: int = 0
    normal_total_delay: float = 0.0
    normal_avg_delay: float = 0.0


# SUMO vehicle classes
_EMERGENCY_VCLASSES = frozenset({"emergency", "authority"})
_TRANSIT_VCLASSES = frozenset({"bus", "tram", "rail_urban"})


class EventMetricsCollector:
    """在仿真循环中持续采集事件特化指标。

    用法:
        collector = EventMetricsCollector()
        # 在每个仿真步调用:
        collector.collect_step(traci_module, controlled_lanes)
        # 仿真结束后:
        metrics = collector.finalize()
    """

    def __init__(self):
        self._emergency_delays: dict[str, float] = {}  # vid -> cumulative delay
        self._bus_delays: dict[str, float] = {}
        self._normal_delays: dict[str, float] = {}
        self._seen_emergency: set[str] = set()
        self._seen_bus: set[str] = set()

    def collect_step(self, traci_module: Any, controlled_lanes: list[str]) -> None:
        """采集当前仿真步的事件特化指标。

        使用 getAccumulatedWaitingTime 追踪每车最大累积等待时间，
        这比每步累加 getWaitingTime 更公平（后者对长连续等待有二次惩罚）。
        """
        for lane_id in controlled_lanes:
            for vid in traci_module.lane.getLastStepVehicleIDs(lane_id):
                try:
                    vclass = traci_module.vehicle.getVehicleClass(vid)
                    acc_wait = traci_module.vehicle.getAccumulatedWaitingTime(vid)

                    if vclass in _EMERGENCY_VCLASSES:
                        self._seen_emergency.add(vid)
                        # 记录最大累积等待（因为车辆可能被多次观测）
                        if acc_wait > self._emergency_delays.get(vid, 0.0):
                            self._emergency_delays[vid] = acc_wait
                    elif vclass in _TRANSIT_VCLASSES:
                        self._seen_bus.add(vid)
                        if acc_wait > self._bus_delays.get(vid, 0.0):
                            self._bus_delays[vid] = acc_wait
                    else:
                        if acc_wait > self._normal_delays.get(vid, 0.0):
                            self._normal_delays[vid] = acc_wait
                except Exception:
                    continue

    def finalize(self) -> EventMetrics:
        """计算最终事件特化指标。"""
        metrics = EventMetrics()

        # Emergency
        metrics.emergency_vehicle_count = len(self._seen_emergency)
        metrics.emergency_total_delay = sum(self._emergency_delays.values())
        if metrics.emergency_vehicle_count > 0:
            metrics.emergency_avg_delay = (
                metrics.emergency_total_delay / metrics.emergency_vehicle_count
            )

        # Bus
        metrics.bus_count = len(self._seen_bus)
        metrics.bus_total_delay = sum(self._bus_delays.values())
        if metrics.bus_count > 0:
            metrics.bus_avg_delay = metrics.bus_total_delay / metrics.bus_count

        # Normal
        metrics.normal_vehicle_count = len(self._normal_delays)
        metrics.normal_total_delay = sum(self._normal_delays.values())
        if metrics.normal_vehicle_count > 0:
            metrics.normal_avg_delay = (
                metrics.normal_total_delay / metrics.normal_vehicle_count
            )

        return metrics


def compute_event_fitness(
    event_metrics: EventMetrics,
    scenario_type: str = "normal",
) -> float:
    """计算事件特化 fitness。

    不同场景类型使用不同权重:
    - emergency: 重点惩罚紧急车辆延误
    - transit: 重点惩罚公交延误，兼顾普通交通
    - incident: 重点惩罚受影响区域延误
    - normal: 标准 fitness（延误 + 排队 + 吞吐量）

    Args:
        event_metrics: 事件特化指标
        scenario_type: 场景类型

    Returns:
        fitness 值（越大越好，通常为负数）
    """
    emg_delay = event_metrics.emergency_avg_delay
    bus_delay = event_metrics.bus_avg_delay
    normal_delay = event_metrics.normal_avg_delay

    if scenario_type == "emergency":
        # 紧急车辆延误权重最高
        fitness = -(0.6 * emg_delay + 0.25 * normal_delay + 0.15 * bus_delay)
    elif scenario_type == "transit":
        # 公交延误权重最高
        fitness = -(0.5 * bus_delay + 0.35 * normal_delay + 0.15 * emg_delay)
    elif scenario_type == "incident":
        # 正常交通延误（受事故影响）权重最高
        fitness = -(0.6 * normal_delay + 0.25 * emg_delay + 0.15 * bus_delay)
    elif scenario_type == "mixed":
        # 均衡权重
        fitness = -(0.35 * emg_delay + 0.35 * bus_delay + 0.30 * normal_delay)
    else:
        # normal: 只看普通交通
        fitness = -normal_delay

    return fitness
