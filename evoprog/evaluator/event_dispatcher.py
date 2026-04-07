"""
evoprog.evaluator.event_dispatcher: 事件驱动优先级调度器。

根据 TraCI 实时事件检测结果，选择最高优先级的技能执行。
优先级固定: emergency(P0) > incident(P1) > transit(P2) > congestion(P3) > normal(P4)

用法:
    dispatcher = EventDispatcher(skills_by_type, executor_config)
    control_fn = dispatcher()
    # control_fn(traci_module) 在每个仿真步调用
"""

from __future__ import annotations

from typing import Any

from evoprog.config import ExecutorConfig
from evoprog.store.models import Gene
from evoprog.evaluator.event_detector import (
    detect_events_for_tl,
    get_active_event_type,
    event_context_to_vars,
)


class EventDispatcherFactory:
    """事件驱动调度器工厂：根据检测到的事件选择对应技能执行。

    持有多个事件特化技能 (Gene)，在仿真运行时根据 TraCI 实时事件
    选择最高优先级的技能执行。

    可 pickle（用于 ProcessPoolExecutor）。
    """

    def __init__(
        self,
        skills: dict[str, Gene],
        executor_config: ExecutorConfig,
    ):
        """
        Args:
            skills: 事件类型 -> Gene 的映射。
                    支持的 key: "normal", "emergency", "transit", "incident", "congestion"
                    至少需要 "normal" 技能。
            executor_config: 执行器配置
        """
        if "normal" not in skills:
            raise ValueError("skills 必须包含 'normal' 技能")
        self.skills = skills
        self.executor_config = executor_config

    def __call__(self):
        """返回事件驱动的 control_fn。"""
        skills = self.skills

        # 惰性缓存
        tl_cache: dict[str, dict] = {}
        # 事件统计（用于事后分析）
        event_counts: dict[str, int] = {
            "normal": 0, "emergency": 0, "transit": 0,
            "incident": 0, "congestion": 0,
        }

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
                if tl_id not in tl_cache:
                    ll, unique_lanes = extract_lane_links(traci_module, tl_id)
                    pm = extract_phase_move_map(traci_module, tl_id)
                    controlled = list(traci_module.trafficlight.getControlledLanes(tl_id))
                    tl_cache[tl_id] = {
                        "ll": ll, "pm": pm,
                        "unique_lanes": unique_lanes,
                        "controlled": controlled,
                    }

                cache = tl_cache[tl_id]
                ll = cache["ll"]
                pm = cache["pm"]
                unique_lanes = cache["unique_lanes"]
                controlled = cache["controlled"]

                # 1. 检测事件
                event_ctx = detect_events_for_tl(
                    traci_module, tl_id, controlled, pm, ll, unique_lanes
                )

                # 2. 确定最高优先级事件
                event_type = get_active_event_type(event_ctx)
                event_counts[event_type] = event_counts.get(event_type, 0) + 1

                # 3. 选择对应技能（fallback 到 normal）
                gene = skills.get(event_type, skills["normal"])

                # 4. 构建观测 + 事件上下文
                obs = build_obs_from_traci(traci_module, tl_id, unique_lanes)
                event_vars = event_context_to_vars(event_ctx)

                # 5. 执行技能代码
                phase_values = _compute_phase_values_with_events(
                    inlane_code=gene.inlane_code,
                    outlane_code=gene.outlane_code,
                    obs=obs,
                    lane_links_per_move=ll,
                    phase_move_map=pm,
                    event_vars=event_vars,
                )

                if phase_values:
                    best_phase = int(phase_values.index(max(phase_values)))
                    traci_module.trafficlight.setPhase(tl_id, best_phase)

        return control_fn


class DispatcherContextControlFnFactory:
    """在 dispatcher 上下文中评估候选 Gene 的工厂。

    用于事件技能进化：固定其他技能，只替换 target_event_type 的技能
    为候选 Gene，然后在完整事件调度管道中评估。

    可 pickle（用于 ProcessPoolExecutor）。
    """

    PREEMPT_MIN_GREEN = 5.0

    def __init__(
        self,
        fixed_skills: dict[str, Gene],
        candidate_gene: Gene,
        candidate_event_type: str,
        executor_config: ExecutorConfig,
    ):
        self.fixed_skills = fixed_skills
        self.candidate_gene = candidate_gene
        self.candidate_event_type = candidate_event_type
        self.executor_config = executor_config

    def __call__(self):
        """返回事件驱动的 control_fn，其中 target 技能被替换为候选 Gene。"""
        skills = dict(self.fixed_skills)
        skills[self.candidate_event_type] = self.candidate_gene

        tl_cache: dict[str, dict] = {}
        preempt_min = self.PREEMPT_MIN_GREEN

        def control_fn(traci_module) -> None:
            from evoprog.evaluator.obs_builder import (
                build_obs_from_traci,
                extract_lane_links,
                extract_phase_move_map,
            )

            for tl_id in traci_module.trafficlight.getIDList():
                if tl_id not in tl_cache:
                    ll, unique_lanes = extract_lane_links(traci_module, tl_id)
                    pm = extract_phase_move_map(traci_module, tl_id)
                    controlled = list(
                        traci_module.trafficlight.getControlledLanes(tl_id)
                    )
                    logics = traci_module.trafficlight.getAllProgramLogics(tl_id)
                    phases = logics[0].phases if logics else []
                    tl_cache[tl_id] = {
                        "ll": ll, "pm": pm,
                        "unique_lanes": unique_lanes,
                        "controlled": controlled,
                        "phases": phases,
                    }

                cache = tl_cache[tl_id]

                # 检测事件
                event_ctx = detect_events_for_tl(
                    traci_module, tl_id,
                    cache["controlled"], cache["pm"],
                    cache["ll"], cache["unique_lanes"],
                )
                event_type = get_active_event_type(event_ctx)
                spent = traci_module.trafficlight.getSpentDuration(tl_id)

                # Emergency: skill-based 但允许立即切换（不等相位结束）
                if event_type == "emergency" and spent >= preempt_min:
                    gene = skills.get("emergency", skills["normal"])
                    obs = build_obs_from_traci(traci_module, tl_id, cache["unique_lanes"])
                    event_vars = event_context_to_vars(event_ctx)
                    phase_values = _compute_phase_values_with_events(
                        inlane_code=gene.inlane_code,
                        outlane_code=gene.outlane_code,
                        obs=obs,
                        lane_links_per_move=cache["ll"],
                        phase_move_map=cache["pm"],
                        event_vars=event_vars,
                    )
                    if phase_values:
                        best_phase = int(phase_values.index(max(phase_values)))
                        abs_phase = _find_green_phase_abs(cache["phases"], best_phase)
                        current_phase = traci_module.trafficlight.getPhase(tl_id)
                        if current_phase != abs_phase:
                            traci_module.trafficlight.setPhase(tl_id, abs_phase)
                    continue

                # 其他事件和常规：在相位切换点决策
                remaining = traci_module.trafficlight.getNextSwitch(tl_id) - traci_module.simulation.getTime()
                if remaining > 2.0:
                    continue

                gene = skills.get(event_type, skills["normal"])
                obs = build_obs_from_traci(traci_module, tl_id, cache["unique_lanes"])
                event_vars = event_context_to_vars(event_ctx)

                phase_values = _compute_phase_values_with_events(
                    inlane_code=gene.inlane_code,
                    outlane_code=gene.outlane_code,
                    obs=obs,
                    lane_links_per_move=cache["ll"],
                    phase_move_map=cache["pm"],
                    event_vars=event_vars,
                )

                if phase_values:
                    best_phase = int(phase_values.index(max(phase_values)))
                    abs_phase = _find_green_phase_abs(cache["phases"], best_phase)
                    traci_module.trafficlight.setPhase(tl_id, abs_phase)

        return control_fn


def _find_green_phase_abs(phases, green_idx: int) -> int:
    """将绿灯相位相对索引转换为完整信号方案中的绝对索引。"""
    gi = 0
    for abs_idx, phase in enumerate(phases):
        if any(c in ("G", "g") for c in phase.state):
            if gi == green_idx:
                return abs_idx
            gi += 1
    return 0


def _compute_phase_values_with_events(
    inlane_code: str,
    outlane_code: str,
    obs: dict[str, list[float]],
    lane_links_per_move: list[list[tuple[int, int]]],
    phase_move_map: list[list[int]],
    event_vars: dict[str, float],
) -> list[float]:
    """带事件上下文的 phase value 计算。

    与 compute_phase_values 逻辑相同，但额外注入事件上下文变量
    到每个 lane-link 的执行环境中。

    Args:
        inlane_code: 入车道策略代码
        outlane_code: 出车道策略代码
        obs: 交通观测数据
        lane_links_per_move: lane-link 结构
        phase_move_map: phase 到 move 的映射
        event_vars: 事件上下文变量字典

    Returns:
        phase_values: list[float]
    """
    from evoprog.config import SAFE_BUILTINS

    num_moves = len(lane_links_per_move)
    move_values: list[float] = []

    for move_id in range(num_moves):
        value = [0.0]
        lane_links = lane_links_per_move[move_id]

        for (in_idx, out_idx) in lane_links:
            local_vars = {
                'inlane_2_num_vehicle': obs['inlane_2_num_vehicle'][in_idx],
                'outlane_2_num_vehicle': obs['outlane_2_num_vehicle'][out_idx],
                'inlane_2_num_waiting_vehicle': obs['inlane_2_num_waiting_vehicle'][in_idx],
                'outlane_2_num_waiting_vehicle': obs['outlane_2_num_waiting_vehicle'][out_idx],
                'inlane_2_vehicle_dist': obs['inlane_2_vehicle_dist'][in_idx],
                'outlane_2_vehicle_dist': obs['outlane_2_vehicle_dist'][out_idx],
                'index': in_idx,
                'value': value,
            }
            # 注入事件上下文变量
            local_vars.update(event_vars)

            if inlane_code.strip():
                exec_globals = dict(SAFE_BUILTINS)
                exec(
                    compile(inlane_code, '<inlane_strategy>', 'exec'),
                    exec_globals,
                    local_vars,
                )

            local_vars['index'] = out_idx

            if outlane_code.strip():
                exec_globals = dict(SAFE_BUILTINS)
                exec(
                    compile(outlane_code, '<outlane_strategy>', 'exec'),
                    exec_globals,
                    local_vars,
                )

        move_values.append(value[0])

    phase_values: list[float] = []
    for move_ids in phase_move_map:
        phase_val = sum(move_values[m] for m in move_ids)
        phase_values.append(phase_val)

    return phase_values
