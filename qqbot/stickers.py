"""表情包仓库：把 `stickers/` 目录里的图片当成机器人可以发的表情包。

约定：文件名就是触发词。例如 `stickers/开心.png` 对应模型输出 `[图:开心]`。
不需要改配置——目录里放什么，模型就知道能发什么。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

TAG_PATTERN = re.compile(r"\[图\s*[:：]\s*([^\[\]]{1,20})\]")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_SYNC_INDEX = ".synced.json"
_MAX_BYTES = 4 * 1024 * 1024

_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)
_UNSAFE = re.compile(r'[\\/:*?"<>|\s\x00-\x1f]+')


def _extension(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _safe_name(name: str, fallback: str) -> str:
    cleaned = _UNSAFE.sub("", name or "").strip(".")
    return (cleaned or fallback)[:12]


class StickerLibrary:
    def __init__(self, directory: str | Path, logger: logging.Logger | None = None) -> None:
        self.dir = Path(directory)
        self.logger = logger or logging.getLogger("qqbot.stickers")
        self._files: dict[str, Path] = {}
        self.reload()

    # ------------------------------------------------------------------ #
    def reload(self) -> None:
        self._files = {}
        if not self.dir.is_dir():
            return
        for path in sorted(self.dir.iterdir()):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                name = path.stem.strip()
                if name:
                    self._files[name] = path
        if self._files:
            self.logger.info("表情包库：%d 个（%s）", len(self._files), "、".join(self.names))

    @property
    def names(self) -> list[str]:
        return list(self._files)

    def find(self, name: str) -> Path | None:
        """精确 > 忽略大小写 > 包含，三级匹配，容忍模型写成"开心一点"。"""
        key = name.strip()
        if not key:
            return None
        if key in self._files:
            return self._files[key]
        lowered = key.lower()
        for candidate, path in self._files.items():
            if candidate.lower() == lowered:
                return path
        for candidate, path in self._files.items():
            if lowered in candidate.lower() or candidate.lower() in lowered:
                return path
        return None

    def extract(self, text: str) -> tuple[str, list[bytes]]:
        """把 `[图:xxx]` 从文本里摘出来，返回（干净文本, 图片列表）。"""
        images: list[bytes] = []

        def replace(match: re.Match[str]) -> str:
            path = self.find(match.group(1))
            if path is None:
                self.logger.debug("没有这张表情包：%s", match.group(1))
                return ""
            try:
                data = path.read_bytes()
            except OSError as exc:
                self.logger.warning("读取表情包失败：%s", exc)
                return ""
            if len(data) > _MAX_BYTES:
                self.logger.warning("表情包太大，跳过：%s", path.name)
                return ""
            images.append(data)
            return ""

        cleaned = TAG_PATTERN.sub(replace, text)
        return cleaned.strip(), images

    @staticmethod
    def to_segment(data: bytes) -> dict:
        """转成 OneBot 的 image 消息段（base64，不依赖本地文件路径）。"""
        return {
            "type": "image",
            "data": {"file": "base64://" + base64.b64encode(data).decode("ascii")},
        }

    # ------------------------------------------------------------------ #
    # 同步 QQ 收藏表情
    # ------------------------------------------------------------------ #
    def _index_path(self) -> Path:
        return self.dir / _SYNC_INDEX

    def load_synced(self) -> dict[str, str]:
        try:
            raw = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def save_synced(self, data: dict[str, str]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._index_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            self.logger.warning("保存同步索引失败：%s", exc)

    def unique_path(self, base: str, ext: str) -> Path:
        candidate = self.dir / f"{base}{ext}"
        index = 2
        while candidate.exists():
            candidate = self.dir / f"{base}{index}{ext}"
            index += 1
        return candidate


async def sync_custom_faces(
    library: StickerLibrary,
    client: Any,
    describer: Callable[[bytes], Awaitable[str]] | None = None,
    logger: logging.Logger | None = None,
) -> tuple[int, int]:
    """把机器人 QQ 里收藏的表情包拉下来，存进 sticker 目录。

    返回 (新增数量, 失败数量)。已经同步过的 URL 会跳过，所以可以反复调用。
    """
    log = logger or library.logger
    try:
        urls = await client.call_api("fetch_custom_face", {}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log.warning("拉取收藏表情失败：%s", exc)
        return 0, 1
    if not isinstance(urls, list) or not urls:
        return 0, 0

    library.dir.mkdir(parents=True, exist_ok=True)
    synced = library.load_synced()
    added = failed = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 QQBot/1.0"},
    ) as http:
        for url in urls:
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            key = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
            if key in synced:
                continue
            try:
                response = await http.get(url)
                response.raise_for_status()
                data = response.content
            except Exception as exc:  # noqa: BLE001
                log.debug("下载表情失败 %s：%s", url, exc)
                failed += 1
                continue
            if not data or len(data) > _MAX_BYTES:
                failed += 1
                continue

            name = ""
            if describer is not None:
                try:
                    name = await describer(data)
                except Exception as exc:  # noqa: BLE001
                    log.debug("表情起名失败：%s", exc)
            name = _safe_name(name, f"收藏{key[:4]}")

            path = library.unique_path(name, _extension(data))
            try:
                path.write_bytes(data)
            except OSError as exc:
                log.warning("写入表情失败：%s", exc)
                failed += 1
                continue
            synced[key] = path.name
            added += 1

    library.save_synced(synced)
    library.reload()
    if added:
        log.info("同步收藏表情：新增 %d 个，失败 %d 个", added, failed)
    return added, failed
