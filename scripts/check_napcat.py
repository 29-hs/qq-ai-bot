"""连接自检：确认 NapCat 的地址、端口、Token 对不对，顺便把账号和群列出来。

用法（在项目目录里）：
    python scripts/check_napcat.py
    python scripts/check_napcat.py -c D:\\qq-ai-bot\\config.yaml

它只读 config.yaml 的 qq 段，不需要先填 DeepSeek 的 API Key。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DEPS = ROOT / ".deps"
if _DEPS.is_dir():
    sys.path.insert(0, str(_DEPS))

import websockets  # noqa: E402
import yaml  # noqa: E402
from websockets.exceptions import WebSocketException  # noqa: E402

from qqbot.config import QQConfig, ConfigError, load_dotenv  # noqa: E402

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""),
            value,
        )
    return value


def load_qq_config(path: Path) -> QQConfig:
    """只解析 qq 段，跳过模型 Key 校验。"""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}")
    load_dotenv(path.parent / ".env")
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 根节点应该是键值对")
    qq_raw = raw.get("qq") or {}
    if not isinstance(qq_raw, dict):
        raise ConfigError("qq 段应该是缩进块")
    qq_raw = {k: _expand(v) for k, v in qq_raw.items()}
    return QQConfig.from_dict(qq_raw)


async def _call(ws, action: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"action": action, "params": params or {}, "echo": "self-check"}, ensure_ascii=False))
    for _ in range(30):
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(raw)
        if data.get("echo") == "self-check" and "post_type" not in data:
            return data
    raise TimeoutError("等不到响应")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查能不能连上 NapCat")
    parser.add_argument("-c", "--config", default=str(ROOT / "config.yaml"))
    args = parser.parse_args(argv)

    try:
        cfg = load_qq_config(Path(args.config))
    except ConfigError as exc:
        print(f"❌ 配置有问题：{exc}")
        return 2

    if cfg.mode != "forward":
        print("当前 config.yaml 是 reverse 模式，这个自检只检查正向连接。")
        print("先在 NapCat 里配「WebSocket 客户端」连到本程序，或把 qq.mode 改成 forward。")
        return 2

    url = cfg.ws_url
    if cfg.access_token:
        url += ("&" if "?" in url else "?") + "access_token=" + quote(cfg.access_token, safe="")

    print(f"正在连接 {cfg.ws_url} …（Token：{'已填写' if cfg.access_token else '未填写'}）")
    try:
        ws = await asyncio.wait_for(
            websockets.connect(url, open_timeout=10, max_size=None), timeout=15
        )
    except (OSError, asyncio.TimeoutError, WebSocketException) as exc:
        print(f"❌ 连不上：{exc}")
        print("   按顺序排查：")
        print("   1. NapCat 启动了吗？扫码登录成功了吗？")
        print("   2. WebUI → 网络配置 里「WebSocket 服务端」启用了吗？（勾了「保存时启用」吗）")
        print(f"   3. 端口对吗？现在用的是 {cfg.ws_url}，NapCat 默认 3001")
        print("   4. Token 一致吗？NapCat 里填的 和 .env 的 NAPCAT_TOKEN 要一模一样")
        return 1

    async with ws:
        print("✅ 端口和 Token 都通了")
        try:
            info = (await _call(ws, "get_login_info")).get("data") or {}
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 握手成功，但取账号信息失败：{exc}")
            return 1
        print(f"✅ 登录账号：{info.get('nickname') or '?'}（{info.get('user_id') or '?'}）")

        try:
            groups = (await _call(ws, "get_group_list")).get("data") or []
        except Exception:  # noqa: BLE001
            groups = []
        if groups:
            print(f"✅ 群列表：共 {len(groups)} 个")
            for g in groups[:8]:
                print(f"   · {g.get('group_name')}（{g.get('group_id')}）")
            if len(groups) > 8:
                print(f"   …… 还有 {len(groups) - 8} 个")
            print("\n提示：想只让机器人在某个群工作，把群号填进 config.yaml 的 bot.trigger.groups")
        else:
            print("（这个号还没加群，私聊测试也够用）")

    print("\n一切正常，去启动机器人吧：python run.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
