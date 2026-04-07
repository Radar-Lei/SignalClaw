"""evoprog.evolution 子包：进化信号提取 + 种群管理 + 诊断指标。"""

from evoprog.evolution.signals import (
    EvolutionSignals,
    extract_signals,
    signals_to_direction,
)
from evoprog.evolution.population import (
    generate_next_population,
    create_seed_population,
    SEED_INLANE_CODE,
    SEED_OUTLANE_CODE,
)
from evoprog.evolution.diagnostics import (
    compute_cmr,
    compute_ancestry_concentration,
    compute_fitness_diversity,
)

__all__ = [
    "EvolutionSignals",
    "extract_signals",
    "signals_to_direction",
    "generate_next_population",
    "create_seed_population",
    "SEED_INLANE_CODE",
    "SEED_OUTLANE_CODE",
    "compute_cmr",
    "compute_ancestry_concentration",
    "compute_fitness_diversity",
]
