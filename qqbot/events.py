"""OneBot v11 事件与消息段模型。

OneBot 的 message 字段有两种形态：
  - 字符串（CQ 码）："你好[CQ:at,qq=123][CQ:face,id=1]"
  - 数组：[{"type": "text", "data": {"text": "你好"}}, ...]
这里统一转成 Segment 列表，方便判断「有没有 @ 我」「纯文本是什么」。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from .utils import unescape_cq

CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_.\-]+)((?:,[^\]]*)?)\]")

# 非文本消息在喂给大模型时的占位符
PLACEHOLDERS: dict[str, str] = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "forward": "[聊天记录]",
    "contact": "[名片]",
    "location": "[位置]",
    "music": "[音乐]",
    "share": "[链接]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "poke": "[戳一戳]",
    "markdown": "[内容]",
}


@dataclass(slots=True)
class Segment:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


def parse_cq_string(text: str) -> list[Segment]:
    """把 CQ 码字符串拆成消息段。"""
    segments: list[Segment] = []
    pos = 0
    for match in CQ_PATTERN.finditer(text):
        if match.start() > pos:
            segments.append(Segment("text", {"text": unescape_cq(text[pos:match.start()])}))
        cq_type = match.group(1)
        params: dict[str, Any] = {}
        for pair in match.group(2).split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            params[key.strip()] = unescape_cq(value)
        segments.append(Segment(cq_type, params))
        pos = match.end()
    if pos < len(text):
        segments.append(Segment("text", {"text": unescape_cq(text[pos:])}))
    return segments


def normalize_message(message: Any) -> list[Segment]:
    """把 str / list 形态的 message 统一成 Segment 列表。"""
    if isinstance(message, str):
        return parse_cq_string(message)
    if isinstance(message, list):
        result: list[Segment] = []
        for item in message:
            if not isinstance(item, dict):
                continue
            seg_type = str(item.get("type") or "text")
            data = item.get("data")
            result.append(Segment(seg_type, dict(data) if isinstance(data, dict) else {}))
        return result
    return []


def segment_text(segment: Segment) -> str:
    """单个消息段转成人类可读文本（用于喂模型和日志）。"""
    if segment.type == "text":
        return str(segment.data.get("text") or "")
    if segment.type in ("reply", "source"):
        return ""
    if segment.type == "at":
        qq = str(segment.data.get("qq") or "")
        return "@全体成员" if qq == "all" else f"@{qq}"
    return PLACEHOLDERS.get(segment.type, f"[{segment.type}]")


def segments_to_text(segments: list[Segment]) -> str:
    return "".join(segment_text(s) for s in segments).strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class MessageEvent:
    """一条收到的聊天消息。"""

    self_id: int
    message_type: str  # private / group
    sub_type: str
    message_id: int
    user_id: int
    group_id: int
    sender_name: str
    time: int
    segments: list[Segment]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_group(self) -> bool:
        return self.message_type == "group"

    @property
    def is_private(self) -> bool:
        return self.message_type == "private"

    @property
    def plain_text(self) -> str:
        return segments_to_text(self.segments)

    @property
    def session_key(self) -> str:
        return f"group:{self.group_id}" if self.is_group else f"private:{self.user_id}"

    @property
    def scene(self) -> str:
        return f"QQ群（群号 {self.group_id}）" if self.is_group else "QQ私聊"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MessageEvent | None":
        """从 OneBot 事件里提取消息；不是聊天消息就返回 None。"""
        if payload.get("post_type") != "message":
            return None
        message_type = payload.get("message_type")
        if message_type not in ("private", "group"):
            return None

        sender = payload.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        user_id = _to_int(payload.get("user_id"))
        group_id = _to_int(payload.get("group_id"))
        name = (
            str(sender.get("card") or "").strip()
            or str(sender.get("nickname") or "").strip()
            or f"QQ{user_id}"
        )

        message = payload.get("message")
        segments = normalize_message(message)
        if not segments and payload.get("raw_message"):
            segments = parse_cq_string(str(payload["raw_message"]))

        return cls(
            self_id=_to_int(payload.get("self_id")),
            message_type=str(message_type),
            sub_type=str(payload.get("sub_type") or ""),
            message_id=_to_int(payload.get("message_id")),
            user_id=user_id,
            group_id=group_id,
            sender_name=name,
            time=_to_int(payload.get("time"), int(time.time())),
            segments=segments,
            raw=payload,
        )
