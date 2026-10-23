"""图片 / 表情包理解。

思路上把「看」和「聊」拆开：
  图 → 多模态模型描述成一句话 → 塞进主模型的上下文 → DeepSeek 接着聊
这样聊天风格、人设、记忆都不受影响，只多了个"眼睛"。

图片拿不到（下载失败、模型报错、太大）时安静降级成 [图片]，不会让整条消息失败。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from .config import LLMConfig, VisionConfig
from .events import Segment
from .llm import LLMClient, LLMError

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# 画质太低会看不清表情包上的字，太高又浪费带宽
_MAX_DESCRIPTION_CHARS = 240


def guess_mime(data: bytes) -> str:
    """按文件头猜 MIME，比信任 URL 后缀靠谱。"""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def is_sticker(segment: Segment) -> bool:
    """判断是不是表情包：商城表情（mface）或带 emoji_id 的 image。"""
    if segment.type == "mface":
        return True
    data = segment.data
    if data.get("emoji_id") or data.get("emoji_package_id") or data.get("key"):
        return True
    summary = str(data.get("summary") or "")
    return "表情" in summary


class ImageAnalyst:
    def __init__(
        self,
        cfg: VisionConfig,
        onebot: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cfg = cfg
        self.onebot = onebot
        self.logger = logger or logging.getLogger("qqbot.vision")
        self._llm = LLMClient(
            LLMConfig(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                model=cfg.model,
                temperature=0.2,
                max_tokens=400,
                timeout=cfg.timeout,
            ),
            logger=self.logger,
        )
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QQBot/1.0"},
        )

    # ------------------------------------------------------------------ #
    async def describe(self, segments: list[Segment]) -> list[str]:
        """返回每张图的描述；失败的位置是空字符串（调用方保留占位符）。"""
        results: list[str] = []
        for segment in segments[: self.cfg.max_images]:
            try:
                source = await self._resolve_source(segment)
                if not source:
                    self.logger.info("图片没有可用的地址，跳过")
                    results.append("")
                    continue
                data_url = await self._to_data_url(source)
                if not data_url:
                    results.append("")
                    continue
                results.append(await self._ask(data_url, segment))
            except LLMError as exc:
                self.logger.warning("图片理解失败：%s", exc)
                results.append("")
            except Exception as exc:  # noqa: BLE001 - 一张图失败不该影响聊天
                self.logger.warning("图片处理失败：%s", exc)
                results.append("")
        return results

    # ------------------------------------------------------------------ #
    async def _resolve_source(self, segment: Segment) -> str:
        """拿到 http(s) URL 或 base64:// 数据。"""
        data = segment.data
        for key in ("url", "file"):
            value = str(data.get(key) or "")
            if value.startswith(("http://", "https://", "base64://")):
                return value

        # 只给了文件名，问 NapCat 要真实地址
        file_id = str(data.get("file") or "")
        if not file_id or self.onebot is None:
            return ""
        try:
            info = await self.onebot.call_api("get_image", {"file": file_id}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("get_image 失败：%s", exc)
            return ""
        if not isinstance(info, dict):
            return ""
        for key in ("url", "file"):
            value = str(info.get(key) or "")
            if value.startswith(("http://", "https://", "base64://")):
                return value
        return ""

    async def _to_data_url(self, source: str) -> str:
        """统一转成 data: URL —— 不依赖模型服务去抓腾讯图床，最稳。"""
        if source.startswith("data:"):
            return source
        if source.startswith("base64://"):
            try:
                raw = base64.b64decode(source[len("base64://"):])
            except Exception:  # noqa: BLE001
                return ""
            return self._encode(raw)

        response = await self._http.get(source)
        response.raise_for_status()
        if len(response.content) > self.cfg.max_bytes:
            self.logger.info("图片太大（%.1f MB），跳过", len(response.content) / 1048576)
            return ""
        return self._encode(response.content)

    def _encode(self, raw: bytes) -> str:
        if not raw or len(raw) > self.cfg.max_bytes:
            return ""
        return f"data:{guess_mime(raw)};base64,{base64.b64encode(raw).decode('ascii')}"

    async def _ask(self, data_url: str, segment: Segment) -> str:
        prompt = self.cfg.prompt.strip()
        if is_sticker(segment):
            prompt = "【这是一张表情包】\n" + prompt
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        text = (await self._llm.chat(messages)).strip()
        text = " ".join(text.split())
        self.logger.info("看图结果：%s", text[:100])
        return text[:_MAX_DESCRIPTION_CHARS]

    async def short_name(self, data: bytes) -> str:
        """给一张表情包起个短名字，用于之后挑选。失败返回空字符串。"""
        data_url = self._encode(data)
        if not data_url:
            return ""
        prompt = (
            "给这个表情包起一个名字，供聊天时挑选使用。\n"
            "要求：不超过 6 个汉字，只描述画面或情绪，不要标点、不要引号、不要解释。\n"
            "例子：猫猫大笑、无语翻白眼、点赞、震惊"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        text = (await self._llm.chat(messages)).strip()
        first_line = text.splitlines()[0] if text else ""
        return first_line.strip().strip("\"“”「」'‘’【】[]()（）").strip()

    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        await self._http.aclose()
        await self._llm.close()
