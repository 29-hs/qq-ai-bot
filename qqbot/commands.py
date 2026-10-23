"""QQ 上可以用的控制指令。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .bot import ChatBot
    from .events import MessageEvent

HELP_TEXT = """📖 指令表

/help            看这条帮助
/reset           清空当前会话的记忆
/persona         查看当前人设
/persona 内容     修改人设（群里只有管理员能改）
/persona reset   恢复默认人设
/on  /off        开关本群的自动回复（管理员）
/at on /at off   切换「群里是否必须 @ 我」（管理员，临时生效）
/sync            同步 QQ 收藏的表情包（管理员）
/status          运行状态（管理员）

怎么跟我搭话：
· 私聊：直接发消息就行
· 群聊：@我，或者带上我的名字，例如「小D 今天吃啥」"""


class CommandRouter:
    def __init__(self, bot: "ChatBot") -> None:
        self.bot = bot

    async def handle(self, ev: "MessageEvent", text: str) -> bool:
        """是命令就执行并返回 True；不是命令返回 False（交给大模型）。"""
        body = text.strip()
        prefix = self.bot.cfg.bot.command_prefix
        if prefix and body.startswith(prefix):
            body = body[len(prefix):].strip()
        elif body.lower() in ("help", "帮助", "指令"):
            body = "help"
        else:
            return False

        name, _, arg = body.partition(" ")
        name = name.strip().lower()
        handler = getattr(self, f"cmd_{name}", None) if name.isidentifier() else None
        if handler is None:
            return False
        await handler(ev, arg.strip())
        return True

    # ------------------------------------------------------------------ #
    async def cmd_help(self, ev: "MessageEvent", arg: str) -> None:
        await self.bot.send(ev, HELP_TEXT)

    async def cmd_reset(self, ev: "MessageEvent", arg: str) -> None:
        removed = self.bot.memory.reset(ev.session_key)
        if removed:
            await self.bot.send(ev, f"好，忘了，刚才那 {removed} 条聊天记录清掉了。")
        else:
            await self.bot.send(ev, "本来就没记着啥，重新开始吧。")

    async def cmd_persona(self, ev: "MessageEvent", arg: str) -> None:
        memory = self.bot.memory
        key = ev.session_key

        if not arg:
            current = (memory.get_persona(key) or self.bot.cfg.llm.system_prompt).strip()
            preview = current if len(current) <= 400 else current[:400] + "…"
            await self.bot.send(ev, "当前人设：\n" + preview)
            return

        if arg.lower() in ("reset", "default", "重置", "默认"):
            if ev.is_group and not self.bot.is_admin(ev):
                await self.bot.send(ev, "群里改人设要有管理员权限。")
                return
            memory.set_persona(key, None)
            await self.bot.send(ev, "人设已经恢复默认。")
            return

        if ev.is_group and not self.bot.is_admin(ev):
            await self.bot.send(ev, "群里改人设要有管理员权限哦。")
            return
        memory.set_persona(key, arg)
        await self.bot.send(ev, "记住了，接下来我就按这个人设说话。")

    async def cmd_on(self, ev: "MessageEvent", arg: str) -> None:
        if not self.bot.is_admin(ev):
            await self.bot.send(ev, "这个只有管理员能用。")
            return
        self.bot.memory.set_enabled(ev.session_key, True)
        await self.bot.send(ev, "好，恢复自动回复。")

    async def cmd_off(self, ev: "MessageEvent", arg: str) -> None:
        if not self.bot.is_admin(ev):
            await self.bot.send(ev, "这个只有管理员能用。")
            return
        self.bot.memory.set_enabled(ev.session_key, False)
        await self.bot.send(ev, "行，我先闭嘴。需要我时发 /on。")

    async def cmd_at(self, ev: "MessageEvent", arg: str) -> None:
        """群里要不要 @ 才回。改的是运行中的内存配置，重启会回到 config.yaml 的值。"""
        if not self.bot.is_admin(ev):
            await self.bot.send(ev, "这个只有管理员能用。")
            return

        trigger = self.bot.cfg.bot.trigger
        mode = arg.strip().lower()

        if mode in ("", "status", "状态"):
            state = "需要 @ 或叫名字" if trigger.require_mention else "不需要 @，所有消息都回"
            await self.bot.send(
                ev,
                f"当前规则：{state}\n"
                "用法：\n"
                "/at on   群里必须 @ 我或叫我才回\n"
                "/at off  群里所有消息都回（会吵，也费钱）",
            )
            return

        if mode in ("on", "开", "开启", "true", "1", "yes"):
            trigger.require_mention = True
            await self.bot.send(ev, "好，之后群里必须 @ 我或叫名字我才回。")
        elif mode in ("off", "关", "关闭", "false", "0", "no"):
            trigger.require_mention = False
            await self.bot.send(ev, "行，群里所有消息我都接了（可能有点吵，也费钱）。想改回来发 /at on。")
        else:
            await self.bot.send(ev, "不认识这个参数，用 /at on 或 /at off。")

    async def cmd_sync(self, ev: "MessageEvent", arg: str) -> None:
        """把机器人 QQ 里收藏的表情包同步进来。"""
        if not self.bot.is_admin(ev):
            await self.bot.send(ev, "这个只有管理员能用。")
            return
        await self.bot.send(ev, "开始同步 QQ 收藏表情，稍等一下…")
        added, failed = await self.bot.sync_stickers()
        if added == 0 and failed == 0:
            await self.bot.send(
                ev,
                "没有新表情。\n"
                "在手机上用机器人那个号：长按表情包 → 添加到表情，然后再发一次 /sync。",
            )
        elif added == 0:
            await self.bot.send(ev, f"没拉到新的（失败 {failed} 个），稍后再试或者看日志。")
        else:
            names = "、".join(self.bot.stickers.names[-added:])
            await self.bot.send(
                ev,
                f"同步完成：新增 {added} 个表情包"
                + (f"，失败 {failed} 个" if failed else "")
                + f"。\n它现在有：{names}",
            )

    async def cmd_status(self, ev: "MessageEvent", arg: str) -> None:
        if not self.bot.is_admin(ev):
            await self.bot.send(ev, "这个只有管理员能用。")
            return
        await self.bot.send(ev, self.bot.status_text())
