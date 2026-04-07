"""evoprog.llm — LLM 代码生成子包。

提供 StrategyLLMClient（openai 封装 + 重试 + 降级）和 PromptBuilder（System+User 分离）。
"""

from evoprog.llm.client import LLMResult, StrategyLLMClient
from evoprog.llm.prompt import SYSTEM_PROMPT, build_user_prompt

__all__ = [
    "LLMResult",
    "StrategyLLMClient",
    "SYSTEM_PROMPT",
    "build_user_prompt",
]
