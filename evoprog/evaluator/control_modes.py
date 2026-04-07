"""
evoprog.evaluator.control_modes: 多控制模式的 SUMO 控制回调工厂。

提供三种控制模式的 ControlFnFactory 类：
  - PhaseExtensionControlFnFactory: 相位延长模式 (CTRL-02)
  - CyclePlanningControlFnFactory: 周期级规划模式 (CTRL-03)
  - ComboControlFnFactory: 组合模式（同时运行多种控制）

以及统一分发器：
  - ModeControlFnFactory: 按 Gene.control_mode 分发到对应工厂

工具函数：
  - _map_values_to_green_durations: 将 phase_values 按正值比例映射到 [min_green, max_green]
  - _rebuild_phases: 只更新绿灯相位时长，保留全红/全黄过渡相位
"""

from __future__ import annotations

from typing import Any

from evoprog.config import ExecutorConfig
from evoprog.store.models import Gene


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _map_values_to_green_durations(
    phase_values: list[float],
    min_green: float,
    max_green: float,
) -> list[float]:
    """将 compute_phase_values 输出按正值比例映射到 [min_green, max_green]。

    负值视为 0（不贡献比例）。全零时均匀分配（每个相位均为 min_green，
    因为 ratio=0/total 对所有元素均成立，不过实际上映射结果是 min_green）。

    返回浮点秒数列表（SUMO setProgramLogic 接受浮点 duration）。

    Args:
        phase_values: 策略执行结果（每个绿灯相位的"价值"）
        min_green: 最小绿灯时长（秒）
        max_green: 最大绿灯时长（秒）

    Returns:
        每个绿灯相位的目标时长（浮点秒数）
    """
    positive_values = [max(0.0, v) for v in phase_values]
    total = sum(positive_values) or 1.0  # 避免除零：全零时 total=1，ratio 全为 0 -> min_green
    return [
        min_green + (v / total) * (max_green - min_green)
        for v in positive_values
    ]


def _rebuild_phases(original_phases: list, green_durations: list[float]) -> list:
    """重建 Phase 列表：只更新含 G/g 的绿灯相位 duration，保留全红/全黄过渡相位。

    使用 type(existing_phase)(duration=new_dur, state=existing_phase.state) 构造新 Phase，
    避免直接修改返回对象（可能只读）。RESEARCH.md Open Question 2 建议。

    Args:
        original_phases: 原始 Phase 对象列表
        green_durations: 绿灯相位的新时长列表（按绿灯相位顺序）

    Returns:
        新 Phase 对象列表（绿灯相位时长已更新，过渡相位保持原样）
    """
    green_idx = 0
    new_phases = []
    for phase in original_phases:
        if any(c in ('G', 'g') for c in phase.state):
            dur = green_durations[green_idx] if green_idx < len(green_durations) else phase.duration
            green_idx += 1
            new_phases.append(type(phase)(duration=dur, state=phase.state))
        else:
            new_phases.append(phase)  # 全红/全黄相位保持原样
    return new_phases


# ---------------------------------------------------------------------------
# PhaseExtensionControlFnFactory
# ---------------------------------------------------------------------------

class PhaseExtensionControlFnFactory:
    """相位延长模式的 SUMO 控制回调工厂（CTRL-02）。

    在当前相位已运行时长 >= min_green 后，根据策略代码输出决定是否延长该相位。
    延长秒数 clamp 到 [0, max_green - spent]，到达 max_green 时不调用 setPhaseDuration。

    可 pickle（用于 ProcessPoolExecutor 跨进程传递）。
    """

    def __init__(self, gene: Gene, executor_config: ExecutorConfig):
        self.gene = gene
        self.executor_config = executor_config

    def __call__(self):
        """被 evaluate_one_scenario 在子进程内调用，返回 phase_extension control_fn。"""
        gene = self.gene
        min_green = self.executor_config.min_green_seconds
        max_green = self.executor_config.max_green_seconds

        # 惰性缓存：每个信号灯 ID 的 lane_links/phase_move_map/controlled_lanes
        lane_links_cache: dict = {}

        def control_fn(traci_module: Any) -> None:
            from evoprog.evaluator.obs_builder import (
                build_obs_from_traci,
                extract_lane_links,
                extract_phase_move_map,
            )
            from evoprog.executor.runner import compute_phase_extension_value

            tl_ids = traci_module.trafficlight.getIDList()
            for tl_id in tl_ids:
                # 惰性初始化拓扑缓存
                if tl_id not in lane_links_cache:
                    ll, unique_lanes = extract_lane_links(traci_module, tl_id)
                    pm = extract_phase_move_map(traci_module, tl_id)
                    lane_links_cache[tl_id] = (ll, pm, unique_lanes)

                ll, pm, ul = lane_links_cache[tl_id]

                # 获取当前相位已持续时长
                spent = traci_module.trafficlight.getSpentDuration(tl_id)

                # min_green 触发条件：spent < min_green 时不触发策略决策
                if spent < min_green:
                    continue

                obs = build_obs_from_traci(traci_module, tl_id, ul)
                phase_values = compute_phase_extension_value(
                    inlane_code=gene.inlane_code,
                    outlane_code=gene.outlane_code,
                    obs=obs,
                    lane_links_per_move=ll,
                    phase_move_map=pm,
                    current_green_time=spent,
                )

                if phase_values:
                    extension = max(0.0, phase_values[0])
                    remaining_budget = max_green - spent
                    extension = min(extension, remaining_budget)
                    # extension == 0 时不调用 setPhaseDuration，让 SUMO 自然切换
                    if extension > 0:
                        traci_module.trafficlight.setPhaseDuration(tl_id, extension)

        return control_fn


# ---------------------------------------------------------------------------
# CyclePlanningControlFnFactory
# ---------------------------------------------------------------------------

class CyclePlanningControlFnFactory:
    """周期级规划模式的 SUMO 控制回调工厂（CTRL-03）。

    在每个信号周期起点（phase==0 且 spent < 1.5）通过 setProgramLogic
    设置完整周期方案（绿灯相位时长按策略输出比例分配）。

    可 pickle（用于 ProcessPoolExecutor 跨进程传递）。
    """

    def __init__(self, gene: Gene, executor_config: ExecutorConfig):
        self.gene = gene
        self.executor_config = executor_config

    def __call__(self):
        """被 evaluate_one_scenario 在子进程内调用，返回 cycle_planning control_fn。"""
        gene = self.gene
        min_green = self.executor_config.min_green_seconds
        max_green = self.executor_config.max_green_seconds
        # RESEARCH.md Pitfall 3：用 spent 阈值而非 phase==0 单独判断，防止重复触发
        decision_step_interval = 1.5

        # 惰性缓存：每个信号灯 ID 的 lane_links/phase_move_map/controlled_lanes
        lane_links_cache: dict = {}

        def control_fn(traci_module: Any) -> None:
            from evoprog.evaluator.obs_builder import (
                build_obs_from_traci,
                extract_lane_links,
                extract_phase_move_map,
            )
            from evoprog.executor.runner import compute_phase_values

            tl_ids = traci_module.trafficlight.getIDList()
            for tl_id in tl_ids:
                # 惰性初始化拓扑缓存
                if tl_id not in lane_links_cache:
                    ll, unique_lanes = extract_lane_links(traci_module, tl_id)
                    pm = extract_phase_move_map(traci_module, tl_id)
                    lane_links_cache[tl_id] = (ll, pm, unique_lanes)

                ll, pm, ul = lane_links_cache[tl_id]

                # 周期起点检测：phase==0 且 spent < decision_step_interval
                current_phase = traci_module.trafficlight.getPhase(tl_id)
                spent = traci_module.trafficlight.getSpentDuration(tl_id)
                if not (current_phase == 0 and spent < decision_step_interval):
                    continue

                obs = build_obs_from_traci(traci_module, tl_id, ul)
                # 周期规划复用 compute_phase_values，输出 value[] 语义为"价值比例"
                phase_values = compute_phase_values(
                    inlane_code=gene.inlane_code,
                    outlane_code=gene.outlane_code,
                    obs=obs,
                    lane_links_per_move=ll,
                    phase_move_map=pm,
                )

                if not phase_values:
                    continue

                # 将价值比例映射为绿灯时长
                green_durations = _map_values_to_green_durations(phase_values, min_green, max_green)

                # 获取当前信号方案
                logics = traci_module.trafficlight.getAllProgramLogics(tl_id)
                if not logics:
                    continue

                logic = logics[0]
                new_phases = _rebuild_phases(logic.phases, green_durations)

                # 构建新 Logic 并设置
                new_logic = traci_module.trafficlight.Logic(
                    logic.getSubID(), 0, 0, new_phases
                )
                traci_module.trafficlight.setProgramLogic(tl_id, new_logic)

        return control_fn


# ---------------------------------------------------------------------------
# ComboControlFnFactory
# ---------------------------------------------------------------------------

class ComboControlFnFactory:
    """组合模式工厂：持有多个 Gene（按 control_mode 分组），生成联合 control_fn。

    cycle_planning 先执行（设置基准时长），phase_extension 后执行（动态调整剩余时长）。
    RESEARCH.md Open Question 3 确认两者兼容。

    可 pickle（用于 ProcessPoolExecutor 跨进程传递）。
    """

    def __init__(self, genes_by_mode: dict[str, Gene], executor_config: ExecutorConfig):
        """
        Args:
            genes_by_mode: dict，key 为 control_mode，value 为对应 Gene
            executor_config: 执行器配置
        """
        self.genes_by_mode = genes_by_mode
        self.executor_config = executor_config

    def __call__(self):
        """返回联合 control_fn，依次调用各模式的 control_fn。"""
        sub_fns = []

        # cycle_planning 先执行
        if "cycle_planning" in self.genes_by_mode:
            cp_factory = CyclePlanningControlFnFactory(
                self.genes_by_mode["cycle_planning"], self.executor_config
            )
            sub_fns.append(cp_factory())

        # phase_extension 后执行
        if "phase_extension" in self.genes_by_mode:
            pe_factory = PhaseExtensionControlFnFactory(
                self.genes_by_mode["phase_extension"], self.executor_config
            )
            sub_fns.append(pe_factory())

        def control_fn(traci_module: Any) -> None:
            for fn in sub_fns:
                fn(traci_module)

        return control_fn


# ---------------------------------------------------------------------------
# ModeControlFnFactory（统一分发器）
# ---------------------------------------------------------------------------

class ModeControlFnFactory:
    """按 Gene.control_mode 分发到对应单模式工厂的统一分发器。

    支持的控制模式：
      - "phase_selection": 相位选择（复用现有 ControlFnFactory 逻辑）
      - "phase_extension": 相位延长 (CTRL-02)
      - "cycle_planning": 周期级规划 (CTRL-03)

    可 pickle（用于 ProcessPoolExecutor 跨进程传递）。
    """

    def __init__(self, gene: Gene, executor_config: ExecutorConfig):
        self.gene = gene
        self.executor_config = executor_config

    def __call__(self):
        """按 gene.control_mode 分发到对应工厂，返回 control_fn。"""
        mode = self.gene.control_mode

        if mode == "phase_extension":
            factory = PhaseExtensionControlFnFactory(self.gene, self.executor_config)
            return factory()

        elif mode == "cycle_planning":
            factory = CyclePlanningControlFnFactory(self.gene, self.executor_config)
            return factory()

        elif mode == "phase_selection":
            # 与 daemon.py 中 ControlFnFactory.__call__ 逻辑一致
            # 直接在此实现，避免循环导入
            gene = self.gene
            lane_links_cache: dict = {}

            def control_fn(traci_module: Any) -> None:
                from evoprog.evaluator.obs_builder import (
                    build_obs_from_traci,
                    extract_lane_links,
                    extract_phase_move_map,
                )
                from evoprog.executor.runner import compute_phase_values

                tl_ids = traci_module.trafficlight.getIDList()
                for tl_id in tl_ids:
                    if tl_id not in lane_links_cache:
                        ll, unique_lanes = extract_lane_links(traci_module, tl_id)
                        pm = extract_phase_move_map(traci_module, tl_id)
                        lane_links_cache[tl_id] = (ll, pm, unique_lanes)

                    ll, pm, ul = lane_links_cache[tl_id]
                    obs = build_obs_from_traci(traci_module, tl_id, ul)
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

        else:
            raise ValueError(f"未知控制模式: {mode!r}，支持: phase_selection, phase_extension, cycle_planning")
