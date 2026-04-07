"""
evoprog.executor.runner: PI-Light 执行框架。

复现 PI-Light 的 lane-link 遍历逻辑（_get_value_for_move2 + _aggregate_for_each_phase），
集成 AST 验证 + multiprocessing 超时 + 安全约束检查的完整执行流。
"""

from __future__ import annotations

import multiprocessing
from dataclasses import dataclass, field
from typing import Optional

from evoprog.config import ExecutorConfig, SAFE_BUILTINS
from evoprog.executor.sandbox import validate_code
from evoprog.executor.constraints import (
    SafetyConstraints,
    Violation,
    apply_constraints,
)


@dataclass
class ExecutionResult:
    """策略执行结果。"""
    success: bool
    phase_values: Optional[list[float]]
    error_type: Optional[str] = None   # 'syntax_error'|'forbidden_access'|'timeout'|'runtime_error'
    error_message: Optional[str] = None
    lineno: Optional[int] = None
    forbidden_name: Optional[str] = None
    violations: list[Violation] = field(default_factory=list)


def compute_phase_values(
    inlane_code: str,
    outlane_code: str,
    obs: dict[str, list[float]],
    lane_links_per_move: list[list[tuple[int, int]]],
    phase_move_map: list[list[int]],
) -> list[float]:
    """
    复现 PI-Light 的执行框架（_get_value_for_move2 + _aggregate_for_each_phase）。

    执行流程：
    1. 对每个 move，遍历其所有 lane-link
    2. 每个 lane-link 上依次 exec inlane_code 和 outlane_code
    3. value=[0.0] 列表引用语义（Pitfall 5）：exec 内通过 value[0] += 修改
    4. 每个 lane-link 的 value[0] 贡献累加到 move_value
    5. 按 phase_move_map 聚合 move_values 得到 phase_values

    Args:
        inlane_code: 入车道策略代码（在每个 lane-link 的 inlane 侧执行）
        outlane_code: 出车道策略代码（在每个 lane-link 的 outlane 侧执行）
        obs: 交通观测数据，dict[变量名, list[float]] 形式，每个变量按车道索引访问
        lane_links_per_move: 每个 move 的 lane-link 列表，每个 lane-link 是 (inlane_idx, outlane_idx)
        phase_move_map: phase_move_map[phase_id] = [move_id, ...]

    Returns:
        phase_values: list[float]，每个 phase 的聚合值（用于 argmax 选相位）
    """
    num_moves = len(lane_links_per_move)
    move_values: list[float] = []

    for move_id in range(num_moves):
        value = [0.0]  # 列表引用语义（Pitfall 5）
        lane_links = lane_links_per_move[move_id]

        for (in_idx, out_idx) in lane_links:
            # 为每个 lane-link 注入标量值（而非列表）到 exec 的 locals
            # 注意：AST 验证禁止了属性访问，所以直接注入标量而非 obs 字典
            local_vars = {
                'inlane_2_num_vehicle': obs['inlane_2_num_vehicle'][in_idx],
                'outlane_2_num_vehicle': obs['outlane_2_num_vehicle'][out_idx],
                'inlane_2_num_waiting_vehicle': obs['inlane_2_num_waiting_vehicle'][in_idx],
                'outlane_2_num_waiting_vehicle': obs['outlane_2_num_waiting_vehicle'][out_idx],
                'inlane_2_vehicle_dist': obs['inlane_2_vehicle_dist'][in_idx],
                'outlane_2_vehicle_dist': obs['outlane_2_vehicle_dist'][out_idx],
                'index': in_idx,
                'value': value,  # 列表引用，exec 内修改 value[0] 会影响外部
            }

            # 执行 inlane_code
            if inlane_code.strip():
                exec_globals = dict(SAFE_BUILTINS)
                exec(
                    compile(inlane_code, '<inlane_strategy>', 'exec'),
                    exec_globals,
                    local_vars,
                )

            # 更新 index 为 outlane 侧
            local_vars['index'] = out_idx

            # 执行 outlane_code
            if outlane_code.strip():
                exec_globals = dict(SAFE_BUILTINS)
                exec(
                    compile(outlane_code, '<outlane_strategy>', 'exec'),
                    exec_globals,
                    local_vars,
                )

        move_values.append(value[0])

    # 按 phase_move_map 聚合 move_values 到 phase_values
    phase_values: list[float] = []
    for move_ids in phase_move_map:
        phase_val = sum(move_values[m] for m in move_ids)
        phase_values.append(phase_val)

    return phase_values


def compute_phase_extension_value(
    inlane_code: str,
    outlane_code: str,
    obs: dict[str, list[float]],
    lane_links_per_move: list[list[tuple[int, int]]],
    phase_move_map: list[list[int]],
    current_green_time: float = 0.0,
) -> list[float]:
    """
    相位延长模式的执行框架：复用 lane-link 遍历逻辑，额外注入 current_green_time。

    与 compute_phase_values 逻辑相同，唯一区别是在 local_vars 中额外注入
    `current_green_time`，供策略代码用于判断延长时长。

    设计理由：创建独立函数而非给 compute_phase_values 加 extra_vars 参数，
    避免通用函数签名因特例而复杂化。compute_phase_values 是 Phase 1 以来的
    核心 API，保持签名稳定。

    Args:
        inlane_code: 入车道策略代码
        outlane_code: 出车道策略代码
        obs: 交通观测数据
        lane_links_per_move: lane-link 结构
        phase_move_map: phase 到 move 的映射
        current_green_time: 当前绿灯已持续时长（由 TraCI getSpentDuration 提供）

    Returns:
        phase_values: list[float]，每个 phase 的聚合值（value[0] 语义为延长秒数）
    """
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
                'current_green_time': current_green_time,  # 相位延长模式额外注入
            }

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


def _compute_phase_values_subprocess(
    inlane_code: str,
    outlane_code: str,
    obs: dict,
    lane_links_per_move: list,
    phase_move_map: list,
    result_queue: 'multiprocessing.Queue',
) -> None:
    """在子进程中执行 compute_phase_values，结果通过 Queue 传回。"""
    try:
        phase_values = compute_phase_values(
            inlane_code=inlane_code,
            outlane_code=outlane_code,
            obs=obs,
            lane_links_per_move=lane_links_per_move,
            phase_move_map=phase_move_map,
        )
        result_queue.put(('ok', phase_values))
    except Exception as e:
        result_queue.put(('error', str(e)))


def execute_strategy(
    inlane_code: str,
    outlane_code: str,
    obs: dict[str, list[float]],
    lane_links_per_move: list[list[tuple[int, int]]],
    phase_move_map: list[list[int]],
    config: ExecutorConfig,
) -> ExecutionResult:
    """
    完整策略执行流：AST 验证 → 受限执行（带超时）→ 安全约束检查。

    Step 1: AST 白名单验证（validate_code）
    Step 2: 在 multiprocessing 子进程中执行 compute_phase_values（超时保护）
    Step 3: apply_constraints 对结果进行 clamp 约束检查

    Args:
        inlane_code: 入车道策略代码
        outlane_code: 出车道策略代码
        obs: 交通观测数据
        lane_links_per_move: lane-link 结构
        phase_move_map: phase 到 move 的映射
        config: 执行配置（超时、约束参数等）

    Returns:
        ExecutionResult: 包含执行结果、错误信息、约束违规记录
    """
    # Step 1: AST 验证
    inlane_errors = validate_code(inlane_code) if inlane_code.strip() else []
    outlane_errors = validate_code(outlane_code) if outlane_code.strip() else []
    all_errors = inlane_errors + outlane_errors

    if all_errors:
        e = all_errors[0]
        return ExecutionResult(
            success=False,
            phase_values=None,
            error_type=e.error_type,
            error_message=str(e),
            lineno=e.lineno if e.lineno else None,
            forbidden_name=e.forbidden_name if e.forbidden_name else None,
            violations=[],
        )

    # Step 2: 受限执行（multiprocessing 超时保护）
    ctx = multiprocessing.get_context('spawn')
    q = ctx.Queue()
    p = ctx.Process(
        target=_compute_phase_values_subprocess,
        args=(inlane_code, outlane_code, obs, lane_links_per_move, phase_move_map, q),
    )
    p.start()
    p.join(timeout=config.exec_timeout_seconds)

    if p.is_alive():
        p.terminate()
        p.join()
        return ExecutionResult(
            success=False,
            phase_values=None,
            error_type='timeout',
            error_message=f'策略代码执行超时（>{config.exec_timeout_seconds}s）',
            violations=[],
        )

    if q.empty():
        return ExecutionResult(
            success=False,
            phase_values=None,
            error_type='runtime_error',
            error_message=f'子进程异常退出（exit code: {p.exitcode}）',
            violations=[],
        )

    status, result = q.get_nowait()
    if status == 'error':
        return ExecutionResult(
            success=False,
            phase_values=None,
            error_type='runtime_error',
            error_message=result,
            violations=[],
        )

    # Step 3: 安全约束检查（clamp + 违规记录）
    constraints = SafetyConstraints.from_config(config)
    clamped_values, violations = apply_constraints(result, constraints)

    return ExecutionResult(
        success=True,
        phase_values=clamped_values,
        error_type=None,
        error_message=None,
        violations=violations,
    )
