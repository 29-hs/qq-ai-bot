"""程序入口：python run.py  /  python -m qqbot"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .bot import ChatBot
from .config import AppConfig, ConfigError, load_config
from .llm import LLMClient
from .lock import InstanceLock
from .logger import setup_logging
from .memory import MemoryStore
from .onebot import OneBotClient


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qq-ai-bot",
        description="接入 QQ 的 AI 聊天机器人（OneBot v11 / NapCatQQ）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python run.py                     # 用默认 config.yaml 启动\n"
            "  python run.py -c my.yaml -v       # 指定配置并输出调试日志\n"
            "  python run.py --check             # 只检查配置对不对，不连 QQ\n"
        ),
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--check", action="store_true", help="只检查配置，不启动机器人")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def _print_banner(cfg: AppConfig) -> None:
    print("=" * 66)
    print(f"  QQ AI Bot  ·  {cfg.bot.name}  ·  OneBot v11")
    print("=" * 66)
    print(cfg.summary())
    print("=" * 66, flush=True)


async def _prune_loop(bot: ChatBot) -> None:
    interval = bot.cfg.memory.prune_interval_minutes
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval * 60)
        try:
            removed = bot.memory.prune(bot.cfg.llm.history_ttl_hours)
            if removed:
                bot.logger.info("清理了 %d 条过期记忆", removed)
        except Exception:  # noqa: BLE001
            bot.logger.exception("清理过期记忆失败")


async def _run(cfg: AppConfig, logger) -> None:
    memory = MemoryStore(cfg.memory.db_path, logger)
    llm = LLMClient(cfg.llm, logger)
    bot = ChatBot(cfg, llm, memory, logger)
    client = OneBotClient(cfg.qq, bot.handle_event, logger)
    bot.client = client

    tasks = [
        asyncio.create_task(client.run_forever(), name="onebot"),
        asyncio.create_task(_prune_loop(bot), name="prune"),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        await bot.close()
        await llm.close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"❌ 配置有问题：{exc}", file=sys.stderr)
        return 2

    if args.check:
        print("✅ 配置检查通过")
        _print_banner(cfg)
        return 0

    if args.verbose:
        cfg.log.level = "DEBUG"

    logger = setup_logging(cfg.log.level, cfg.log.file or None)
    _print_banner(cfg)

    lock = InstanceLock(Path(cfg.memory.db_path).parent / "bot.lock")
    if not lock.acquire():
        print(
            "\n❌ 已经有一个机器人在运行了（同一个 data 目录只允许一个实例）。\n"
            "   如果你确实想重开：先关掉原来那个黑窗口，再启动这个。\n",
            file=sys.stderr,
        )
        return 3

    logger.info("启动中… 按 Ctrl+C 退出")

    try:
        asyncio.run(_run(cfg, logger))
    except KeyboardInterrupt:
        print("\n👋 已退出")
    except Exception:  # noqa: BLE001
        logger.exception("程序异常退出")
        return 1
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
