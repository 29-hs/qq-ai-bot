"""机器人大脑：判断该不该回 → 组装上下文 → 调模型 → 回消息。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .commands import CommandRouter
from .config import AppConfig
from .events import MessageEvent, Segment, segment_text
from .llm import LLMClient, LLMError
from .memory import MemoryStore
from .stickers import StickerLibrary, sync_custom_faces
from .utils import escape_cq, human_duration, split_message, strip_wake_words, truncate
from .vision import ImageAnalyst, is_sticker

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class ChatBot:
    def __init__(
        self,
        cfg: AppConfig,
        llm: LLMClient,
        memory: MemoryStore,
        logger: logging.Logger,
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.memory = memory
        self.logger = logger
        self.client: Any = None  # 由 __main__ 注入 OneBotClient
        self.commands = CommandRouter(self)

        self.self_id: int = cfg.qq.self_id
        self.start_time = time.monotonic()
        self.stats = {"events": 0, "replies": 0, "llm_calls": 0, "errors": 0}

        # 图片理解是可选的：没配视觉模型就退回 [图片] 占位符
        self.vision: ImageAnalyst | None = (
            ImageAnalyst(cfg.vision, logger=logger) if cfg.vision.enabled else None
        )
        # 本地表情包库：目录里有图，模型就能发
        self.stickers = StickerLibrary(cfg.bot.stickers_dir, logger)

        self._semaphore = asyncio.Semaphore(max(1, cfg.bot.concurrency))
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_reply: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #
    async def handle_event(self, payload: dict[str, Any]) -> None:
        post_type = payload.get("post_type")

        # QQ 号可能在 meta_event 或 get_login_info 里才拿到
        if not self.self_id and self.client is not None:
            self.self_id = int(getattr(self.client, "self_id", 0) or 0)

        if post_type == "meta_event":
            self._on_meta(payload)
            return
        if post_type != "message":
            return  # notice / request 之类暂不处理

        event = MessageEvent.from_payload(payload)
        if event is None:
            return

        self.stats["events"] += 1
        if event.self_id and not self.self_id:
            self.self_id = event.self_id

        # 自己发的、系统号、黑名单，一律不回
        if not event.user_id or event.user_id == self.self_id:
            return
        if event.sub_type in ("group_self", "self"):
            return
        if event.user_id in self.cfg.bot.deny_users:
            return
        # 白名单非空时，只认名单里的人
        if self.cfg.bot.allow_users and event.user_id not in self.cfg.bot.allow_users:
            self.logger.debug("QQ %s 不在白名单里，忽略", event.user_id)
            return

        trigger = self.cfg.bot.trigger
        if event.is_private:
            if not trigger.private:
                return
        else:
            if trigger.groups and event.group_id not in trigger.groups:
                return
            if event.group_id in trigger.exclude_groups:
                return

        addressed, text, at_me = self._decide_reply(event)
        if not addressed:
            return

        # 指令优先（即使本群被 /off 关掉，也要能用 /on 打开）
        if await self.commands.handle(event, text):
            return

        if event.is_group and not self.memory.is_enabled(event.session_key):
            return

        images = self._collect_images(event) if self.vision is not None else []

        # 只有图片、没有文字也算有效消息（让 AI 看图说话）
        if not text and not images:
            if at_me or event.is_private:
                await self.send(event, self.cfg.bot.empty_mention_reply)
            return

        self.logger.info(
            "收到 [%s] %s(%s): %s%s",
            "群" + str(event.group_id) if event.is_group else "私聊",
            event.sender_name,
            event.user_id,
            truncate(text, 80),
            f"（附图 {len(images)} 张）" if images else "",
        )
        await self._respond(event, text, images)

    def _on_meta(self, payload: dict[str, Any]) -> None:
        self_id = payload.get("self_id")
        if self_id:
            try:
                self.self_id = int(self_id)
            except (TypeError, ValueError):
                pass
        if payload.get("meta_event_type") == "lifecycle":
            self.logger.info("QQ 账号已就绪：self_id=%s", payload.get("self_id"))
        elif payload.get("meta_event_type") == "heartbeat" and payload.get("status"):
            status = payload["status"]
            if isinstance(status, dict) and status.get("online") is False:
                self.logger.warning("OneBot 报告账号掉线")

    # ------------------------------------------------------------------ #
    # 触发判断
    # ------------------------------------------------------------------ #
    def _wake_words(self) -> list[str]:
        words = [self.cfg.bot.name, *self.cfg.bot.trigger.wake_words]
        seen: list[str] = []
        for word in words:
            word = (word or "").strip()
            if word and word.lower() not in {w.lower() for w in seen}:
                seen.append(word)
        return seen

    def _decide_reply(self, event: MessageEvent) -> tuple[bool, str, bool]:
        """返回：要不要回、去掉唤醒词后的正文、是不是 @ 了我。"""
        parts: list[str] = []
        at_me = False
        at_all = False
        my_qq = str(event.self_id or self.self_id)
        for segment in event.segments:
            if segment.type == "at":
                qq = str(segment.data.get("qq") or "")
                if qq == my_qq:
                    at_me = True
                    continue
                if qq == "all":
                    at_all = True
                    continue
            parts.append(segment_text(segment))
        text = "".join(parts).strip()

        trigger = self.cfg.bot.trigger
        if event.is_private:
            addressed = True
        elif at_me:
            addressed = True
        elif at_all and trigger.respond_to_at_all:
            addressed = True
        elif trigger.respond_to_images and any(
            seg.type in ("image", "mface") for seg in event.segments
        ):
            # 有人往群里丢图/表情包，就算在逗它
            addressed = True
        else:
            lowered = text.lower()
            if any(word.lower() in lowered for word in self._wake_words()):
                addressed = True
            elif trigger.prefix and lowered.startswith(trigger.prefix.lower()):
                addressed = True
            else:
                addressed = not trigger.require_mention

        cleaned = text
        if addressed:
            cleaned = strip_wake_words(text, self._wake_words())
            if trigger.prefix and cleaned.lower().startswith(trigger.prefix.lower()):
                cleaned = cleaned[len(trigger.prefix):].strip()
        return addressed, cleaned, at_me

    # ------------------------------------------------------------------ #
    # 调模型
    # ------------------------------------------------------------------ #
    async def _respond(
        self,
        event: MessageEvent,
        text: str,
        images: list[tuple[str, Segment]] | None = None,
    ) -> None:
        key = event.session_key
        lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            cooldown = self.cfg.bot.cooldown_seconds
            if cooldown > 0:
                last = self._last_reply.get(key)
                if last is not None and time.monotonic() - last < cooldown:
                    self.logger.debug("会话 %s 冷却中，跳过", key)
                    return

            if images and self.vision is not None:
                self.vision.onebot = self.client
                self.stats["llm_calls"] += 1
                descriptions = await self.vision.describe([seg for _, seg in images])
                text = self._apply_descriptions(text, images, descriptions)

            user_line = self._format_user_line(event, text)
            history = self.memory.history(
                key,
                max_turns=self.cfg.llm.max_history_turns,
                max_chars=self.cfg.llm.max_history_chars,
                ttl_hours=self.cfg.llm.history_ttl_hours,
            )
            messages = [
                {"role": "system", "content": self._system_prompt(event, key)},
                *history,
                {"role": "user", "content": user_line},
            ]

            async with self._semaphore:
                try:
                    self.stats["llm_calls"] += 1
                    reply = await self.llm.chat(messages)
                except LLMError as exc:
                    self.stats["errors"] += 1
                    self.logger.error("模型调用失败：%s", exc)
                    await self.send(event, exc.user_message)
                    return

            self.memory.add_turn(key, user_line, reply)
            self._last_reply[key] = time.monotonic()
            self.stats["replies"] += 1
            self.logger.info("回复 %s：%s", truncate(event.sender_name, 12), truncate(reply, 80))
            await self.send(event, reply)

    # ------------------------------------------------------------------ #
    # 图片
    # ------------------------------------------------------------------ #
    @staticmethod
    def _collect_images(event: MessageEvent) -> list[tuple[str, Segment]]:
        """找出可以送去识别的图，返回 (占位符, 消息段)。"""
        images: list[tuple[str, Segment]] = []
        for segment in event.segments:
            if segment.type == "image":
                images.append(("[图片]", segment))
            elif segment.type == "mface" and str(segment.data.get("url") or "").startswith("http"):
                images.append(("[表情]", segment))
        return images

    @staticmethod
    def _apply_descriptions(
        text: str,
        images: list[tuple[str, Segment]],
        descriptions: list[str],
    ) -> str:
        """把 [图片] / [表情] 换成带描述的版本；没识别出来也标注清楚。"""
        items: list[str] = []
        for (placeholder, segment), description in zip(images, descriptions):
            label = "表情包" if is_sticker(segment) else "图片"
            item = f"[{label}：{description}]" if description else f"[{label}：内容未能识别]"
            if placeholder in text:
                text = text.replace(placeholder, item, 1)
            elif description:
                text = f"{text} {item}".strip()
            items.append(item)
        if not text.strip():
            text = " ".join(items)
        return text

    async def close(self) -> None:
        if self.vision is not None:
            await self.vision.close()

    async def sync_stickers(self) -> tuple[int, int]:
        """把机器人 QQ 里收藏的表情包同步到本地表情库。"""
        if self.client is None:
            return 0, 0
        describer = self.vision.short_name if self.vision is not None else None
        return await sync_custom_faces(self.stickers, self.client, describer, self.logger)

    @staticmethod
    def _format_user_line(event: MessageEvent, text: str) -> str:
        return f"{event.sender_name}：{text}" if event.is_group else text

    def _system_prompt(self, event: MessageEvent, key: str) -> str:
        persona = self.memory.get_persona(key) or self.cfg.llm.system_prompt
        persona = persona.replace("{bot_name}", self.cfg.bot.name)

        try:
            now = datetime.now(ZoneInfo(self.cfg.bot.timezone))
        except Exception:  # noqa: BLE001 - 时区名写错也不该让机器人挂掉
            now = datetime.now()
        info = "\n".join(
            [
                f"- 现在时间：{now:%Y-%m-%d %H:%M}（{_WEEKDAYS[now.weekday()]}，{self.cfg.bot.timezone}）",
                f"- 场景：{event.scene}",
                f"- 正在跟你说话的人：{event.sender_name}（QQ {event.user_id}）",
                "- 输出要求：纯文本，不要用 Markdown，不要输出 [CQ:...] 之类的控制码。",
            ]
        )
        if "{current_info}" in persona:
            prompt = persona.replace("{current_info}", info)
        else:
            prompt = f"{persona}\n\n[当前信息]\n{info}"

        if self.stickers.names:
            prompt += (
                "\n\n[表情包]\n"
                "你存了一些表情包，可以在回复里用 [图:名字] 发出去，系统会自动换成图片。\n"
                f"可用的有：{'、'.join(self.stickers.names)}\n"
                "用法：文字照常写，把 [图:名字] 放在想发表情的位置；**一次最多用一张**，"
                "而且别每句都用，只在真的很合适的时候发。"
            )
        return prompt

    # ------------------------------------------------------------------ #
    # 发消息
    # ------------------------------------------------------------------ #
    async def send(self, event: MessageEvent, text: str) -> None:
        raw = (text or "").strip()
        if not raw:
            return
        if self.client is None:
            self.logger.error("OneBot 客户端还没初始化，无法发送消息")
            return

        # 把 [图:名字] 摘出来，剩下的当文字发，表情包单独发一条
        text, stickers = self.stickers.extract(raw)
        limit = max(0, self.cfg.bot.max_stickers_per_reply)
        stickers = stickers[:limit]
        if not text and not stickers:
            return

        first = True
        for chunk in split_message(text, self.cfg.bot.max_reply_chars):
            segments: list[dict[str, Any]] = []
            if first:
                segments.extend(self._reply_prefix(event))
                first = False
            segments.append({"type": "text", "data": {"text": escape_cq(chunk)}})
            if not await self._send_segments(event, segments):
                return
            await asyncio.sleep(0.4)  # 分条之间喘口气，别被风控

        for data in stickers:
            segments = []
            if first:
                segments.extend(self._reply_prefix(event))
                first = False
            segments.append(StickerLibrary.to_segment(data))
            if not await self._send_segments(event, segments):
                return
            await asyncio.sleep(0.3)

    def _reply_prefix(self, event: MessageEvent) -> list[dict[str, Any]]:
        """回复前缀：引用原消息 或 @ 对方。"""
        if self.cfg.bot.reply_with_quote and event.message_id:
            return [{"type": "reply", "data": {"id": str(event.message_id)}}]
        if event.is_group and self.cfg.bot.reply_with_at:
            return [
                {"type": "at", "data": {"qq": str(event.user_id)}},
                {"type": "text", "data": {"text": " "}},
            ]
        return []

    async def _send_segments(self, event: MessageEvent, segments: list[dict[str, Any]]) -> bool:
        if not segments:
            return True
        params: dict[str, Any] = {"message": segments, "auto_escape": False}
        if event.is_group:
            action = "send_group_msg"
            params["group_id"] = event.group_id
        else:
            action = "send_private_msg"
            params["user_id"] = event.user_id
            if event.group_id:  # 群临时会话
                params["group_id"] = event.group_id
        try:
            await self.client.call_api(action, params, timeout=20)
            return True
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.logger.error("发送失败（%s）：%s", action, exc)
            return False

    # ------------------------------------------------------------------ #
    def is_admin(self, event: MessageEvent) -> bool:
        if event.user_id in self.cfg.bot.admins:
            return True
        # 没配管理员时，私聊本人拥有全部权限（自用场景）
        return not self.cfg.bot.admins and not event.is_group

    def status_text(self) -> str:
        stats = self.memory.stats()
        return "\n".join(
            [
                "🤖 运行状态",
                f"已运行：{human_duration(time.monotonic() - self.start_time)}",
                f"收到消息：{self.stats['events']} 条｜回复：{self.stats['replies']} 条"
                f"｜报错：{self.stats['errors']} 次",
                f"模型：{self.cfg.llm.model}",
                f"用量：{self.llm.usage_summary()}",
                f"记忆：{stats['messages']} 条消息 / {stats['active_sessions']} 个活跃会话",
            ]
        )
