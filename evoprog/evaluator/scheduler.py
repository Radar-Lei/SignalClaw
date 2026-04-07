"""
evoprog.evaluator.scheduler: 多场景并行评估调度器

提供：
- evaluate_one_scenario: 在子进程中安全创建/销毁 SumoEvaluator，评估单场景
- evaluate_strategy_multi_scenario: 使用 ProcessPoolExecutor 并行评估多场景，返回原始结果

注意事项：
- evaluate_one_scenario 必须为顶层函数（ProcessPoolExecutor pickle 要求）
- control_fn_factory 在子进程中调用，而非传递 closure（pickle 限制）
- 每个子进程独立通过 sys.path.insert 加载 traci
- 随机端口 random.randint(10000, 60000) 避免冲突
"""
from __future__ import annotations

import glob
import os
import random
from concurrent.futures import ProcessPoolExecutor
from typing import Callable, Optional

from evoprog.config import EvaluatorConfig
from evoprog.evaluator.runner import EvaluationResult, SumoEvaluator


def _resolve_sumocfg_path(path: str) -> Optional[str]:
    """
    解析场景路径，支持目录或 .sumocfg 文件。

    Args:
        path: 场景目录路径或 .sumocfg 文件路径

    Returns:
        解析后的 .sumocfg 文件绝对路径，找不到时返回 None
    """
    # 如果路径以 .sumocfg 结尾，直接返回（让下游处理文件是否存在）
    if path.endswith(".sumocfg"):
        return os.path.abspath(path)

    # 如果是目录，查找目录下的 .sumocfg 文件
    if os.path.isdir(path):
        sumocfg_files = glob.glob(os.path.join(path, "*.sumocfg"))
        if sumocfg_files:
            # 优先选择与目录名同名的 .sumocfg，否则选第一个
            dir_name = os.path.basename(path)
            for f in sumocfg_files:
                if os.path.basename(f) == f"{dir_name}.sumocfg":
                    return os.path.abspath(f)
            return os.path.abspath(sumocfg_files[0])

    # 路径不存在或找不到 .sumocfg 文件
    return None


def _get_free_port() -> int:
    """获取 OS 分配的空闲端口，避免冲突。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def evaluate_one_scenario(args: tuple) -> Optional[EvaluationResult]:
    """
    在子进程中评估单个场景（顶层函数，满足 ProcessPoolExecutor pickle 要求）。

    子进程启动时显式插入 SUMO_HOME/tools 到 sys.path，确保 traci 可用。
    失败时返回 None，不传播异常（场景容错）。
    含重试逻辑：端口冲突或 SUMO 启动失败时最多重试 3 次。

    Args:
        args: (sumocfg_path, control_fn_factory, evaluator_config) 元组

    Returns:
        EvaluationResult（成功）或 None（失败）
    """
    import sys
    import os
    import time

    sumocfg_path, control_fn_factory, evaluator_config = args

    # 子进程中显式设置 SUMO_HOME/tools 路径（Pitfall 6: 每个子进程独立导入）
    sumo_home = evaluator_config.sumo_home
    sumo_tools_path = os.path.join(sumo_home, "tools")
    if sumo_tools_path not in sys.path:
        sys.path.insert(0, sumo_tools_path)

    max_retries = 3
    for attempt in range(max_retries):
        port = _get_free_port()
        label = f"eval_{port}_{os.getpid()}"
        try:
            with SumoEvaluator(sumocfg_path, port, label, evaluator_config) as sim:
                control_fn = control_fn_factory()
                return sim.run(control_fn)
        except OSError:
            # 端口冲突，等待后重试
            time.sleep(0.5 * (attempt + 1))
            continue
        except Exception:
            # 其他错误，安全返回 None
            return None

    return None


def evaluate_strategy_multi_scenario(
    scenario_paths: list[str],
    control_fn_factory: Callable,
    evaluator_config: EvaluatorConfig,
    max_workers: Optional[int] = None,
) -> list[Optional[EvaluationResult]]:
    """
    并行评估一个策略在多个场景下的表现，返回各场景原始结果。

    并行度默认等于场景数（可通过 max_workers 配置）。
    某场景失败时返回 None。fitness 计算由调用方（daemon）在收集所有策略
    结果后跨策略批量归一化完成。

    Args:
        scenario_paths: 场景目录路径或 .sumocfg 文件路径列表
        control_fn_factory: 控制函数工厂（子进程中调用，产生 control_fn）
        evaluator_config: EvaluatorConfig 实例
        max_workers: 并行 worker 数（默认等于场景数）

    Returns:
        各场景 EvaluationResult 或 None（失败）的列表
    """
    # 解析场景路径：目录 -> .sumocfg 文件
    resolved_paths = []
    for path in scenario_paths:
        resolved = _resolve_sumocfg_path(path)
        if resolved:
            resolved_paths.append(resolved)
        else:
            print(f"[Scheduler] 警告: 无法解析场景路径 '{path}'，跳过")

    if not resolved_paths:
        print("[Scheduler] 错误: 没有有效的场景路径")
        return []

    args_list = [
        (path, control_fn_factory, evaluator_config)
        for path in resolved_paths
    ]

    workers = max_workers if max_workers is not None else len(resolved_paths)

    with ProcessPoolExecutor(max_workers=workers) as executor:
        results_list = list(executor.map(evaluate_one_scenario, args_list))

    return results_list
