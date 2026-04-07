"""
evoprog.evaluator.obs_builder: 从 TraCI 实时查询构建 obs dict。

从 TraCI 查询受控交叉口的车道级交通数据，构建 compute_phase_values 所需的 obs dict。
obs dict 格式与 PI-Light 的 lane-link 变量名一致。
"""

from __future__ import annotations

from typing import Any


def build_obs_from_traci(
    traci_module: Any,
    tl_id: str,
    controlled_lanes: list[str],
) -> dict[str, list[float]]:
    """
    从 TraCI 实时查询构建 obs dict，供 compute_phase_values 使用。

    Args:
        traci_module: traci 模块（或 mock）
        tl_id: 信号灯 ID（当前版本未直接使用，由调用方传入 controlled_lanes）
        controlled_lanes: 受控车道 ID 列表（可包含重复）

    Returns:
        obs dict：
            - inlane_2_num_vehicle: list[float]     - 入车道车辆数
            - outlane_2_num_vehicle: list[float]    - 出车道车辆数（同一组 lane 数据）
            - inlane_2_num_waiting_vehicle: list[float] - 等待车辆数
            - inlane_2_vehicle_dist: list[float]    - 入车道平均车间距
            - outlane_2_vehicle_dist: list[float]   - 出车道平均车间距（同一组 lane 数据）
    """
    # 去重并保持顺序（同一 lane 可能在 controlled_lanes 中重复出现）
    unique_lanes = list(dict.fromkeys(controlled_lanes))

    num_vehicle_list: list[float] = []
    num_waiting_list: list[float] = []
    vehicle_dist_list: list[float] = []

    for lane_id in unique_lanes:
        num_vehicle = traci_module.lane.getLastStepVehicleNumber(lane_id)
        num_waiting = traci_module.lane.getLastStepHaltingNumber(lane_id)
        lane_length = traci_module.lane.getLength(lane_id)

        # vehicle_dist：当 lane 上有车辆时，使用 lane_length / num_vehicle 作为平均间距近似值
        # 无车辆时为 lane_length
        if num_vehicle > 0:
            vehicle_dist = lane_length / num_vehicle
        else:
            vehicle_dist = lane_length

        num_vehicle_list.append(float(num_vehicle))
        num_waiting_list.append(float(num_waiting))
        vehicle_dist_list.append(float(vehicle_dist))

    return {
        "inlane_2_num_vehicle": num_vehicle_list,
        "outlane_2_num_vehicle": num_vehicle_list,         # 同一组数据，in/out 共用
        "inlane_2_num_waiting_vehicle": num_waiting_list,
        "outlane_2_num_waiting_vehicle": num_waiting_list,  # 出车道等待车辆（下游背压）
        "inlane_2_vehicle_dist": vehicle_dist_list,
        "outlane_2_vehicle_dist": vehicle_dist_list,       # 同一组数据，in/out 共用
    }


def extract_lane_links(
    traci_module: Any,
    tl_id: str,
) -> tuple[list[list[tuple[int, int]]], list[str]]:
    """
    从 TraCI 信号灯控制链接中提取 lane_links_per_move 结构。

    Args:
        traci_module: traci 模块（或 mock）
        tl_id: 信号灯 ID

    Returns:
        (lane_links_per_move, unique_lanes)：
            - lane_links_per_move: list[list[tuple[int, int]]]
              - 外层列表按 move 索引，内层每个元素为 (inLane_idx, outLane_idx)
            - unique_lanes: list[str] — 去重后的 lane 列表（保持顺序）
    """
    # 获取受控车道（包含重复）和链接
    controlled_lanes = list(traci_module.trafficlight.getControlledLanes(tl_id))
    controlled_links = traci_module.trafficlight.getControlledLinks(tl_id)

    # 构建唯一 lane 列表（去重保序）
    unique_lanes = list(dict.fromkeys(controlled_lanes))

    lane_links_per_move: list[list[tuple[int, int]]] = []

    for move_links in controlled_links:
        move_lane_links: list[tuple[int, int]] = []
        for link in move_links:
            in_lane, out_lane, _via = link[0], link[1], link[2]
            # 将 lane 名称转换为唯一 lane 列表中的索引
            if in_lane not in unique_lanes:
                unique_lanes.append(in_lane)
            if out_lane not in unique_lanes:
                unique_lanes.append(out_lane)
            in_idx = unique_lanes.index(in_lane)
            out_idx = unique_lanes.index(out_lane)
            move_lane_links.append((in_idx, out_idx))
        lane_links_per_move.append(move_lane_links)

    return lane_links_per_move, unique_lanes


def extract_phase_move_map(
    traci_module: Any,
    tl_id: str,
) -> list[list[int]]:
    """
    从 TraCI 信号方案中提取 phase 到 move 的映射。

    phase_move_map[phase_idx] = [move_idx, ...] — 每个相位包含的绿灯 move 索引

    只包含包含至少一个绿灯（'G' 或 'g'）的相位；
    跳过全红/全黄过渡相位（state 全为 'r'/'y' 的相位）。

    Args:
        traci_module: traci 模块（或 mock）
        tl_id: 信号灯 ID

    Returns:
        phase_move_map: list[list[int]]
    """
    program_logics = traci_module.trafficlight.getAllProgramLogics(tl_id)
    if not program_logics:
        return []

    # 取第一个（当前激活的）信号方案
    logic = program_logics[0]
    phases = logic.phases

    phase_move_map: list[list[int]] = []

    for phase in phases:
        state = phase.state
        # 跳过全红/全黄过渡相位
        if not any(c in ('G', 'g') for c in state):
            continue

        # 找到所有绿灯 move 索引
        move_indices = [
            i for i, c in enumerate(state) if c in ('G', 'g')
        ]
        phase_move_map.append(move_indices)

    return phase_move_map
