"""
evoprog.executor.constraints: 安全约束检查——clamp + 违规记录。

对策略代码输出的 phase_values 强制执行安全约束：
- 低于 min_green 时 clamp 到 min_green
- 高于 max_green 时 clamp 到 max_green
- 违规时记录结构化 Violation 对象
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evoprog.config import ExecutorConfig


@dataclass
class Violation:
    """安全约束违规记录。"""
    phase_id: int
    original_value: float
    clamped_value: float
    constraint_name: str  # 'min_green' | 'max_green'
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class SafetyConstraints:
    """安全约束参数。"""
    min_green: float
    max_green: float
    all_red: float

    @classmethod
    def from_config(cls, config: 'ExecutorConfig') -> 'SafetyConstraints':
        """从 ExecutorConfig 构造 SafetyConstraints。"""
        return cls(
            min_green=config.min_green_seconds,
            max_green=config.max_green_seconds,
            all_red=config.all_red_seconds,
        )


def apply_constraints(
    phase_values: list[float],
    constraints: SafetyConstraints,
) -> tuple[list[float], list[Violation]]:
    """
    对 phase_values 应用安全约束（clamp 到 [min_green, max_green]）。

    Returns:
        (clamped_values, violations) 元组：
        - clamped_values: 每个 phase 的 clamp 后值
        - violations: 所有违规记录列表
    """
    clamped_values: list[float] = []
    violations: list[Violation] = []

    for phase_id, value in enumerate(phase_values):
        original = float(value)
        if original < constraints.min_green:
            clamped = constraints.min_green
            violations.append(Violation(
                phase_id=phase_id,
                original_value=original,
                clamped_value=clamped,
                constraint_name='min_green',
            ))
            clamped_values.append(clamped)
        elif original > constraints.max_green:
            clamped = constraints.max_green
            violations.append(Violation(
                phase_id=phase_id,
                original_value=original,
                clamped_value=clamped,
                constraint_name='max_green',
            ))
            clamped_values.append(clamped)
        else:
            clamped_values.append(original)

    return clamped_values, violations
