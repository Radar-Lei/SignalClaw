"""evoprog 配置模块：安全约束参数与白名单定义。"""

import os
from dataclasses import dataclass, field


@dataclass
class ExecutorConfig:
    """策略执行器配置。"""
    min_green_seconds: float = 5.0
    max_green_seconds: float = 60.0
    all_red_seconds: float = 2.0
    exec_timeout_seconds: float = 2.0
    log_rotation_max_bytes: int = 104857600  # 100MB
    log_rotation_max_lines: int = 10000


@dataclass
class EvaluatorConfig:
    """SUMO 评估器配置（与 ExecutorConfig 独立）。"""
    sumo_home: str = field(
        default_factory=lambda: os.environ.get("SUMO_HOME", "/usr/share/sumo")
    )
    decision_step_interval: int = 1   # 每 N 步调用一次 control_fn
    weight_delay: float = 0.8         # delay-dominant: 常规信控核心指标
    weight_queue: float = 0.15
    weight_throughput: float = 0.05


# 策略代码允许访问的所有名称（白名单方式）
ALLOWED_NAMES: frozenset = frozenset({
    # 6 个交通特征变量（由执行框架注入）
    'inlane_2_num_vehicle',
    'outlane_2_num_vehicle',
    'inlane_2_num_waiting_vehicle',
    'outlane_2_num_waiting_vehicle',     # 出车道等待车辆（下游背压）
    'inlane_2_vehicle_dist',
    'outlane_2_vehicle_dist',
    # 相位延长模式额外变量（仅 phase_extension 模式注入）
    'current_green_time',
    # 事件上下文变量（EventClaw 事件驱动技能进化）
    'event_emergency_distance',
    'event_emergency_phase',
    'event_emergency_count',
    'event_bus_count',
    'event_bus_distance',
    'event_bus_phase',
    'event_incident_blocked',
    'event_congestion_level',
    # 执行框架注入的上下文变量
    'value',
    'index',
    # 允许的内置函数
    'min',
    'max',
    'abs',
    'sum',
    'len',
    'range',
    # True/False/None 在 Python 3 的 AST 中是 Constant 节点，不是 Name 节点
    # 但为兼容性保留（某些场景下可能出现为 Name 节点）
})

# exec() 受限命名空间：只暴露安全的内置函数
# 注意：__builtins__ 设为白名单 dict 而非 None 或 {}，避免 Pitfall 1
SAFE_BUILTINS: dict = {
    '__builtins__': {
        'min': min,
        'max': max,
        'abs': abs,
        'sum': sum,
        'len': len,
        'range': range,
        'True': True,
        'False': False,
        'None': None,
    }
}
