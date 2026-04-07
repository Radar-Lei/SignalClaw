"""种群管理：精英复制 + LLM 生成 + 种子策略。

实现进化循环的种群生命周期：
  - 第 0 代：create_seed_population 生成 PI-Light 基线种群
  - 后续代：generate_next_population 实现精英复制 + LLM 生成变体
"""

import logging
import random
from typing import Optional

from evoprog.evolution.signals import EvolutionSignals, signals_to_direction
from evoprog.executor.sandbox import validate_code
from evoprog.llm.client import StrategyLLMClient
from evoprog.llm.prompt import SYSTEM_PROMPT, build_user_prompt, get_system_prompt, get_event_skill_prompt
from evoprog.store.asset_store import AssetStore
from evoprog.store.content_hash import compute_gene_id
from evoprog.store.models import Capsule, Gene

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PI-Light 论文基线策略代码（Figure 2）
# ---------------------------------------------------------------------------

# 相位选择基线策略：MaxPressure-inspired 压力差（上游等待 - 下游等待）
SEED_INLANE_CODE = "value[0] += inlane_2_num_waiting_vehicle - outlane_2_num_waiting_vehicle"
SEED_OUTLANE_CODE = ""

# 相位延长种子：综合各相位排队状况，当前绿灯时间越长越应切换
SEED_EXTENSION_INLANE_CODE = "value[0] += inlane_2_num_waiting_vehicle - current_green_time * 0.1"
SEED_EXTENSION_OUTLANE_CODE = "value[0] -= outlane_2_num_vehicle"

# 周期规划种子：按到达车辆数比例分配
SEED_CYCLE_INLANE_CODE = "value[0] += inlane_2_num_vehicle + inlane_2_num_waiting_vehicle"
SEED_CYCLE_OUTLANE_CODE = "value[0] += outlane_2_num_vehicle"


def _get_seed_code(control_mode: str) -> tuple[str, str]:
    """根据控制模式返回对应的种子代码。

    Args:
        control_mode: 控制模式名称

    Returns:
        (inlane_code, outlane_code) 元组
    """
    if control_mode == "phase_extension":
        return SEED_EXTENSION_INLANE_CODE, SEED_EXTENSION_OUTLANE_CODE
    elif control_mode == "cycle_planning":
        return SEED_CYCLE_INLANE_CODE, SEED_CYCLE_OUTLANE_CODE
    else:
        # 默认为 phase_selection
        return SEED_INLANE_CODE, SEED_OUTLANE_CODE


def create_seed_population(
    pop_size: int,
    store: AssetStore,
    control_mode: str = "phase_selection",
) -> list[Gene]:
    """生成初代种群（按控制模式选择种子策略）。

    所有种子使用相同的种子代码，因此所有种子的 SHA-256 ID 相同。这是预期行为：
    第一代全部是基线策略，评估结果用于建立基线 fitness。

    Args:
        pop_size: 种群大小
        store: AssetStore 实例，用于持久化和事件记录
        control_mode: 控制模式（默认 phase_selection）

    Returns:
        包含 pop_size 个 Gene 的列表（内容相同，ID 相同）
    """
    inlane, outlane = _get_seed_code(control_mode)
    gene_id = compute_gene_id(inlane, outlane)
    seed_gene = Gene(
        id=gene_id,
        inlane_code=inlane,
        outlane_code=outlane,
        control_mode=control_mode,
    )

    # 持久化（upsert，重复 ID 会被更新）
    store.upsert_gene(seed_gene)

    # 为每个种群成员记录生成事件
    population: list[Gene] = []
    for _ in range(pop_size):
        store.log_generated(gene_id, generation=0)
        population.append(seed_gene)

    return population


def generate_next_population(
    current_elite: list[Gene],
    capsules: list[Capsule],
    signals: EvolutionSignals,
    pop_size: int,
    elite_count: int,
    llm_client: StrategyLLMClient,
    store: AssetStore,
    generation: int,
    metrics: dict,
    control_mode: str = "phase_selection",
    target_event_type: str = "",
) -> list[Gene]:
    """生成下一代种群：精英复制 + LLM 生成变体。

    实现流程：
    1. 精英复制：前 elite_count 个精英直接进入下一代
    2. 参考策略选择：优先最新 Capsule，否则取精英
    3. LLM 生成剩余名额（最多尝试 pop_size * 3 次）
    4. LLM 持续失败时用精英副本填充，保证种群不为空

    Args:
        current_elite: 当前代精英 Gene 列表（按 fitness 降序）
        capsules: 历史 Capsule 列表（按时间升序，最新在末尾）
        signals: 当代进化信号
        pop_size: 目标种群大小
        elite_count: 精英复制数量
        llm_client: LLM 客户端
        store: AssetStore 实例
        generation: 当前代号
        metrics: 当代指标字典（用于构建 LLM prompt）
        control_mode: 控制模式（默认 phase_selection），影响 system_prompt 和新 Gene.control_mode

    Returns:
        新一代种群 Gene 列表，长度 == pop_size
    """
    new_population: list[Gene] = []

    # 1. 精英复制
    for gene in current_elite[:elite_count]:
        new_population.append(gene)

    # 2. 构建参考策略池（多参考策略，打破单一参考的多样性瓶颈）
    reference_pool = _build_reference_pool(current_elite, capsules, store)

    # 3. 生成进化方向
    direction = signals_to_direction(signals)

    # 4. 获取对应模式的 system_prompt（事件进化时使用事件特化 prompt）
    if target_event_type and target_event_type != "normal":
        system_prompt = get_event_skill_prompt(target_event_type)
        logger.info("使用事件特化 prompt: %s", target_event_type)
    else:
        system_prompt = get_system_prompt(control_mode)

    # 5. force_innovation 时提高 temperature
    temperature = None  # 使用客户端默认值
    if signals.force_innovation:
        temperature = llm_client.temperature + 0.4
        logger.info("force_innovation 激活，提高 temperature 至 %.1f", temperature)

    # 6. LLM 生成剩余名额（多参考 + crossover）
    needed = pop_size - len(new_population)
    max_attempts = pop_size * 3
    attempts = 0
    seen_ids = {g.id for g in new_population}

    while len(new_population) < pop_size and attempts < max_attempts:
        attempts += 1

        # 从参考池中随机选择参考策略（多样性关键改进）
        reference_gene = random.choice(reference_pool)

        # 50% 概率使用 crossover（同时展示两个父代给 LLM）
        use_crossover = len(reference_pool) >= 2 and random.random() < 0.5
        if use_crossover:
            parent_b = random.choice([g for g in reference_pool if g.id != reference_gene.id] or reference_pool)
            crossover_hint = (
                f"\n\n## 参考策略 B（可选择性融合）\n\n"
                f"inlane_code：\n```python\n{parent_b.inlane_code}\n```\n\n"
                f"outlane_code：\n```python\n{parent_b.outlane_code}\n```\n\n"
                f"请结合两个策略的优点，生成一个融合策略。"
            )
        else:
            crossover_hint = ""

        user_prompt = build_user_prompt(
            inlane_code=reference_gene.inlane_code,
            outlane_code=reference_gene.outlane_code,
            metrics=metrics,
            direction=direction + crossover_hint,
        )
        result = llm_client.generate(system_prompt, user_prompt, temperature=temperature)

        if result.success:
            # AST 验证：拒绝含非法变量名或语法的策略，避免浪费 SUMO 评估
            ast_errors = []
            if result.inlane_code.strip():
                ast_errors.extend(validate_code(result.inlane_code))
            if result.outlane_code.strip():
                ast_errors.extend(validate_code(result.outlane_code))
            if ast_errors:
                logger.warning(
                    "LLM 生成的代码未通过 AST 验证（第 %d/%d 次）：%s",
                    attempts, max_attempts, ast_errors[0],
                )
                continue

            gene_id = compute_gene_id(result.inlane_code, result.outlane_code)

            # 去重：跳过已在种群中的策略
            if gene_id in seen_ids:
                logger.info(
                    "LLM 生成了重复策略（第 %d/%d 次），跳过: %s",
                    attempts, max_attempts, gene_id[:16],
                )
                continue

            seen_ids.add(gene_id)
            new_gene = Gene(
                id=gene_id,
                inlane_code=result.inlane_code,
                outlane_code=result.outlane_code,
                parent_id=reference_gene.id,
                control_mode=control_mode,
            )
            store.upsert_gene(new_gene)
            store.log_generated(gene_id, generation=generation)
            new_population.append(new_gene)
        else:
            logger.warning(
                "LLM 生成失败（第 %d/%d 次尝试）：%s",
                attempts, max_attempts, result.error
            )

    # 7. 安全填充：LLM 持续失败时用精英策略副本填充
    if len(new_population) < pop_size:
        fill_gene = reference_pool[0] if reference_pool else current_elite[0]
        shortfall = pop_size - len(new_population)
        logger.warning(
            "LLM 生成不足，用精英策略副本填充 %d 个空位。", shortfall
        )
        for _ in range(shortfall):
            new_population.append(fill_gene)

    return new_population


def _build_reference_pool(
    current_elite: list[Gene],
    capsules: list[Capsule],
    store: AssetStore,
    max_pool_size: int = 4,
) -> list[Gene]:
    """构建参考策略池：从精英和历史 Capsule 中选择多个参考策略。

    多参考策略打破了"只从 top-1 变异"的多样性瓶颈。
    LLM 每次从池中随机选择一个参考，产生更多样的变体。

    Args:
        current_elite: 精英 Gene 列表（按 fitness 降序）
        capsules: 历史 Capsule 列表（按时间升序）
        store: AssetStore 实例
        max_pool_size: 参考池最大大小

    Returns:
        参考 Gene 列表（至少包含 1 个）
    """
    pool: list[Gene] = []
    seen_ids: set[str] = set()

    # 1. 从精英中选择 top-K
    for gene in current_elite[:max_pool_size]:
        if gene.id not in seen_ids:
            pool.append(gene)
            seen_ids.add(gene.id)

    # 2. 从最近的 Capsule 中补充（可能包含过去代的好策略）
    if capsules:
        genes = store.read_genes()
        genes_by_id = {g.id: g for g in genes}
        for capsule in reversed(capsules[-max_pool_size:]):
            if len(pool) >= max_pool_size:
                break
            capsule_gene = genes_by_id.get(capsule.gene_id)
            if capsule_gene is not None and capsule_gene.id not in seen_ids:
                pool.append(capsule_gene)
                seen_ids.add(capsule_gene.id)

    # 3. 回退：至少有种子策略
    if not pool:
        logger.warning("无可用参考策略，使用种子策略。")
        pool.append(Gene(
            id=compute_gene_id(SEED_INLANE_CODE, SEED_OUTLANE_CODE),
            inlane_code=SEED_INLANE_CODE,
            outlane_code=SEED_OUTLANE_CODE,
            control_mode="phase_selection",
        ))

    return pool
