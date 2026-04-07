"""evoprog.executor: 策略代码执行子包公共接口。"""

from evoprog.executor.sandbox import (
    validate_code,
    ValidationError,
)
from evoprog.executor.runner import (
    ExecutionResult,
    execute_strategy,
    compute_phase_values,
)
from evoprog.executor.constraints import (
    SafetyConstraints,
    Violation,
    apply_constraints,
)

__all__ = [
    'validate_code',
    'ValidationError',
    'ExecutionResult',
    'execute_strategy',
    'compute_phase_values',
    'SafetyConstraints',
    'Violation',
    'apply_constraints',
]
