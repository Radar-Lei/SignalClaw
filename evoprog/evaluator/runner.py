"""
evoprog.evaluator.runner: SumoEvaluator Context Manager + 仿真循环 + 控制回调注入。

- SumoEvaluator 作为 Context Manager 启动 SUMO 无头模式，__exit__ 无论异常都关闭 traci 连接并 kill SUMO 进程
- 仿真循环使用 .sumocfg 配置的默认结束时间，不覆盖
- 评估器接受可插拔的 control_fn(traci_module) -> None 回调函数
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional, Any

from evoprog.config import EvaluatorConfig
from evoprog.evaluator.metrics import (
    subscribe_all_edges,
    collect_step_metrics,
    aggregate_metrics,
    get_lane_vehicle_ids,
    collect_vehicle_waiting_time,
)
from evoprog.executor.runner import compute_phase_values

# 延迟导入 traci（在 __enter__ 中通过 sys.path 加载）
# 模块级引用用于测试时 patch
try:
    _sumo_tools = os.environ.get("SUMO_HOME", "/usr/share/sumo")
    _sumo_tools_path = os.path.join(_sumo_tools, "tools")
    if _sumo_tools_path not in sys.path:
        sys.path.insert(0, _sumo_tools_path)
    import traci  # type: ignore
except ImportError:
    traci = None  # type: ignore


@dataclass
class EvaluationResult:
    """SUMO 评估结果：包含三项指标、适应度和执行状态。"""
    success: bool
    avg_delay: float = 0.0
    avg_delay_person: float = 0.0  # person-weighted (bus×30, car×1.5)
    avg_queue: float = 0.0
    avg_throughput: float = 0.0
    fitness: float = 0.0
    total_steps: int = 0
    error: Optional[str] = None
    passed_vehicles: int = 0
    total_delay: float = 0.0


class SumoEvaluator:
    """
    SUMO 评估器 Context Manager。

    用法：
        with SumoEvaluator(sumocfg_path, port, label, config) as evaluator:
            result = evaluator.run(control_fn)
    """

    def __init__(
        self,
        sumocfg_path: str,
        port: int,
        label: str,
        evaluator_config: EvaluatorConfig,
        seed: Optional[int] = None,
    ) -> None:
        self.sumocfg_path = sumocfg_path
        self.port = port
        self.label = label
        self.config = evaluator_config
        self.seed = seed
        self._sumo_proc = None
        self._edge_ids: list[str] = []
        self._tl_ids: list[str] = []
        self._controlled_lanes: list[str] = []
        self._vehicles_before: set[str] = set()
        self._traci = None  # 允许测试注入 mock

    def __enter__(self) -> "SumoEvaluator":
        """启动 SUMO 无头模式，注册边级订阅，记录仿真前车辆集合。"""
        # 确保 traci 可用（支持测试时 patch）
        _traci = self._get_traci()

        # 构建无头模式 SUMO 命令
        sumo_bin = os.path.join(self.config.sumo_home, "bin", "sumo")
        # 如果完整路径不存在，尝试系统 PATH 中的 sumo
        if not os.path.isfile(sumo_bin):
            sumo_bin = "sumo"

        sumo_cmd = [
            sumo_bin,
            "-c", self.sumocfg_path,
            "--no-warnings", "true",
            "--no-step-log",
        ]

        # 添加 SUMO 随机种子（影响车辆插入、路径选择等随机性）
        if self.seed is not None:
            sumo_cmd.extend(["--seed", str(self.seed)])

        # 启动 SUMO 并建立 TraCI 连接
        _traci.start(sumo_cmd, port=self.port, label=self.label)

        # 获取信号灯列表
        self._tl_ids = list(_traci.trafficlight.getIDList())

        # 获取所有信号灯的受控车道（去重保序）
        all_controlled: list[str] = []
        for tl_id in self._tl_ids:
            all_controlled.extend(_traci.trafficlight.getControlledLanes(tl_id))
        self._controlled_lanes = list(dict.fromkeys(all_controlled))

        # 从受控车道推导所属边（去重），只订阅受控交叉口相关的边
        controlled_edges: list[str] = []
        for lane_id in self._controlled_lanes:
            edge_id = _traci.lane.getEdgeID(lane_id)
            if edge_id not in controlled_edges:
                controlled_edges.append(edge_id)
        self._edge_ids = controlled_edges

        # 注册受控边级订阅
        subscribe_all_edges(_traci, self._edge_ids)

        # 记录仿真前车辆集合（用于计算 passed_vehicles）
        self._vehicles_before = get_lane_vehicle_ids(_traci, self._controlled_lanes)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """关闭 TraCI 连接，kill SUMO 进程（无论是否发生异常）。"""
        _traci = self._get_traci()

        # 尝试关闭 traci 连接
        try:
            _traci.switch(self.label)
            _traci.close()
        except Exception:
            pass  # 关闭失败不掩盖原始异常

        # 兜底：如果进程仍在运行，强制 kill
        if self._sumo_proc is not None and self._sumo_proc.poll() is None:
            try:
                self._sumo_proc.kill()
            except Exception:
                pass

        return False  # 不抑制异常

    def _get_traci(self) -> Any:
        """获取 traci 模块（测试时可注入 mock）。"""
        if self._traci is not None:
            return self._traci
        return traci

    def run(self, control_fn: Callable) -> EvaluationResult:
        """
        驱动仿真循环，每步采集指标，按 decision_step_interval 调用控制回调。

        Args:
            control_fn: 控制回调函数 (traci_module) -> None

        Returns:
            EvaluationResult: 仿真评估结果
        """
        _traci = self._get_traci()
        interval = self.config.decision_step_interval
        step_metrics_list = []
        step = 0
        # 追踪每车累积等待时间和类型（用于 person-weighted 指标）
        vehicle_waiting: dict[str, float] = {}  # vid -> max accumulated waiting
        vehicle_vclass: dict[str, str] = {}     # vid -> SUMO vClass
        total_step_delay: float = 0.0  # 旧公式：每步等待时间总和

        end_time = _traci.simulation.getEndTime()

        while _traci.simulation.getTime() < end_time:
            # 采集当前步指标（边级订阅）
            metrics = collect_step_metrics(_traci)
            step_metrics_list.append(metrics)

            # 旧公式：每步累积等待时间（用于 avg_delay）
            total_step_delay += collect_vehicle_waiting_time(_traci, self._controlled_lanes)

            # 追踪每车累积等待时间和车辆类型（用于 person-weighted 指标）
            for lane_id in self._controlled_lanes:
                for vid in _traci.lane.getLastStepVehicleIDs(lane_id):
                    w = _traci.vehicle.getAccumulatedWaitingTime(vid)
                    if vid not in vehicle_waiting or w > vehicle_waiting[vid]:
                        vehicle_waiting[vid] = w
                    if vid not in vehicle_vclass:
                        vehicle_vclass[vid] = _traci.vehicle.getVehicleClass(vid)

            # 按 interval 调用控制回调
            if step % interval == 0:
                control_fn(_traci)

            # 推进仿真
            _traci.simulationStep()
            step += 1

        # 仿真结束后计算 passed_vehicles（前后车辆集合差集）
        vehicles_after = get_lane_vehicle_ids(_traci, self._controlled_lanes)
        passed_vehicles = len(self._vehicles_before - vehicles_after)

        # 聚合指标
        aggregated = aggregate_metrics(step_metrics_list)

        # avg_delay = 旧公式（每步平均等待时间，用于常规信控）
        avg_delay = total_step_delay / max(step, 1)
        total_delay = total_step_delay

        # avg_delay_person = 新公式（per-vehicle 累积等待, person-weighted, 用于事件信控）
        _OCCUPANCY = {"bus": 30, "tram": 30, "rail_urban": 30}
        _DEFAULT_OCC = 1.5
        total_person_delay = 0.0
        total_persons = 0.0
        for vid, delay in vehicle_waiting.items():
            occ = _OCCUPANCY.get(vehicle_vclass.get(vid, ""), _DEFAULT_OCC)
            total_person_delay += delay * occ
            total_persons += occ
        avg_delay_person = total_person_delay / max(total_persons, 1)

        return EvaluationResult(
            success=True,
            avg_delay=avg_delay,
            avg_delay_person=avg_delay_person,
            avg_queue=aggregated["avg_queue"],
            avg_throughput=aggregated["avg_throughput"],
            fitness=0.0,
            total_steps=step,
            passed_vehicles=passed_vehicles,
            total_delay=total_delay,
        )


def make_phase_selection_fn(
    gene: Any,
    obs_builder: Callable,
    lane_links: list,
    phase_move_map: list,
    executor_config: Any,
) -> Callable:
    """
    工厂函数：创建相位选择控制回调。

    相位选择模式：直接调用 compute_phase_values → argmax(phase_values) → setPhase

    注意：不再使用 execute_strategy（会 spawn 子进程），因为 control_fn 在仿真循环中每步调用，
    而 SumoEvaluator 本身可能已运行在 ProcessPoolExecutor worker 中，嵌套 spawn 会崩溃。
    策略已在生成阶段通过 AST 验证，运行时直接调用 compute_phase_values 即可。

    Args:
        gene: Gene 对象（包含 inlane_code/outlane_code）
        obs_builder: 函数 (traci_module, tl_id) -> obs dict
        lane_links: lane-link 结构（lane_links_per_move）
        phase_move_map: phase 到 move 的映射
        executor_config: ExecutorConfig 实例（保留参数兼容性）

    Returns:
        control_fn: 接受 traci_module 参数的控制回调函数
    """

    def control_fn(traci_module: Any) -> None:
        tl_ids = traci_module.trafficlight.getIDList()
        for tl_id in tl_ids:
            obs = obs_builder(traci_module, tl_id)
            phase_values = compute_phase_values(
                inlane_code=gene.inlane_code,
                outlane_code=gene.outlane_code,
                obs=obs,
                lane_links_per_move=lane_links,
                phase_move_map=phase_move_map,
            )
            if phase_values:
                best_phase = int(phase_values.index(max(phase_values)))
                traci_module.trafficlight.setPhase(tl_id, best_phase)

    return control_fn
