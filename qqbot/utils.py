"""纯文本处理工具：CQ 码转义、长消息切分、唤醒词清理。"""

from __future__ import annotations

import re

# CQ 码规定这三个字符必须转义，否则用户发的 "&" / "[CQ:...]" 会被 QQ 解析成别的东西
_ESCAPE_TABLE = str.maketrans({"&": "&amp;", "[": "&#91;", "]": "&#93;"})
_UNESCAPE_RE = re.compile(r"&amp;|&#91;|&#93;")
_UNESCAPE_MAP = {"&amp;": "&", "&#91;": "[", "&#93;": "]"}

# 切分时优先在这些位置断开
_SENTENCE_ENDS = ("\n\n", "\n", "。", "！", "？", "；", "…", ". ", "! ", "? ", "; ", "，", ", ")

_LEADING_NOISE = " \t\r\n，,。.！!？?：:；;、~～-—"


def escape_cq(text: str) -> str:
    """转义为可安全放进消息段的文本。"""
    return text.translate(_ESCAPE_TABLE)


def unescape_cq(text: str) -> str:
    """还原转义字符。"""
    return _UNESCAPE_RE.sub(lambda m: _UNESCAPE_MAP[m.group(0)], text)


def split_message(text: str, limit: int = 1200) -> list[str]:
    """把长文本切成不超过 limit 的若干段，尽量在句末断开。"""
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0:
        return [text]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for sep in _SENTENCE_ENDS:
            idx = window.rfind(sep)
            # 太靠前的断点会让每条都很短，所以要求至少用掉一半
            if idx >= limit // 2:
                cut = idx + len(sep)
                break
        if cut <= 0:
            idx = window.rfind(" ")
            cut = idx if idx >= limit // 2 else limit
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        chunks.append(remaining.strip())
    return [c for c in chunks if c]


def strip_wake_words(text: str, words: list[str]) -> str:
    """去掉唤醒词，以及开头多余的标点和空白（句尾的问号感叹号要留着）。"""
    out = text
    for word in sorted({w for w in words if w}, key=len, reverse=True):
        out = re.sub(re.escape(word), "", out, flags=re.IGNORECASE)
    return out.strip().lstrip(_LEADING_NOISE).strip()


def truncate(text: str, limit: int = 40) -> str:
    """日志里截断长文本。"""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}天{hours}小时"
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"
