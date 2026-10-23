"""大模型调用层：任何 OpenAI 兼容的 /chat/completions 服务都能接。

默认接 DeepSeek；换模型只改 config.yaml 里的 base_url / model。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from .config import LLMConfig


class LLMError(Exception):
    """调用大模型失败，user_message 是给 QQ 里用户看的友好提示。"""

    def __init__(self, message: str, user_message: str = "我这会儿有点懵，稍后再试一下？") -> None:
        super().__init__(message)
        self.user_message = user_message


class LLMClient:
    def __init__(self, cfg: LLMConfig, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.logger = logger or logging.getLogger("qqbot.llm")
        self._url = self._build_url(cfg.base_url)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout, connect=15.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_calls = 0

    @staticmethod
    def _build_url(base_url: str) -> str:
        base = base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    async def chat(self, messages: list[dict[str, str]]) -> str:
        """发一轮对话，返回模型文本。失败会重试可重试的错误。"""
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

        last_error: LLMError | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(self._url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = LLMError(
                    f"请求超时（{self.cfg.timeout:g}s）: {exc}",
                    "想太久了，网络或者模型那边有点慢，再发一次试试？",
                )
            except httpx.HTTPError as exc:
                last_error = LLMError(
                    f"网络错误: {exc}",
                    "我这边网络断了，稍等再试～",
                )
            else:
                if resp.status_code == 200:
                    return self._parse(resp)
                last_error = self._http_error(resp)
                # 4xx（除 429）重试没用，直接失败
                if resp.status_code != 429 and resp.status_code < 500:
                    raise last_error

            if attempt < 2:
                delay = 1.5 * (2 ** attempt) + random.uniform(0, 0.4)
                self.logger.warning("模型调用失败（第 %d 次）：%s；%.1fs 后重试", attempt + 1, last_error, delay)
                await asyncio.sleep(delay)

        raise last_error or LLMError("模型调用失败")

    def _parse(self, resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"响应不是 JSON: {resp.text[:200]}", "模型返回了奇怪的东西，再试一次？") from exc

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"响应里没有 choices: {str(data)[:300]}", "模型没给出回复，再试一次？")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 少数服务返回分片
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        text = str(content or "").strip()

        usage = data.get("usage") or {}
        self.total_calls += 1
        self.total_prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.total_completion_tokens += int(usage.get("completion_tokens") or 0)

        if not text:
            finish = choices[0].get("finish_reason")
            if finish == "content_filter":
                raise LLMError("内容被模型过滤", "这个话题我不太好聊，换一个吧。")
            raise LLMError(f"模型返回空内容（finish_reason={finish}）", "我刚刚脑子空了，再说一遍？")
        return text

    def _http_error(self, resp: httpx.Response) -> LLMError:
        detail = resp.text[:300].replace("\n", " ")
        code = resp.status_code
        if code in (401, 403):
            return LLMError(f"鉴权失败 {code}: {detail}", "我的 API Key 好像不对，管理员看下日志。")
        if code == 402:
            return LLMError(f"余额不足 {code}: {detail}", "钱包空了…管理员该充值了。")
        if code == 404:
            return LLMError(
                f"接口不存在 {code}: {detail}",
                "模型地址配错了，管理员看下日志。",
            )
        if code == 429:
            return LLMError(f"被限流 {code}: {detail}", "问得太快啦，等几秒再问我。")
        if code >= 500:
            return LLMError(f"服务端错误 {code}: {detail}", "模型服务抽风了，稍后再试。")
        return LLMError(f"HTTP {code}: {detail}", "刚刚出错了，再试一次？")

    async def close(self) -> None:
        await self._client.aclose()

    def usage_summary(self) -> str:
        return (
            f"调用 {self.total_calls} 次，"
            f"输入 {self.total_prompt_tokens} tokens / 输出 {self.total_completion_tokens} tokens"
        )
