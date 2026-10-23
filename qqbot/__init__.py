"""一个能接入 QQ（OneBot v11 / NapCatQQ）的 AI 聊天机器人。"""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "1.0.0"

# 支持把依赖解包在项目内的 .deps 目录（pip 用不了时的备用方案，见 scripts/offline_deps.py）。
# 用 append 而不是 insert：系统里正常装好的包优先级更高。
_DEPS_DIR = Path(__file__).resolve().parent.parent / ".deps"
if _DEPS_DIR.is_dir() and str(_DEPS_DIR) not in sys.path:
    sys.path.append(str(_DEPS_DIR))
