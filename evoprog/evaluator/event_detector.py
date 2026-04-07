"""
evoprog.evaluator.event_detector: 从 TraCI 实时检测交通事件。

事件类型：
  - EMERGENCY: 紧急车辆（救护车、消防车）接近信号灯
  - TRANSIT: 公交车在上游车道
  - INCIDENT: 车辆在车道中长时间停滞（疑似事故）
  - CONGESTION: 严重拥堵（队列超过历史 P90）

每个事件携带上下文信息（距离、相位、数量等），供事件特化技能使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class EventContext:
    """单个信号灯的事件上下文，供事件特化技能代码使用。

    这些值会作为变量注入到策略代码的执行环境中。
    """
    # Emergency
    emergency_detected: bool = False
    emergency_distance: float = 999.0   # 最近紧急车辆距离 (m)
    emergency_phase: int = -1           # 紧急车辆需要的相位编号
    emergency_count: int = 0            # 检测范围内紧急车辆数量

    # Transit (Bus)
    transit_detected: bool = False
    bus_count: int = 0                  # 上游公交车数量
    bus_min_distance: float = 999.0     # 最近公交车距离 (m)
    bus_phase: int = -1                 # 公交车需要的相位编号

    # Incident
    incident_detected: bool = False
    incident_blocked_lanes: int = 0     # 被事故阻塞的车道数

    # Congestion
    congestion_detected: bool = False
    congestion_level: int = 0           # 拥堵等级 (0=正常, 1=轻度, 2=中度, 3=严重)


# Priority ordering (lower number = higher priority)
EVENT_PRIORITY = {
    "emergency": 0,
    "incident": 1,
    "transit": 2,
    "congestion": 3,
    "normal": 4,
}

# Emergency vehicle classes in SUMO
_EMERGENCY_VCLASSES = frozenset({"emergency", "authority"})
# Transit vehicle classes in SUMO
_TRANSIT_VCLASSES = frozenset({"bus", "tram", "rail_urban"})

# Detection parameters
EMERGENCY_DETECT_RANGE = 200.0  # meters
TRANSIT_DETECT_RANGE = 100.0    # meters
INCIDENT_STOP_THRESHOLD = 60.0  # seconds stopped to count as incident
CONGESTION_QUEUE_THRESHOLDS = (15, 30, 60)  # queue lengths for levels 1, 2, 3


def detect_events_for_tl(
    traci_module: Any,
    tl_id: str,
    controlled_lanes: list[str],
    phase_move_map: list[list[int]],
    lane_links_per_move: list[list[tuple[int, int]]],
    unique_lanes: list[str],
) -> EventContext:
    """检测指定信号灯附近的所有交通事件。

    Args:
        traci_module: traci 模块
        tl_id: 信号灯 ID
        controlled_lanes: 受控车道列表
        phase_move_map: 相位到 move 的映射
        lane_links_per_move: lane-link 结构
        unique_lanes: 去重后的车道列表

    Returns:
        EventContext: 包含所有检测到的事件及其上下文
    """
    ctx = EventContext()

    # 获取所有受控车道上的车辆
    all_vehicles: dict[str, dict] = {}  # vid -> {lane, vclass, speed, waiting, distance}

    for lane_id in controlled_lanes:
        lane_length = traci_module.lane.getLength(lane_id)
        for vid in traci_module.lane.getLastStepVehicleIDs(lane_id):
            try:
                vclass = traci_module.vehicle.getVehicleClass(vid)
                speed = traci_module.vehicle.getSpeed(vid)
                waiting = traci_module.vehicle.getWaitingTime(vid)
                # 距离信号灯的近似距离：lane_length - 车辆在车道上的位置
                lane_pos = traci_module.vehicle.getLanePosition(vid)
                dist_to_tl = lane_length - lane_pos

                all_vehicles[vid] = {
                    "lane": lane_id,
                    "vclass": vclass,
                    "speed": speed,
                    "waiting": waiting,
                    "dist_to_tl": max(0.0, dist_to_tl),
                }
            except Exception:
                continue  # 车辆可能在当步消失

    # --- Emergency Detection ---
    emergency_vehicles = []
    for vid, info in all_vehicles.items():
        if info["vclass"] in _EMERGENCY_VCLASSES and info["dist_to_tl"] < EMERGENCY_DETECT_RANGE:
            emergency_vehicles.append((vid, info))

    if emergency_vehicles:
        ctx.emergency_detected = True
        ctx.emergency_count = len(emergency_vehicles)
        # 找最近的紧急车辆
        closest = min(emergency_vehicles, key=lambda x: x[1]["dist_to_tl"])
        ctx.emergency_distance = closest[1]["dist_to_tl"]
        # 确定紧急车辆所在的相位
        ctx.emergency_phase = _find_phase_for_lane(
            closest[1]["lane"], unique_lanes, lane_links_per_move, phase_move_map
        )

    # --- Transit Detection ---
    transit_vehicles = []
    for vid, info in all_vehicles.items():
        if info["vclass"] in _TRANSIT_VCLASSES and info["dist_to_tl"] < TRANSIT_DETECT_RANGE:
            transit_vehicles.append((vid, info))

    if transit_vehicles:
        ctx.transit_detected = True
        ctx.bus_count = len(transit_vehicles)
        closest_bus = min(transit_vehicles, key=lambda x: x[1]["dist_to_tl"])
        ctx.bus_min_distance = closest_bus[1]["dist_to_tl"]
        ctx.bus_phase = _find_phase_for_lane(
            closest_bus[1]["lane"], unique_lanes, lane_links_per_move, phase_move_map
        )

    # --- Incident Detection ---
    # 车辆在非信号灯位置长时间停滞 -> 疑似事故
    incident_lanes = set()
    for vid, info in all_vehicles.items():
        if (info["waiting"] > INCIDENT_STOP_THRESHOLD
                and info["speed"] < 0.1
                and info["dist_to_tl"] > 10.0  # 排除信号灯前正常等待
                and info["vclass"] not in _EMERGENCY_VCLASSES):
            incident_lanes.add(info["lane"])

    if incident_lanes:
        ctx.incident_detected = True
        ctx.incident_blocked_lanes = len(incident_lanes)

    # --- Congestion Detection ---
    total_halting = 0
    for lane_id in controlled_lanes:
        total_halting += traci_module.lane.getLastStepHaltingNumber(lane_id)

    if total_halting >= CONGESTION_QUEUE_THRESHOLDS[2]:
        ctx.congestion_detected = True
        ctx.congestion_level = 3
    elif total_halting >= CONGESTION_QUEUE_THRESHOLDS[1]:
        ctx.congestion_detected = True
        ctx.congestion_level = 2
    elif total_halting >= CONGESTION_QUEUE_THRESHOLDS[0]:
        ctx.congestion_detected = True
        ctx.congestion_level = 1

    return ctx


def get_active_event_type(ctx: EventContext) -> str:
    """根据优先级返回当前最高优先级的事件类型。

    Priority: emergency > incident > transit > congestion > normal

    Returns:
        事件类型字符串: "emergency" | "incident" | "transit" | "congestion" | "normal"
    """
    if ctx.emergency_detected:
        return "emergency"
    if ctx.incident_detected:
        return "incident"
    if ctx.transit_detected:
        return "transit"
    if ctx.congestion_detected:
        return "congestion"
    return "normal"


def event_context_to_vars(ctx: EventContext) -> dict[str, float]:
    """将 EventContext 转换为策略代码可用的变量字典。

    这些变量会被注入到 exec() 的 local_vars 中，供事件特化技能代码使用。

    Returns:
        dict: 变量名 -> 值
    """
    return {
        "event_emergency_distance": ctx.emergency_distance,
        "event_emergency_phase": float(ctx.emergency_phase),
        "event_emergency_count": float(ctx.emergency_count),
        "event_bus_count": float(ctx.bus_count),
        "event_bus_distance": ctx.bus_min_distance,
        "event_bus_phase": float(ctx.bus_phase),
        "event_incident_blocked": float(ctx.incident_blocked_lanes),
        "event_congestion_level": float(ctx.congestion_level),
    }


def _find_phase_for_lane(
    lane_id: str,
    unique_lanes: list[str],
    lane_links_per_move: list[list[tuple[int, int]]],
    phase_move_map: list[list[int]],
) -> int:
    """找到包含指定车道的相位编号。

    通过 lane_id -> lane_idx -> 包含该 lane_idx 的 move -> 包含该 move 的 phase。

    Returns:
        相位编号（从 0 开始），找不到时返回 -1
    """
    if lane_id not in unique_lanes:
        return -1

    lane_idx = unique_lanes.index(lane_id)

    # 找到包含该 lane 的所有 move
    target_moves = set()
    for move_id, links in enumerate(lane_links_per_move):
        for in_idx, out_idx in links:
            if in_idx == lane_idx or out_idx == lane_idx:
                target_moves.add(move_id)

    # 找到包含这些 move 的相位
    for phase_id, move_ids in enumerate(phase_move_map):
        if target_moves & set(move_ids):
            return phase_id

    return -1
