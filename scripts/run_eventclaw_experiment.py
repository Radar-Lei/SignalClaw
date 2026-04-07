#!/usr/bin/env python3
"""
EventClaw 实验运行器：在事件注入场景中对比 EventClaw vs RL vs MaxPressure。

实验矩阵:
    场景: E1(救护车), B1(公交), I1(事故), M1(混合)
    方法: EventClaw(进化技能) vs DQN vs MaxPressure vs NoControl(固定时序)

用法:
    python scripts/run_eventclaw_experiment.py --scenario emergency_e1 --method eventclaw
    python scripts/run_eventclaw_experiment.py --all  # 运行全部
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# 添加项目根目录到 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from evoprog.config import ExecutorConfig, EvaluatorConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# 场景配置
SCENARIOS = {
    "emergency_e1": {
        "sumocfg": "scenarios/emergency_e1/emergency_e1.sumocfg",
        "type": "emergency",
        "description": "Emergency vehicles every 300s",
    },
    "emergency_e2": {
        "sumocfg": "scenarios/emergency_e2/emergency_e2.sumocfg",
        "type": "emergency",
        "description": "Emergency vehicles every 120s (high freq)",
    },
    "transit_b1": {
        "sumocfg": "scenarios/transit_b1/transit_b1.sumocfg",
        "type": "transit",
        "description": "2 bus lines, 180s interval",
    },
    "transit_b2": {
        "sumocfg": "scenarios/transit_b2/transit_b2.sumocfg",
        "type": "transit",
        "description": "4 bus lines, 120s interval",
    },
    "incident_i1": {
        "sumocfg": "scenarios/incident_i1/incident_i1.sumocfg",
        "type": "incident",
        "description": "Single incident at t=600",
    },
    "mixed_m1": {
        "sumocfg": "scenarios/mixed_m1/mixed_m1.sumocfg",
        "type": "mixed",
        "description": "Emergency + bus + incident",
    },
    # 对照: 无事件的 T1 场景
    "arterial4x4_t1": {
        "sumocfg": "scenarios/arterial4x4_t1/arterial4x4_t1.sumocfg",
        "type": "normal",
        "description": "Baseline: balanced uniform (no events)",
    },
}


def run_no_control(sumocfg_path: str, evaluator_config: EvaluatorConfig) -> dict:
    """运行固定时序控制（无自适应）作为基线。"""
    from evoprog.evaluator.runner import SumoEvaluator
    from evoprog.evaluator.event_metrics import EventMetricsCollector

    port = 18800 + os.getpid() % 1000
    label = f"nocontrol_{port}"

    collector = EventMetricsCollector()

    with SumoEvaluator(sumocfg_path, port, label, evaluator_config) as evaluator:
        # 用空 control_fn（不做任何控制）
        def noop_control(traci_module):
            collector.collect_step(traci_module, evaluator._controlled_lanes)

        result = evaluator.run(noop_control)

    event_metrics = collector.finalize()
    return {
        "method": "no_control",
        "avg_delay": result.avg_delay,
        "avg_queue": result.avg_queue,
        "avg_throughput": result.avg_throughput,
        "total_steps": result.total_steps,
        "emergency_avg_delay": event_metrics.emergency_avg_delay,
        "emergency_count": event_metrics.emergency_vehicle_count,
        "bus_avg_delay": event_metrics.bus_avg_delay,
        "bus_count": event_metrics.bus_count,
        "normal_avg_delay": event_metrics.normal_avg_delay,
    }


def _find_green_phase_absolute_index(phases, green_idx: int) -> int:
    """将绿灯相位相对索引转换为完整信号方案中的绝对索引。"""
    gi = 0
    for abs_idx, phase in enumerate(phases):
        if any(c in ("G", "g") for c in phase.state):
            if gi == green_idx:
                return abs_idx
            gi += 1
    return 0


def run_maxpressure(sumocfg_path: str, evaluator_config: EvaluatorConfig) -> dict:
    """运行 MaxPressure 基线（只在当前绿灯即将结束时决策下一个相位）。"""
    from evoprog.evaluator.runner import SumoEvaluator
    from evoprog.evaluator.event_metrics import EventMetricsCollector

    port = 18800 + os.getpid() % 1000
    label = f"maxpressure_{port}"

    collector = EventMetricsCollector()

    with SumoEvaluator(sumocfg_path, port, label, evaluator_config) as evaluator:
        mp_cache = {}

        def maxpressure_control(traci_module):
            collector.collect_step(traci_module, evaluator._controlled_lanes)

            for tl_id in traci_module.trafficlight.getIDList():
                # 只在当前相位即将结束时（剩余 < 2s）决策
                remaining = traci_module.trafficlight.getNextSwitch(tl_id) - traci_module.simulation.getTime()
                if remaining > 2.0:
                    continue

                if tl_id not in mp_cache:
                    controlled = list(
                        traci_module.trafficlight.getControlledLanes(tl_id)
                    )
                    links = traci_module.trafficlight.getControlledLinks(tl_id)
                    logics = traci_module.trafficlight.getAllProgramLogics(tl_id)
                    phases = logics[0].phases if logics else []
                    mp_cache[tl_id] = {
                        "controlled": controlled,
                        "links": links,
                        "phases": phases,
                    }

                cache = mp_cache[tl_id]
                phases = cache["phases"]

                best_phase = 0
                best_pressure = -float("inf")
                green_idx = 0

                for phase in phases:
                    if not any(c in ("G", "g") for c in phase.state):
                        continue

                    pressure = 0.0
                    for i, c in enumerate(phase.state):
                        if c in ("G", "g"):
                            links_for_move = cache["links"][i] if i < len(cache["links"]) else []
                            for link in links_for_move:
                                in_lane = link[0]
                                out_lane = link[1]
                                in_q = traci_module.lane.getLastStepHaltingNumber(in_lane)
                                out_q = traci_module.lane.getLastStepHaltingNumber(out_lane)
                                pressure += in_q - out_q

                    if pressure > best_pressure:
                        best_pressure = pressure
                        best_phase = green_idx
                    green_idx += 1

                abs_phase = _find_green_phase_absolute_index(phases, best_phase)
                traci_module.trafficlight.setPhase(tl_id, abs_phase)

        result = evaluator.run(maxpressure_control)

    event_metrics = collector.finalize()
    return {
        "method": "maxpressure",
        "avg_delay": result.avg_delay,
        "avg_queue": result.avg_queue,
        "avg_throughput": result.avg_throughput,
        "total_steps": result.total_steps,
        "emergency_avg_delay": event_metrics.emergency_avg_delay,
        "emergency_count": event_metrics.emergency_vehicle_count,
        "bus_avg_delay": event_metrics.bus_avg_delay,
        "bus_count": event_metrics.bus_count,
        "normal_avg_delay": event_metrics.normal_avg_delay,
    }


def run_handcrafted_preemption(
    sumocfg_path: str,
    scenario_type: str,
    evaluator_config: EvaluatorConfig,
) -> dict:
    """MaxPressure + handcrafted emergency preemption (no LLM/evolution).

    This baseline uses the same event detector and phase-switching logic as
    SignalClaw, but replaces ALL evolved skills with pure MaxPressure.
    Purpose: isolate whether the emergency 0.0s result comes from the
    handcrafted preemption rule or from the evolved skill code.
    """
    from evoprog.evaluator.runner import SumoEvaluator
    from evoprog.evaluator.event_detector import (
        detect_events_for_tl,
        get_active_event_type,
    )
    from evoprog.evaluator.obs_builder import (
        extract_lane_links,
        extract_phase_move_map,
    )
    from evoprog.evaluator.event_metrics import EventMetricsCollector

    PREEMPT_MIN_GREEN = 5.0

    port = 18800 + os.getpid() % 1000
    label = f"handcrafted_{port}"
    collector = EventMetricsCollector()

    with SumoEvaluator(sumocfg_path, port, label, evaluator_config) as evaluator:
        tl_cache = {}

        def handcrafted_control(traci_module):
            collector.collect_step(traci_module, evaluator._controlled_lanes)

            for tl_id in traci_module.trafficlight.getIDList():
                if tl_id not in tl_cache:
                    ll, unique_lanes = extract_lane_links(traci_module, tl_id)
                    pm = extract_phase_move_map(traci_module, tl_id)
                    controlled = list(
                        traci_module.trafficlight.getControlledLanes(tl_id)
                    )
                    links = traci_module.trafficlight.getControlledLinks(tl_id)
                    logics = traci_module.trafficlight.getAllProgramLogics(tl_id)
                    phases = logics[0].phases if logics else []
                    tl_cache[tl_id] = {
                        "ll": ll, "pm": pm,
                        "unique_lanes": unique_lanes,
                        "controlled": controlled,
                        "links": links,
                        "phases": phases,
                    }

                cache = tl_cache[tl_id]

                # Detect events
                event_ctx = detect_events_for_tl(
                    traci_module, tl_id,
                    cache["controlled"], cache["pm"],
                    cache["ll"], cache["unique_lanes"],
                )
                event_type = get_active_event_type(event_ctx)
                spent = traci_module.trafficlight.getSpentDuration(tl_id)

                if event_type == "emergency":
                    # Same preemption as SignalClaw
                    target_green = event_ctx.emergency_phase
                    if target_green >= 0 and spent >= PREEMPT_MIN_GREEN:
                        abs_phase = _find_green_phase_absolute_index(
                            cache["phases"], target_green
                        )
                        current_phase = traci_module.trafficlight.getPhase(tl_id)
                        if current_phase != abs_phase:
                            traci_module.trafficlight.setPhase(tl_id, abs_phase)
                        else:
                            remaining = traci_module.trafficlight.getNextSwitch(tl_id) - traci_module.simulation.getTime()
                            if remaining < 10.0:
                                traci_module.trafficlight.setPhaseDuration(tl_id, 15.0)
                else:
                    # Pure MaxPressure for all non-emergency traffic
                    remaining = traci_module.trafficlight.getNextSwitch(tl_id) - traci_module.simulation.getTime()
                    if remaining > 2.0:
                        continue

                    best_phase = 0
                    best_pressure = -float("inf")
                    green_idx = 0

                    for phase in cache["phases"]:
                        if not any(c in ("G", "g") for c in phase.state):
                            continue
                        pressure = 0.0
                        for i, c in enumerate(phase.state):
                            if c in ("G", "g"):
                                links_for_move = cache["links"][i] if i < len(cache["links"]) else []
                                for link in links_for_move:
                                    in_lane = link[0]
                                    out_lane = link[1]
                                    in_q = traci_module.lane.getLastStepHaltingNumber(in_lane)
                                    out_q = traci_module.lane.getLastStepHaltingNumber(out_lane)
                                    pressure += in_q - out_q
                        if pressure > best_pressure:
                            best_pressure = pressure
                            best_phase = green_idx
                        green_idx += 1

                    abs_phase = _find_green_phase_absolute_index(
                        cache["phases"], best_phase
                    )
                    traci_module.trafficlight.setPhase(tl_id, abs_phase)

        result = evaluator.run(handcrafted_control)

    event_metrics = collector.finalize()
    return {
        "method": "handcrafted_preemption",
        "avg_delay": result.avg_delay,
        "avg_queue": result.avg_queue,
        "avg_throughput": result.avg_throughput,
        "total_steps": result.total_steps,
        "emergency_avg_delay": event_metrics.emergency_avg_delay,
        "emergency_count": event_metrics.emergency_vehicle_count,
        "bus_avg_delay": event_metrics.bus_avg_delay,
        "bus_count": event_metrics.bus_count,
        "normal_avg_delay": event_metrics.normal_avg_delay,
    }


def _load_evolved_skill(store_dir: str) -> dict | None:
    """Load best evolved skill from a GLM-5 evolution store directory.

    Returns dict with 'inlane_code' and 'outlane_code', or None if not found.
    """
    import json
    genes_path = Path(store_dir) / "phase_selection" / "genes.json"
    ckpt_path = Path(store_dir) / "phase_selection" / "checkpoint.json"

    if not genes_path.exists() or not ckpt_path.exists():
        return None

    with open(ckpt_path) as f:
        ckpt = json.load(f)
    best_id = ckpt.get("best_gene_id")
    if not best_id:
        return None

    with open(genes_path) as f:
        genes = json.load(f)
    for g in genes:
        if g.get("id") == best_id:
            return {"inlane_code": g["inlane_code"], "outlane_code": g["outlane_code"]}
    return None


# GPT-5.4-high evolved skill directories (V2 dispatcher-context evolution)
EVOLVED_SKILL_DIRS = {
    "emergency": "store/v2_improved/gpt5_evolve/emergency_dispatcher",
    "transit": "store/v2_improved/gpt5_evolve/transit_v3",
    "incident": "store/v2_improved/gpt5_evolve/incident_v3",
    "mixed": "store/v2_improved/gpt5_evolve/mixed_dispatcher",
}

# Normal skill evolved on T1+T2+T3 (V2)
EVOLVED_NORMAL_DIR = "store/v2_improved/gpt5_evolve/normal"


def run_eventclaw_handcrafted(
    sumocfg_path: str,
    scenario_type: str,
    evaluator_config: EvaluatorConfig,
    seed: int = None,
) -> dict:
    """运行 EventClaw，优先使用 GLM-5 进化技能，回退到手写种子技能。"""
    from evoprog.evaluator.runner import SumoEvaluator
    from evoprog.evaluator.event_detector import (
        EventContext,
        detect_events_for_tl,
        get_active_event_type,
        event_context_to_vars,
    )
    from evoprog.evaluator.obs_builder import (
        build_obs_from_traci,
        extract_lane_links,
        extract_phase_move_map,
    )
    from evoprog.evaluator.event_dispatcher import _compute_phase_values_with_events
    from evoprog.evaluator.event_metrics import EventMetricsCollector
    from evoprog.store.models import Gene

    # 手写种子技能（回退）
    SEED_SKILLS = {
        "normal": {
            "inlane_code": "value[0] += inlane_2_num_waiting_vehicle * 3 + inlane_2_num_vehicle",
            "outlane_code": "value[0] -= outlane_2_num_vehicle * 2",
        },
        "emergency": {
            "inlane_code": (
                "if event_emergency_count > 0:\n"
                "    value[0] += max(0, 200 - event_emergency_distance) * 10\n"
                "else:\n"
                "    value[0] += inlane_2_num_waiting_vehicle * 3"
            ),
            "outlane_code": "value[0] -= outlane_2_num_vehicle * 0.3",
        },
        "transit": {
            "inlane_code": (
                "value[0] += inlane_2_num_waiting_vehicle * 3 + inlane_2_num_vehicle\n"
                "if event_bus_count > 0 and event_bus_distance < 50:\n"
                "    value[0] += inlane_2_num_waiting_vehicle + max(0, 50 - event_bus_distance) * 0.2"
            ),
            "outlane_code": "value[0] -= outlane_2_num_vehicle * 2",
        },
        "incident": {
            "inlane_code": (
                "if event_incident_blocked > 0:\n"
                "    value[0] += max(0, inlane_2_num_vehicle - inlane_2_num_waiting_vehicle) * 5\n"
                "else:\n"
                "    value[0] += inlane_2_num_waiting_vehicle * 3"
            ),
            "outlane_code": "value[0] -= outlane_2_num_vehicle * 0.5",
        },
        "congestion": {
            "inlane_code": (
                "value[0] += inlane_2_num_waiting_vehicle ** 2\n"
                "if event_congestion_level > 1:\n"
                "    value[0] += inlane_2_num_waiting_vehicle * event_congestion_level * 2"
            ),
            "outlane_code": "value[0] += outlane_2_vehicle_dist * 0.5",
        },
    }

    # 加载进化技能（如果存在），否则回退到种子技能
    SKILLS = {}
    for event_type, seed_skill in SEED_SKILLS.items():
        # 查找进化目录：事件技能 -> EVOLVED_SKILL_DIRS, normal -> EVOLVED_NORMAL_DIR
        if event_type == "normal":
            evolved_dir = EVOLVED_NORMAL_DIR
        else:
            evolved_dir = EVOLVED_SKILL_DIRS.get(event_type)
        evolved = None
        if evolved_dir:
            evolved = _load_evolved_skill(str(PROJECT_ROOT / evolved_dir))
        if evolved:
            logger.info(f"  Using evolved skill for '{event_type}' from {evolved_dir}")
            skill_data = evolved
        else:
            skill_data = seed_skill
        SKILLS[event_type] = Gene(
            id=f"{'evolved' if evolved else 'seed'}_{event_type}",
            inlane_code=skill_data["inlane_code"],
            outlane_code=skill_data["outlane_code"],
            control_mode="phase_selection",
        )

    # For mixed scenario, also try loading mixed-specific emergency skill
    if scenario_type == "mixed":
        mixed_evolved = _load_evolved_skill(
            str(PROJECT_ROOT / "store/glm5_evolve/mixed_dispatcher")
        )
        if mixed_evolved:
            logger.info("  Using mixed-evolved emergency skill for M1")
            SKILLS["emergency"] = Gene(
                id="evolved_emergency_mixed",
                inlane_code=mixed_evolved["inlane_code"],
                outlane_code=mixed_evolved["outlane_code"],
                control_mode="phase_selection",
            )

    PREEMPT_MIN_GREEN = 4.0  # 最小绿灯时间（秒）
    USE_GLOBAL_COORD = False  # 全局协同效果不佳，禁用

    port = 18800 + os.getpid() % 1000
    label = f"eventclaw_{port}"
    collector = EventMetricsCollector()
    event_type_counts = {}

    with SumoEvaluator(sumocfg_path, port, label, evaluator_config, seed=seed) as evaluator:
        _traci = evaluator._get_traci()
        tl_cache = {}

        step_counter = [0]

        def _eval_skill_for_tl(traci_module, tl_id, gene, event_ctx):
            """为单个 TL 计算 phase_values（复用辅助函数）。"""
            cache = tl_cache[tl_id]
            obs = build_obs_from_traci(
                traci_module, tl_id, cache["unique_lanes"]
            )
            event_vars = event_context_to_vars(event_ctx)
            return _compute_phase_values_with_events(
                inlane_code=gene.inlane_code,
                outlane_code=gene.outlane_code,
                obs=obs,
                lane_links_per_move=cache["ll"],
                phase_move_map=cache["pm"],
                event_vars=event_vars,
            )

        def eventclaw_control(traci_module):
            collector.collect_step(traci_module, evaluator._controlled_lanes)
            step_counter[0] += 1

            # 初始化 TL 缓存
            tl_ids = traci_module.trafficlight.getIDList()
            for tl_id in tl_ids:
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

            # === 全局协同模式（transit 场景）===
            if USE_GLOBAL_COORD and step_counter[0] % 5 == 0:
                # 检查是否有紧急车辆需要 per-TL 处理
                emergency_tls = {}
                for tl_id in tl_ids:
                    cache = tl_cache[tl_id]
                    event_ctx = detect_events_for_tl(
                        traci_module, tl_id,
                        cache["controlled"], cache["pm"],
                        cache["ll"], cache["unique_lanes"],
                    )
                    et = get_active_event_type(event_ctx)
                    event_type_counts[et] = event_type_counts.get(et, 0) + 1
                    if et == "emergency":
                        emergency_tls[tl_id] = event_ctx

                # 全局相位投票：每个 TL 用 normal skill 计算 phase_values，求和
                gene = SKILLS["normal"]
                n_phases = len(tl_cache[tl_ids[0]]["pm"])
                global_values = [0.0] * n_phases
                valid = False

                for tl_id in tl_ids:
                    if tl_id in emergency_tls:
                        continue  # 紧急 TL 单独处理
                    spent = traci_module.trafficlight.getSpentDuration(tl_id)
                    if spent < PREEMPT_MIN_GREEN:
                        continue
                    cache = tl_cache[tl_id]
                    event_ctx_normal = EventContext()  # 空事件上下文
                    pv = _eval_skill_for_tl(traci_module, tl_id, gene, event_ctx_normal)
                    if pv and len(pv) == n_phases:
                        for i in range(n_phases):
                            global_values[i] += pv[i]
                        valid = True

                if valid:
                    best_global = int(global_values.index(max(global_values)))
                    for tl_id in tl_ids:
                        if tl_id in emergency_tls:
                            continue
                        spent = traci_module.trafficlight.getSpentDuration(tl_id)
                        if spent < PREEMPT_MIN_GREEN:
                            continue
                        cache = tl_cache[tl_id]
                        abs_phase = _find_green_phase_absolute_index(
                            cache["phases"], best_global
                        )
                        traci_module.trafficlight.setPhase(tl_id, abs_phase)

                # 紧急 TL 独立处理
                for tl_id, event_ctx in emergency_tls.items():
                    spent = traci_module.trafficlight.getSpentDuration(tl_id)
                    emg_dist = event_ctx.emergency_distance
                    if spent >= PREEMPT_MIN_GREEN and emg_dist < 80:
                        target_green = event_ctx.emergency_phase
                        if target_green >= 0:
                            cache = tl_cache[tl_id]
                            pv = _eval_skill_for_tl(
                                traci_module, tl_id,
                                SKILLS.get("emergency", gene), event_ctx
                            )
                            if pv:
                                bonus = 500.0 * max(0, 1.0 - emg_dist / 80.0)
                                pv[target_green] += bonus
                                best = int(pv.index(max(pv)))
                                abs_phase = _find_green_phase_absolute_index(
                                    cache["phases"], best
                                )
                                traci_module.trafficlight.setPhase(tl_id, abs_phase)
                return

            # === Per-TL 模式（非 transit 场景，或非全局评估步） ===
            if USE_GLOBAL_COORD:
                return  # transit 场景只在 %5 步做全局评估

            for tl_id in tl_ids:
                cache = tl_cache[tl_id]

                # 检测事件
                event_ctx = detect_events_for_tl(
                    traci_module, tl_id,
                    cache["controlled"], cache["pm"],
                    cache["ll"], cache["unique_lanes"],
                )
                event_type = get_active_event_type(event_ctx)
                event_type_counts[event_type] = (
                    event_type_counts.get(event_type, 0) + 1
                )

                spent = traci_module.trafficlight.getSpentDuration(tl_id)

                # === normal/congestion/transit: 进化技能 ===
                if event_type in ("normal", "congestion", "transit"):
                    # 每步评估，但 spent < min_green 时不切换
                    remaining = traci_module.trafficlight.getNextSwitch(tl_id) - traci_module.simulation.getTime()
                    if remaining > 2.0 and spent < PREEMPT_MIN_GREEN:
                        continue
                    gene = SKILLS["normal"]
                    phase_values = _eval_skill_for_tl(traci_module, tl_id, gene, event_ctx)
                    if phase_values:
                        best = int(phase_values.index(max(phase_values)))
                        abs_phase = _find_green_phase_absolute_index(
                            cache["phases"], best
                        )
                        traci_module.trafficlight.setPhase(tl_id, abs_phase)
                    continue

                if event_type == "emergency":
                    emg_dist = event_ctx.emergency_distance
                    if spent >= PREEMPT_MIN_GREEN and emg_dist < 80:
                        target_green = event_ctx.emergency_phase
                        if target_green >= 0:
                            gene = SKILLS.get("emergency", SKILLS["normal"])
                            phase_values = _eval_skill_for_tl(traci_module, tl_id, gene, event_ctx)
                            if phase_values:
                                bonus = 500.0 * max(0, 1.0 - emg_dist / 80.0)
                                phase_values[target_green] += bonus
                                best = int(phase_values.index(max(phase_values)))
                                abs_phase = _find_green_phase_absolute_index(
                                    cache["phases"], best
                                )
                                current_phase = traci_module.trafficlight.getPhase(tl_id)
                                if current_phase != abs_phase:
                                    traci_module.trafficlight.setPhase(tl_id, abs_phase)

                elif event_type == "incident":
                    remaining = traci_module.trafficlight.getNextSwitch(tl_id) - traci_module.simulation.getTime()
                    if spent >= PREEMPT_MIN_GREEN:
                        if remaining > 2.0 and int(spent) % 5 != 0:
                            continue
                        gene = SKILLS.get("incident", SKILLS["normal"])
                        phase_values = _eval_skill_for_tl(traci_module, tl_id, gene, event_ctx)
                        if phase_values:
                            best = int(phase_values.index(max(phase_values)))
                            abs_phase = _find_green_phase_absolute_index(
                                cache["phases"], best
                            )
                            traci_module.trafficlight.setPhase(tl_id, abs_phase)

        result = evaluator.run(eventclaw_control)

    event_metrics = collector.finalize()
    logger.info(f"Event type distribution: {event_type_counts}")

    return {
        "method": "eventclaw_seed",
        "avg_delay": result.avg_delay,
        "avg_queue": result.avg_queue,
        "avg_throughput": result.avg_throughput,
        "total_steps": result.total_steps,
        "emergency_avg_delay": event_metrics.emergency_avg_delay,
        "emergency_count": event_metrics.emergency_vehicle_count,
        "bus_avg_delay": event_metrics.bus_avg_delay,
        "bus_count": event_metrics.bus_count,
        "normal_avg_delay": event_metrics.normal_avg_delay,
        "event_type_counts": event_type_counts,
    }


def run_experiment(scenario_name: str, methods: list[str] = None):
    """运行单个场景的全部或指定方法对比实验。"""
    if scenario_name not in SCENARIOS:
        logger.error(f"Unknown scenario: {scenario_name}")
        return

    scenario = SCENARIOS[scenario_name]
    sumocfg = str(PROJECT_ROOT / scenario["sumocfg"])
    scenario_type = scenario["type"]

    if not Path(sumocfg).exists():
        logger.error(f"Scenario not found: {sumocfg}")
        logger.info("Run: python scripts/generate_event_scenarios.py")
        return

    if methods is None:
        methods = ["no_control", "maxpressure", "eventclaw"]

    evaluator_config = EvaluatorConfig()
    results = []

    logger.info(f"{'='*60}")
    logger.info(f"Scenario: {scenario_name} ({scenario['description']})")
    logger.info(f"Type: {scenario_type}")
    logger.info(f"Methods: {methods}")
    logger.info(f"{'='*60}")

    for method in methods:
        logger.info(f"\n--- Running {method} ---")
        t0 = time.time()

        try:
            if method == "no_control":
                result = run_no_control(sumocfg, evaluator_config)
            elif method == "maxpressure":
                result = run_maxpressure(sumocfg, evaluator_config)
            elif method == "eventclaw":
                result = run_eventclaw_handcrafted(
                    sumocfg, scenario_type, evaluator_config
                )
            elif method == "handcrafted_preemption":
                result = run_handcrafted_preemption(
                    sumocfg, scenario_type, evaluator_config
                )
            else:
                logger.warning(f"Unknown method: {method}, skipping")
                continue

            elapsed = time.time() - t0
            result["scenario"] = scenario_name
            result["scenario_type"] = scenario_type
            result["elapsed_seconds"] = round(elapsed, 1)
            results.append(result)

            logger.info(
                f"  avg_delay={result['avg_delay']:.1f}, "
                f"avg_queue={result['avg_queue']:.1f}, "
                f"emg_delay={result.get('emergency_avg_delay', 0):.1f}, "
                f"bus_delay={result.get('bus_avg_delay', 0):.1f}, "
                f"time={elapsed:.1f}s"
            )
        except Exception as e:
            logger.error(f"  FAILED: {e}", exc_info=True)
            results.append({
                "method": method,
                "scenario": scenario_name,
                "error": str(e),
            })

    # 保存结果
    output_dir = PROJECT_ROOT / "store" / "eventclaw_results"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{scenario_name}_results.json"

    with open(output_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    logger.info(f"\nResults saved to {output_file}")

    # 打印对比表
    print_comparison_table(results)

    return results


def print_comparison_table(results: list[dict]):
    """打印对比表格。"""
    print("\n" + "=" * 80)
    print(f"{'Method':<20} {'AvgDelay':>10} {'EmgDelay':>10} {'BusDelay':>10} "
          f"{'NrmDelay':>10} {'Queue':>8}")
    print("-" * 80)
    for r in results:
        if "error" in r:
            print(f"{r['method']:<20} ERROR: {r['error']}")
            continue
        print(
            f"{r['method']:<20} "
            f"{r['avg_delay']:>10.1f} "
            f"{r.get('emergency_avg_delay', 0):>10.1f} "
            f"{r.get('bus_avg_delay', 0):>10.1f} "
            f"{r.get('normal_avg_delay', 0):>10.1f} "
            f"{r.get('avg_queue', 0):>8.1f}"
        )
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="EventClaw experiment runner")
    parser.add_argument("--scenario", type=str, help="Scenario name (e.g. emergency_e1)")
    parser.add_argument("--method", type=str, nargs="+",
                        help="Methods to run (no_control, maxpressure, eventclaw)")
    parser.add_argument("--all", action="store_true",
                        help="Run all event scenarios")
    parser.add_argument("--quick", action="store_true",
                        help="Quick test: E1 + B1 only")
    args = parser.parse_args()

    if args.all:
        all_results = {}
        for name in ["emergency_e1", "emergency_e2", "transit_b1",
                      "transit_b2", "incident_i1", "mixed_m1"]:
            results = run_experiment(name, args.method)
            all_results[name] = results

        # 保存汇总
        summary_path = PROJECT_ROOT / "store" / "eventclaw_results" / "ALL_RESULTS.json"
        with open(summary_path, "w") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        logger.info(f"\nAll results saved to {summary_path}")

    elif args.quick:
        for name in ["emergency_e1", "transit_b1"]:
            run_experiment(name, args.method)

    elif args.scenario:
        run_experiment(args.scenario, args.method)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
