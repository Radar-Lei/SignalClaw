"""TOML 配置加载器：DaemonConfig dataclass + argparse CLI 覆盖。

兼容 Python 3.11+ 内置 tomllib 和旧版本的 tomli 三方包。
"""

import argparse
import sys
from dataclasses import dataclass, field
from typing import Optional

# TOML 解析兼容层：Python 3.11+ 使用内置 tomllib，旧版尝试 tomli
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

import os
from pathlib import Path

from evoprog.config import EvaluatorConfig


def _load_dotenv():
    """Load .env file from project root if it exists."""
    for candidate in [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]:
        if candidate.exists():
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ.setdefault(key.strip(), value.strip())
            break


_load_dotenv()


@dataclass
class LLMConfig:
    """LLM 客户端配置。

    优先级: TOML 配置 > 环境变量 > 默认值。
    支持的环境变量: OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = 120.0
    max_retries: int = 3
    temperature: float = 0.8

    def __post_init__(self):
        if not self.base_url:
            self.base_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:1234/v1")
        if not self.api_key:
            self.api_key = os.environ.get("OPENAI_API_KEY", "not-needed")
        if not self.model:
            self.model = os.environ.get("OPENAI_MODEL", "local-model")


@dataclass
class FixedSkillConfig:
    """固定技能配置（用于 dispatcher_context 模式）。"""
    inlane_code: str = ""
    outlane_code: str = ""


@dataclass
class EvolutionConfig:
    """进化循环配置。"""

    pop_size: int = 4
    generations: int = 5
    stagnation_threshold: int = 3
    elite_count: int = 1
    # Dispatcher-context evolution: evolve one event skill within the full dispatcher
    dispatcher_context: bool = False
    target_event_type: str = ""  # e.g., "transit", "incident"


@dataclass
class StoreConfig:
    """资产存储配置。"""

    store_dir: str = "store"


@dataclass
class DaemonConfig:
    """守护进程配置聚合：包含 llm/evolution/evaluator/store 四段。"""

    llm: LLMConfig
    evolution: EvolutionConfig
    store: StoreConfig
    scenario_dirs: list[str] = field(default_factory=list)
    evaluator: EvaluatorConfig = field(default_factory=EvaluatorConfig)
    config_path: str = "evoprog.toml"
    control_modes: list[str] = field(default_factory=lambda: ["phase_selection"])
    # Fixed skills for dispatcher-context evolution
    fixed_skills: dict[str, FixedSkillConfig] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    """解析命令行参数（可覆盖 TOML 配置值）。

    Returns:
        argparse.Namespace：解析后的参数
    """
    parser = argparse.ArgumentParser(
        description="EvoProgTSC 进化闭环守护进程"
    )
    parser.add_argument(
        "--config",
        default="evoprog.toml",
        help="TOML 配置文件路径（默认：evoprog.toml）",
    )
    parser.add_argument(
        "--generations",
        type=int,
        default=None,
        help="覆盖 evolution.generations",
    )
    parser.add_argument(
        "--pop-size",
        dest="pop_size",
        type=int,
        default=None,
        help="覆盖 evolution.pop_size",
    )
    parser.add_argument(
        "--store-dir",
        dest="store_dir",
        default=None,
        help="覆盖 store.store_dir",
    )
    parser.add_argument(
        "--scenario-dir",
        dest="scenario_dir",
        action="append",
        default=[],
        help="追加场景路径（可重复）",
    )
    return parser.parse_args()


def _load_toml(config_path: str) -> dict:
    """尝试加载 TOML 配置文件，文件不存在时返回空字典。"""
    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def load_config(args: argparse.Namespace) -> DaemonConfig:
    """从 TOML 文件加载配置并用 CLI 参数覆盖。

    优先级：CLI 参数 > TOML 文件 > 默认值

    Args:
        args: argparse.Namespace，来自 parse_args() 或测试构造

    Returns:
        DaemonConfig：合并后的完整配置
    """
    raw = _load_toml(args.config)

    # 1. 从 TOML 构建各段 Config（利用字典解包）
    # LLMConfig: TOML values override env vars (which override defaults)
    llm_raw = raw.get("llm", {})
    llm_cfg = LLMConfig(
        base_url=llm_raw.get("base_url", ""),
        api_key=llm_raw.get("api_key", ""),
        model=llm_raw.get("model", ""),
        timeout_seconds=llm_raw.get("timeout_seconds", 120.0),
        max_retries=llm_raw.get("max_retries", 3),
        temperature=llm_raw.get("temperature", 0.8),
    )

    _evo_defaults = EvolutionConfig()
    evo_raw = raw.get("evolution", {})
    evo_cfg = EvolutionConfig(
        pop_size=evo_raw.get("pop_size", _evo_defaults.pop_size),
        generations=evo_raw.get("generations", _evo_defaults.generations),
        stagnation_threshold=evo_raw.get(
            "stagnation_threshold", _evo_defaults.stagnation_threshold
        ),
        elite_count=evo_raw.get("elite_count", _evo_defaults.elite_count),
        dispatcher_context=evo_raw.get("dispatcher_context", False),
        target_event_type=evo_raw.get("target_event_type", ""),
    )

    eval_raw = raw.get("evaluator", {})
    _eval_defaults = EvaluatorConfig()
    eval_cfg = EvaluatorConfig(
        sumo_home=eval_raw.get("sumo_home", _eval_defaults.sumo_home),
        decision_step_interval=eval_raw.get(
            "decision_step_interval", _eval_defaults.decision_step_interval
        ),
        weight_delay=eval_raw.get("weight_delay", _eval_defaults.weight_delay),
        weight_queue=eval_raw.get("weight_queue", _eval_defaults.weight_queue),
        weight_throughput=eval_raw.get(
            "weight_throughput", _eval_defaults.weight_throughput
        ),
    )

    _store_defaults = StoreConfig()
    store_raw = raw.get("store", {})
    store_cfg = StoreConfig(
        store_dir=store_raw.get("store_dir", _store_defaults.store_dir),
    )

    # 场景目录：优先取根级 scenario_dirs，其次取 store 段下的 scenario_dirs
    # 支持两种写法：根级键（evoprog.toml 模板）或 [store] 段下的键
    toml_scenario_dirs: list[str] = (
        raw.get("scenario_dirs")
        or store_raw.get("scenario_dirs", [])
    )
    cli_scenario_dirs: list[str] = list(args.scenario_dir or [])
    scenario_dirs = list(toml_scenario_dirs) + [
        d for d in cli_scenario_dirs if d not in toml_scenario_dirs
    ]

    # 解析控制模式
    control_modes: list[str] = raw.get("control_modes", ["phase_selection"])

    # 合法组合白名单（CONTEXT.md 锁定决策）
    _VALID_COMBINATIONS = {
        frozenset(["phase_selection"]),
        frozenset(["phase_extension"]),
        frozenset(["cycle_planning"]),
        frozenset(["cycle_planning", "phase_extension"]),
    }
    if frozenset(control_modes) not in _VALID_COMBINATIONS:
        raise ValueError(
            f"非法控制模式组合: {control_modes}。"
            f"合法组合: 任意单模式, 或 ['cycle_planning', 'phase_extension']"
        )

    # 2. CLI 覆盖（仅在非 None 时覆盖）
    if args.generations is not None:
        evo_cfg.generations = args.generations
    if args.pop_size is not None:
        evo_cfg.pop_size = args.pop_size
    if args.store_dir is not None:
        store_cfg.store_dir = args.store_dir

    # Parse fixed_skills for dispatcher-context mode
    fixed_skills_raw = raw.get("fixed_skills", {})
    fixed_skills: dict[str, FixedSkillConfig] = {}
    for event_type, skill_raw in fixed_skills_raw.items():
        if isinstance(skill_raw, dict):
            fixed_skills[event_type] = FixedSkillConfig(
                inlane_code=skill_raw.get("inlane_code", ""),
                outlane_code=skill_raw.get("outlane_code", ""),
            )

    return DaemonConfig(
        llm=llm_cfg,
        evolution=evo_cfg,
        evaluator=eval_cfg,
        store=store_cfg,
        scenario_dirs=scenario_dirs,
        config_path=args.config,
        control_modes=control_modes,
        fixed_skills=fixed_skills,
    )
