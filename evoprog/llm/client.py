"""LLM 代码生成客户端：openai 库封装 + JSON Schema 结构化输出 + 指数退避重试 + 降级策略。"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import openai

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    """LLM 策略代码生成结果。

    Attributes:
        success: 是否成功生成代码
        inlane_code: 内车道策略代码片段
        outlane_code: 外车道策略代码片段
        error: 错误信息（失败时）
        attempts: 实际调用次数
    """

    success: bool
    inlane_code: str = ""
    outlane_code: str = ""
    error: Optional[str] = None
    attempts: int = 0


class StrategyLLMClient:
    """策略代码 LLM 生成客户端。

    通过 openai 库连接 LM Studio 本地端点，支持 JSON Schema 结构化输出和指数退避重试。

    特性：
    - 优先使用 JSON Schema 结构化输出约束返回格式
    - 模型不支持结构化输出时（BadRequestError）自动降级为普通调用
    - APIError/APITimeoutError/APIConnectionError 时指数退避重试（最多 max_retries 次）
    - 所有失败返回 LLMResult(success=False) 而非抛出异常
    """

    STRATEGY_SCHEMA: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": "strategy_output",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "inlane_code": {"type": "string"},
                    "outlane_code": {"type": "string"},
                },
                "required": ["inlane_code", "outlane_code"],
                "additionalProperties": False,
            },
        },
    }

    def __init__(
        self,
        base_url: str = "http://localhost:1234/v1",
        api_key: str = "not-needed",
        timeout_seconds: float = 120.0,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        model: str = "local-model",
        temperature: float = 0.8,
        max_tokens: int = 8192,
    ):
        """初始化 LLM 客户端。

        Args:
            base_url: API 基础 URL（本地 LM Studio 或远程 API）
            api_key: API 密钥（本地推理可设为 "not-needed"）
            timeout_seconds: 单次 API 调用超时时间（秒）
            max_retries: 最大重试次数（含首次调用最多调用 max_retries 次）
            retry_base_delay: 指数退避基础延迟（秒），实际延迟 = base * 2^attempt
            model: 模型名称
            temperature: 生成温度（越高越多样，默认 0.8）
        """
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = openai.OpenAI(base_url=base_url, api_key=api_key)

    def generate(self, system_prompt: str, user_prompt: str, temperature: Optional[float] = None) -> LLMResult:
        """生成策略代码。

        优先使用结构化输出（JSON Schema），失败时降级为普通调用。
        API 错误时指数退避重试，最终失败返回 LLMResult(success=False)。

        Args:
            system_prompt: 系统角色定义 prompt
            user_prompt: 包含当前代码、指标、进化方向的用户 prompt

        Returns:
            LLMResult：成功时包含 inlane_code 和 outlane_code，失败时包含错误信息
        """
        temp = temperature if temperature is not None else self.temperature

        # 1. 先尝试带结构化输出的调用
        result = self._call_with_structured_output(system_prompt, user_prompt, temp)
        if result.success:
            return result

        # 2. 结构化输出失败（BadRequest/超时/其他）→ 降级为普通调用
        logger.warning(
            "结构化输出失败（%s），降级为普通调用。模型：%s",
            result.error[:80] if result.error else "unknown",
            self.model,
        )
        return self._call_regular(system_prompt, user_prompt, temp)

    def _call_with_structured_output(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.8
    ) -> LLMResult:
        """尝试带 JSON Schema 结构化输出的调用。

        Returns:
            LLMResult：成功时 success=True；
                       BadRequestError 时 success=False, error 含 "bad_request_fallback" 标记；
                       其他失败重试后仍失败时 success=False
        """
        last_error: Optional[str] = None
        attempts = 0

        total_calls = max(1, self.max_retries)
        for attempt in range(total_calls):
            attempts += 1
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    timeout=self.timeout_seconds,
                    temperature=temperature,
                    max_tokens=self.max_tokens,
                    response_format=self.STRATEGY_SCHEMA,
                )
                return self._parse_response(response, attempts)

            except openai.BadRequestError as e:
                # 模型不支持结构化输出，不重试，标记为降级
                logger.warning("结构化输出不支持（BadRequestError）：%s", str(e))
                return LLMResult(
                    success=False,
                    error=f"bad_request_fallback: {str(e)}",
                    attempts=attempts,
                )

            except openai.APITimeoutError as e:
                last_error = f"API timeout after {self.timeout_seconds}s: {str(e)}"
                logger.warning(
                    "结构化输出 API 超时（第 %d/%d 次）：%s",
                    attempts, total_calls, str(e)
                )

            except openai.APIConnectionError as e:
                last_error = f"API connection error: {str(e)}"
                logger.warning(
                    "结构化输出连接错误（第 %d/%d 次）：%s",
                    attempts, total_calls, str(e)
                )

            except openai.APIError as e:
                last_error = f"API error: {str(e)}"
                logger.warning(
                    "结构化输出 API 错误（第 %d/%d 次）：%s",
                    attempts, total_calls, str(e)
                )

            # 指数退避（最后一次不 sleep）
            if attempt < total_calls - 1:
                delay = self.retry_base_delay * (2 ** attempt)
                logger.info("等待 %.1fs 后重试...", delay)
                time.sleep(delay)

        return LLMResult(
            success=False,
            error=last_error or "unknown error",
            attempts=attempts,
        )

    def _call_regular(self, system_prompt: str, user_prompt: str, temperature: float = 0.8) -> LLMResult:
        """普通调用（不带 response_format），手动 json.loads 解析响应。"""
        last_error: Optional[str] = None
        attempts = 0

        total_calls = max(1, self.max_retries)
        for attempt in range(total_calls):
            attempts += 1
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    timeout=self.timeout_seconds,
                    temperature=temperature,
                    max_tokens=self.max_tokens,
                )
                return self._parse_response(response, attempts)

            except openai.APITimeoutError as e:
                last_error = f"API timeout after {self.timeout_seconds}s: {str(e)}"
                logger.warning(
                    "API 超时（第 %d/%d 次）：%s",
                    attempts, total_calls, str(e)
                )

            except openai.APIConnectionError as e:
                last_error = f"API connection error: {str(e)}"
                logger.warning(
                    "API 连接错误（第 %d/%d 次）：%s",
                    attempts, total_calls, str(e)
                )

            except openai.APIError as e:
                last_error = f"API error: {str(e)}"
                logger.warning(
                    "API 错误（第 %d/%d 次）：%s",
                    attempts, total_calls, str(e)
                )

            # 指数退避
            if attempt < total_calls - 1:
                delay = self.retry_base_delay * (2 ** attempt)
                logger.info("等待 %.1fs 后重试...", delay)
                time.sleep(delay)

        return LLMResult(
            success=False,
            error=last_error or "unknown error",
            attempts=attempts,
        )

    def _parse_response(self, response: Any, attempts: int) -> LLMResult:
        """从 openai 响应中提取并解析 JSON 内容。

        Args:
            response: openai ChatCompletion 响应对象
            attempts: 当前已尝试次数（用于 LLMResult.attempts）

        Returns:
            LLMResult：解析成功时 success=True，JSON 错误时 success=False
        """
        content = ""
        if response.choices and len(response.choices) > 0:
            choice = response.choices[0]
            if choice.message and choice.message.content:
                content = choice.message.content

        try:
            data = self._parse_json_payload(content)
            inlane_code = data.get("inlane_code", "")
            outlane_code = data.get("outlane_code", "")
            logger.debug("LLM 响应解析成功（第 %d 次调用）", attempts)
            return LLMResult(
                success=True,
                inlane_code=inlane_code,
                outlane_code=outlane_code,
                attempts=attempts,
            )
        except json.JSONDecodeError as e:
            error_msg = f"JSON parse error: {str(e)}. Content: {content[:100]}"
            logger.warning("响应 JSON 解析失败：%s", error_msg)
            return LLMResult(
                success=False,
                error=error_msg,
                attempts=attempts,
            )

    @staticmethod
    def _parse_json_payload(content: str) -> dict[str, Any]:
        """从模型返回文本中尽量稳健地提取 JSON payload。

        一些 OpenAI-compatible 服务会返回:
        - ```json fenced blocks
        - 带前后说明文字的 JSON
        - 带 `<think>...</think>` 前缀的内容
        """
        stripped = content.strip()
        candidates: list[str] = [stripped]

        if "</think>" in stripped:
            after_think = stripped.split("</think>", 1)[1].strip()
            if after_think:
                candidates.append(after_think)

        for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL):
            fenced = match.group(1).strip()
            if fenced:
                candidates.append(fenced)

        seen: set[str] = set()
        decoder = json.JSONDecoder()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)

            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

            for match in re.finditer(r"\{", candidate):
                try:
                    parsed, _ = decoder.raw_decode(candidate[match.start():])
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue

        raise json.JSONDecodeError("No valid JSON object found", content, 0)
