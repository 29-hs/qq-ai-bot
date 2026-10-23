"""日志初始化：控制台 + 可选的滚动文件。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "[%(asctime)s] %(levelname)-7s %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", file: str | None = None) -> logging.Logger:
    """配置根 logger，返回项目 logger。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # Windows 控制台默认 GBK，强制 UTF-8，避免中文/表情炸日志
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover - 部分终端不支持
        pass

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    root.addHandler(console)

    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        root.addHandler(rotating)

    # 第三方库的噪音降级
    for name in ("httpx", "httpcore", "websockets", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return logging.getLogger("qqbot")
