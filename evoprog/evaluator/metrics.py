"""
evoprog.evaluator.metrics: 边级订阅注册 + 每步指标批量采集 + 仿真结束后均值聚合。

使用边级订阅 API 批量采集延误、排队、吞吐三项指标，非逐车查询。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# TraCI 变量 ID 常量（来自 traci.constants）
# 在运行时从 traci 导入，但在 metrics.py 中直接引用以支持测试时 mock
_VAR_CURRENT_TRAVELTIME = 90          # tc.VAR_CURRENT_TRAVELTIME
_LAST_STEP_VEHICLE_HALTING_NUMBER = 20  # tc.LAST_STEP_VEHICLE_HALTING_NUMBER
_LAST_STEP_VEHICLE_NUMBER = 16         # tc.LAST_STEP_VEHICLE_NUMBER


@dataclass
class StepMetrics:
    """单步仿真指标：三项边级统计聚合值。"""
    delay_sum: float = 0.0
    halting_sum: int = 0
    vehicle_sum: int = 0


def subscribe_all_edges(traci_module: Any, edge_ids: list[str]) -> None:
    """
    对所有边注册三项指标订阅。

    Args:
        traci_module: traci 模块（或 mock）
        edge_ids: 需要订阅的边 ID 列表
    """
    var_ids = [
        _VAR_CURRENT_TRAVELTIME,
        _LAST_STEP_VEHICLE_HALTING_NUMBER,
        _LAST_STEP_VEHICLE_NUMBER,
    ]
    for edge_id in edge_ids:
        traci_module.edge.subscribe(edge_id, var_ids)


def collect_step_metrics(traci_module: Any) -> StepMetrics:
    """
    从边级订阅结果中批量采集单步指标。

    Args:
        traci_module: traci 模块（或 mock）

    Returns:
        StepMetrics: 当前步所有边的指标聚合值
    """
    results = traci_module.edge.getAllSubscriptionResults()

    delay_sum = 0.0
    halting_sum = 0
    vehicle_sum = 0

    for edge_data in results.values():
        delay_sum += edge_data.get(_VAR_CURRENT_TRAVELTIME, 0.0)
        halting_sum += edge_data.get(_LAST_STEP_VEHICLE_HALTING_NUMBER, 0)
        vehicle_sum += edge_data.get(_LAST_STEP_VEHICLE_NUMBER, 0)

    return StepMetrics(
        delay_sum=delay_sum,
        halting_sum=halting_sum,
        vehicle_sum=vehicle_sum,
    )


def aggregate_metrics(steps: list[StepMetrics]) -> dict:
    """
    对多步 StepMetrics 列表计算均值。

    Args:
        steps: 每步指标列表

    Returns:
        dict with keys: avg_delay, avg_queue, avg_throughput
    """
    if not steps:
        return {"avg_delay": 0.0, "avg_queue": 0.0, "avg_throughput": 0.0}

    n = len(steps)
    avg_delay = sum(s.delay_sum for s in steps) / n
    avg_queue = sum(s.halting_sum for s in steps) / n
    avg_throughput = sum(s.vehicle_sum for s in steps) / n

    return {
        "avg_delay": avg_delay,
        "avg_queue": avg_queue,
        "avg_throughput": avg_throughput,
    }


def get_lane_vehicle_ids(traci_module: Any, lane_ids: list[str]) -> set[str]:
    """
    获取指定受控车道上所有车辆 ID 的集合。

    参考 TSC_CYCLE/src/grpo/rewards.py 仿真前后车辆集合计算方式。

    Args:
        traci_module: traci 模块（或 mock）
        lane_ids: 受控车道 ID 列表

    Returns:
        所有车辆 ID 的 set（去重合并）
    """
    vehicle_ids: set[str] = set()
    for lane_id in lane_ids:
        vehicle_ids.update(traci_module.lane.getLastStepVehicleIDs(lane_id))
    return vehicle_ids


def collect_vehicle_waiting_time(traci_module: Any, lane_ids: list[str]) -> float:
    """
    累积受控车道上所有车辆的等待时间。

    参考 TSC_CYCLE/src/grpo/rewards.py 中每步 total_delay 累积方式。

    Args:
        traci_module: traci 模块（或 mock）
        lane_ids: 受控车道 ID 列表

    Returns:
        所有车辆等待时间的累积和（float）
    """
    total_waiting: float = 0.0
    for lane_id in lane_ids:
        for vid in traci_module.lane.getLastStepVehicleIDs(lane_id):
            total_waiting += traci_module.vehicle.getWaitingTime(vid)
    return total_waiting
