"""核心逻辑单元测试（不需要连 QQ，也不需要真 API Key）。

运行：
    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import base64
import itertools
import logging
import os
import shutil
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from qqbot.bot import ChatBot
from qqbot.config import AppConfig, BotConfig, ConfigError, LLMConfig, MemoryConfig, TriggerConfig, load_config
from qqbot.events import MessageEvent, normalize_message, parse_cq_string, segments_to_text
from qqbot.memory import MemoryStore
from qqbot.stickers import StickerLibrary, sync_custom_faces
from qqbot.utils import escape_cq, split_message, unescape_cq
from qqbot.vision import guess_mime, is_sticker

SELF_ID = 999

# 固定在项目内建临时目录，避免某些沙箱/受限环境里系统临时目录不可写
_TEST_TMP = Path(__file__).resolve().parent / ".tmp"
_counter = itertools.count(1)
_RUN_ID = uuid.uuid4().hex[:8]


def new_dir() -> Path:
    _TEST_TMP.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP / f"{_RUN_ID}-{next(_counter)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# 清掉上一次跑剩下的目录，免得旧数据库影响断言
if _TEST_TMP.is_dir():
    for _stale in _TEST_TMP.glob("*"):
        shutil.rmtree(_stale, ignore_errors=True) if _stale.is_dir() else _stale.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeClient:
    """假装是 OneBot 客户端，只把发出去的消息记下来。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action: str, params: dict | None = None, timeout: float = 30.0):
        self.calls.append((action, params or {}))
        return {"message_id": len(self.calls)}


class FakeFaceClient:
    """只回答 fetch_custom_face 的假客户端。"""

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def call_api(self, action: str, params: dict | None = None, timeout: float = 30.0):
        if action == "fetch_custom_face":
            return list(self.urls)
        return {}


def _fake_describer(name: str):
    async def describe(data: bytes) -> str:
        return name

    return describe


def _start_png_server() -> ThreadingHTTPServer:
    """最小图床：任何 GET 都返回同一张 PNG。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FakeLLM:
    def __init__(self, reply: str = "好呀") -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self.reply

    def usage_summary(self) -> str:
        return "fake"


class FakeVision:
    """假装是视觉模型：按顺序返回预先写好的描述。"""

    def __init__(self, descriptions: list[str]) -> None:
        self.descriptions = descriptions
        self.calls: list[list] = []
        self.onebot = None

    async def describe(self, segments: list) -> list[str]:
        self.calls.append(segments)
        out = self.descriptions[: len(segments)]
        return out + [""] * (len(segments) - len(out))

    async def close(self) -> None:
        pass


def private_payload(text: str | None = None, segments: list | None = None, user_id: int = 10001) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 1,
        "user_id": user_id,
        "self_id": SELF_ID,
        "time": 0,
        "message": segments if segments is not None else [{"type": "text", "data": {"text": text or ""}}],
        "sender": {"user_id": user_id, "nickname": "小明", "card": ""},
    }


def group_payload(
    text: str | None = None,
    segments: list | None = None,
    user_id: int = 10001,
    group_id: int = 20002,
    name: str = "张三",
) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 1,
        "user_id": user_id,
        "group_id": group_id,
        "self_id": SELF_ID,
        "time": 0,
        "message": segments if segments is not None else [{"type": "text", "data": {"text": text or ""}}],
        "sender": {"user_id": user_id, "nickname": name, "card": name},
    }


def at_me_then(text: str) -> list:
    return [
        {"type": "at", "data": {"qq": str(SELF_ID)}},
        {"type": "text", "data": {"text": " " + text}},
    ]


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
class TestMessageParsing(unittest.TestCase):
    def test_parse_cq_string(self) -> None:
        segments = parse_cq_string("你好[CQ:at,qq=123] [CQ:face,id=1]")
        self.assertEqual([s.type for s in segments], ["text", "at", "text", "face"])
        self.assertEqual(segments[1].data["qq"], "123")
        self.assertEqual(segments_to_text(segments), "你好@123 [表情]")

    def test_cq_escape_roundtrip(self) -> None:
        raw = "A&B [CQ:at,qq=all]"
        escaped = escape_cq(raw)
        self.assertNotIn("[CQ:", escaped)
        self.assertEqual(unescape_cq(escaped), raw)

    def test_normalize_array_message(self) -> None:
        segments = normalize_message(
            [
                {"type": "text", "data": {"text": "看图"}},
                {"type": "image", "data": {"file": "a.jpg"}},
            ]
        )
        self.assertEqual(segments_to_text(segments), "看图[图片]")

    def test_event_from_payload(self) -> None:
        event = MessageEvent.from_payload(group_payload(" hi "))
        assert event is not None
        self.assertEqual(event.session_key, "group:20002")
        self.assertEqual(event.sender_name, "张三")
        private = MessageEvent.from_payload(private_payload("你好"))
        assert private is not None
        self.assertEqual(private.session_key, "private:10001")

    def test_non_message_payload(self) -> None:
        self.assertIsNone(MessageEvent.from_payload({"post_type": "notice"}))
        self.assertIsNone(MessageEvent.from_payload({"post_type": "meta_event"}))


class TestTextUtils(unittest.TestCase):
    def test_split_message_respects_limit(self) -> None:
        text = "一二三四五六七八九十。" * 30
        chunks = split_message(text, 100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)
        self.assertEqual("".join(chunks), text)

    def test_split_short_message(self) -> None:
        self.assertEqual(split_message("你好", 100), ["你好"])
        self.assertEqual(split_message("   ", 100), [])


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = new_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env_key = "QQBOT_TEST_KEY_9F3"
        os.environ.pop(self.env_key, None)

    def _write(self, body: str) -> Path:
        path = self.dir / "config.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_env_expansion(self) -> None:
        (self.dir / ".env").write_text(f"{self.env_key}=sk-from-dotenv\n", encoding="utf-8")
        path = self._write(
            f"""
qq:
  mode: forward
  ws_url: "ws://127.0.0.1:3001"
llm:
  api_key: "${{{self.env_key}}}"
  model: deepseek-chat
bot:
  name: 测试机
"""
        )
        cfg = load_config(path)
        self.assertEqual(cfg.llm.api_key, "sk-from-dotenv")
        self.assertEqual(cfg.bot.name, "测试机")
        self.assertEqual(cfg.qq.mode, "forward")
        self.assertTrue(str(cfg.memory.db_path).endswith("memory.db"))

    def test_unknown_key_is_rejected(self) -> None:
        path = self._write(
            """
qq:
  mode: forward
  ws_urll: "ws://127.0.0.1:3001"
llm:
  api_key: sk-test
"""
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("ws_urll", str(ctx.exception))

    def test_missing_api_key_is_rejected(self) -> None:
        path = self._write("llm:\n  api_key: ''\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("api_key", str(ctx.exception))

    def test_bad_mode_is_rejected(self) -> None:
        path = self._write("qq:\n  mode: udp\nllm:\n  api_key: sk-test\n")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_vision_enabled_without_key_is_rejected(self) -> None:
        path = self._write("vision:\n  enabled: true\nllm:\n  api_key: sk-test\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("vision.api_key", str(ctx.exception))

    def test_vision_disabled_by_default(self) -> None:
        path = self._write("llm:\n  api_key: sk-test\n")
        cfg = load_config(path)
        self.assertFalse(cfg.vision.enabled)


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #
class TestMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = new_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = MemoryStore(self.dir / "m.db")

    def test_add_and_history(self) -> None:
        self.store.add_turn("group:1", "张三：你好", "你好呀")
        self.store.add_turn("group:1", "张三：在吗", "在的")
        history = self.store.history("group:1", max_turns=10, max_chars=1000, ttl_hours=24)
        self.assertEqual([m["content"] for m in history], ["张三：你好", "你好呀", "张三：在吗", "在的"])

    def test_turn_limit(self) -> None:
        for i in range(5):
            self.store.add_turn("s", f"u{i}", f"a{i}")
        history = self.store.history("s", max_turns=2, max_chars=1000, ttl_hours=24)
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "u3")

    def test_char_limit_drops_oldest(self) -> None:
        for i in range(4):
            self.store.add_turn("s", "长" * 100 + str(i), "短" * 100)
        history = self.store.history("s", max_turns=10, max_chars=250, ttl_hours=24)
        self.assertLessEqual(sum(len(m["content"]) for m in history), 250)

    def test_reset(self) -> None:
        self.store.add_turn("s", "u", "a")
        self.assertEqual(self.store.reset("s"), 2)
        self.assertEqual(self.store.history("s", ttl_hours=24), [])

    def test_persona_and_switch(self) -> None:
        self.assertIsNone(self.store.get_persona("g"))
        self.assertTrue(self.store.is_enabled("g"))
        self.store.set_persona("g", "你是一只猫")
        self.store.set_enabled("g", False)
        self.assertEqual(self.store.get_persona("g"), "你是一只猫")
        self.assertFalse(self.store.is_enabled("g"))


# --------------------------------------------------------------------------- #
# 机器人：触发规则 / 指令 / 回复
# --------------------------------------------------------------------------- #
class TestBot(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = new_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.bot = self.make_bot()

    def make_bot(self, **bot_kwargs) -> ChatBot:
        cfg = AppConfig(
            bot=BotConfig(name="小D", cooldown_seconds=0, **bot_kwargs),
            llm=LLMConfig(api_key="sk-test", system_prompt="你是{bot_name}。\n{current_info}"),
            memory=MemoryConfig(db_path=str(self.dir / "m.db")),
        )
        bot = ChatBot(cfg, FakeLLM(), MemoryStore(cfg.memory.db_path), logging.getLogger("test"))
        bot.client = FakeClient()
        bot.self_id = SELF_ID
        return bot

    async def test_group_message_without_mention_is_ignored(self) -> None:
        await self.bot.handle_event(group_payload("今天天气不错"))
        self.assertEqual(self.bot.llm.calls, [])
        self.assertEqual(self.bot.client.calls, [])

    async def test_group_at_me_replies(self) -> None:
        await self.bot.handle_event(group_payload(segments=at_me_then("今天天气")))
        self.assertEqual(len(self.bot.llm.calls), 1)
        action, params = self.bot.client.calls[0]
        self.assertEqual(action, "send_group_msg")
        self.assertEqual(params["group_id"], 20002)
        segments = params["message"]
        self.assertEqual(segments[0]["type"], "at")
        self.assertEqual(segments[0]["data"]["qq"], "10001")
        self.assertEqual(segments[-1]["data"]["text"], "好呀")
        # @ 和唤醒词都应该从正文里去掉
        self.assertEqual(self.bot.llm.calls[0][-1]["content"], "张三：今天天气")

    async def test_wake_word_triggers_and_is_stripped(self) -> None:
        await self.bot.handle_event(group_payload("小D 今天吃啥"))
        self.assertEqual(len(self.bot.llm.calls), 1)
        self.assertEqual(self.bot.llm.calls[0][-1]["content"], "张三：今天吃啥")

    async def test_private_always_replies(self) -> None:
        await self.bot.handle_event(private_payload("在吗"))
        self.assertEqual(len(self.bot.llm.calls), 1)
        self.assertEqual(self.bot.client.calls[0][0], "send_private_msg")

    async def test_group_sticker_ignored_by_default(self) -> None:
        sticker = [{"type": "image", "data": {"file": "s.gif", "url": "https://example.com/s.gif"}}]
        await self.bot.handle_event(group_payload(segments=sticker))
        self.assertEqual(self.bot.llm.calls, [])  # 默认没 @ 就不理

    async def test_group_sticker_replies_when_enabled(self) -> None:
        bot = self.make_bot(trigger=TriggerConfig(respond_to_images=True))
        bot.vision = FakeVision(["粉发猫猫挑眉"])
        sticker = [{"type": "image", "data": {"file": "s.gif", "url": "https://example.com/s.gif"}}]
        await bot.handle_event(group_payload(segments=sticker))
        self.assertEqual(len(bot.llm.calls), 1)
        self.assertIn("粉发猫猫挑眉", bot.llm.calls[0][-1]["content"])

    async def test_require_mention_false(self) -> None:
        bot = self.make_bot(trigger=TriggerConfig(require_mention=False))
        await bot.handle_event(group_payload("随便说句话"))
        self.assertEqual(len(bot.llm.calls), 1)

    async def test_history_is_passed_back(self) -> None:
        await self.bot.handle_event(private_payload("我叫小红"))
        await self.bot.handle_event(private_payload("我叫什么？"))
        second = self.bot.llm.calls[1]
        self.assertEqual([m["role"] for m in second], ["system", "user", "assistant", "user"])
        self.assertEqual(second[1]["content"], "我叫小红")

    async def test_system_prompt_has_context(self) -> None:
        await self.bot.handle_event(private_payload("你好"))
        system = self.bot.llm.calls[0][0]["content"]
        self.assertIn("小D", system)
        self.assertIn("QQ 10001", system)

    async def test_reply_is_cq_escaped(self) -> None:
        self.bot.llm.reply = "[CQ:at,qq=all] 冲"
        await self.bot.handle_event(private_payload("hi"))
        text = self.bot.client.calls[0][1]["message"][-1]["data"]["text"]
        self.assertNotIn("[CQ:", text)
        self.assertIn("&#91;CQ:at,qq=all&#93;", text)

    async def test_long_reply_is_split(self) -> None:
        bot = self.make_bot(max_reply_chars=50)
        bot.llm.reply = "这是一句话。" * 30
        await bot.handle_event(private_payload("讲个长的"))
        self.assertGreater(len(bot.client.calls), 1)
        for _, params in bot.client.calls:
            self.assertLessEqual(len(params["message"][-1]["data"]["text"]), 50)

    async def test_reset_command_clears_memory(self) -> None:
        await self.bot.handle_event(private_payload("记住我"))
        self.assertEqual(self.bot.memory.stats()["messages"], 2)
        await self.bot.handle_event(private_payload("/reset"))
        self.assertEqual(self.bot.memory.stats()["messages"], 0)
        # 指令不应该消耗模型调用
        self.assertEqual(len(self.bot.llm.calls), 1)

    async def test_help_command(self) -> None:
        await self.bot.handle_event(private_payload("/help"))
        self.assertEqual(self.bot.llm.calls, [])
        self.assertIn("指令表", self.bot.client.calls[0][1]["message"][-1]["data"]["text"])

    async def test_off_requires_admin(self) -> None:
        await self.bot.handle_event(group_payload(segments=at_me_then("/off")))
        self.assertTrue(self.bot.memory.is_enabled("group:20002"))
        self.assertIn("管理员", self.bot.client.calls[0][1]["message"][-1]["data"]["text"])

    async def test_admin_can_toggle_group(self) -> None:
        bot = self.make_bot(admins=[10001])
        await bot.handle_event(group_payload(segments=at_me_then("/off")))
        self.assertFalse(bot.memory.is_enabled("group:20002"))
        await bot.handle_event(group_payload(segments=at_me_then("还在吗")))
        self.assertEqual(bot.llm.calls, [])  # 关掉后不再回
        await bot.handle_event(group_payload(segments=at_me_then("/on")))
        self.assertTrue(bot.memory.is_enabled("group:20002"))

    async def test_at_command_toggles_require_mention(self) -> None:
        bot = self.make_bot(admins=[10001])
        self.assertTrue(bot.cfg.bot.trigger.require_mention)
        await bot.handle_event(group_payload(segments=at_me_then("/at off")))
        self.assertFalse(bot.cfg.bot.trigger.require_mention)
        # 关掉之后，群里不 @ 也会回
        await bot.handle_event(group_payload("随便聊聊"))
        self.assertEqual(len(bot.llm.calls), 1)
        # 再打开
        await bot.handle_event(group_payload("/at on"))
        self.assertTrue(bot.cfg.bot.trigger.require_mention)

    async def test_denied_group_is_ignored(self) -> None:
        bot = self.make_bot(trigger=TriggerConfig(exclude_groups=[20002]))
        await bot.handle_event(group_payload(segments=at_me_then("你好")))
        self.assertEqual(bot.llm.calls, [])

    async def test_persona_command(self) -> None:
        await self.bot.handle_event(private_payload("/persona 你是一只鹦鹉"))
        self.assertEqual(self.bot.memory.get_persona("private:10001"), "你是一只鹦鹉")

    # ------------------------------------------------------------------ #
    # 图片 / 表情包
    # ------------------------------------------------------------------ #
    async def test_image_only_message_triggers_reply(self) -> None:
        self.bot.vision = FakeVision(["一只橘猫趴在键盘上"])
        payload = private_payload(
            segments=[{"type": "image", "data": {"file": "cat.jpg", "url": "https://example.com/cat.jpg"}}]
        )
        await self.bot.handle_event(payload)
        self.assertEqual(len(self.bot.llm.calls), 1)
        content = self.bot.llm.calls[0][-1]["content"]
        self.assertIn("[图片：一只橘猫趴在键盘上]", content)

    async def test_image_description_merges_with_text(self) -> None:
        self.bot.vision = FakeVision(["一张写着“笑死”的表情包"])
        payload = private_payload(
            segments=[
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"file": "a.jpg", "url": "https://example.com/a.jpg"}},
            ]
        )
        await self.bot.handle_event(payload)
        content = self.bot.llm.calls[0][-1]["content"]
        self.assertEqual(content, "看这个[图片：一张写着“笑死”的表情包]")

    async def test_sticker_uses_sticker_label(self) -> None:
        self.bot.vision = FakeVision(["黑猫翻白眼"])
        payload = private_payload(
            segments=[
                {
                    "type": "image",
                    "data": {"file": "s.gif", "url": "https://example.com/s.gif", "emoji_id": "123"},
                }
            ]
        )
        await self.bot.handle_event(payload)
        self.assertIn("[表情包：黑猫翻白眼]", self.bot.llm.calls[0][-1]["content"])

    async def test_placeholder_marked_when_description_fails(self) -> None:
        self.bot.vision = FakeVision([""])  # 视觉模型没给出描述
        payload = private_payload(
            segments=[{"type": "image", "data": {"file": "x.jpg", "url": "https://example.com/x.jpg"}}]
        )
        await self.bot.handle_event(payload)
        self.assertIn("[图片：内容未能识别]", self.bot.llm.calls[0][-1]["content"])

    async def test_no_vision_keeps_placeholder(self) -> None:
        payload = private_payload(
            segments=[{"type": "image", "data": {"file": "x.jpg", "url": "https://example.com/x.jpg"}}]
        )
        await self.bot.handle_event(payload)
        self.assertEqual(self.bot.llm.calls[0][-1]["content"], "[图片]")


class TestVisionHelpers(unittest.TestCase):
    def test_guess_mime(self) -> None:
        self.assertEqual(guess_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(guess_mime(b"\x89PNG\r\n\x1a\n"), "image/png")
        self.assertEqual(guess_mime(b"GIF89a...."), "image/gif")
        self.assertEqual(guess_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")

    def test_is_sticker(self) -> None:
        from qqbot.events import Segment

        self.assertTrue(is_sticker(Segment("mface", {"summary": "[表情]"})))
        self.assertTrue(is_sticker(Segment("image", {"emoji_id": "123"})))
        self.assertFalse(is_sticker(Segment("image", {"file": "a.jpg"})))


# --------------------------------------------------------------------------- #
# 表情包
# --------------------------------------------------------------------------- #
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestStickerLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = new_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "开心.png").write_bytes(_PNG)
        (self.dir / "无语.gif").write_bytes(b"GIF89a")
        (self.dir / "说明.txt").write_text("这不是图", encoding="utf-8")
        self.lib = StickerLibrary(self.dir)

    def test_scans_only_images(self) -> None:
        self.assertEqual(sorted(self.lib.names), ["开心", "无语"])

    def test_find_matches_loosely(self) -> None:
        self.assertIsNotNone(self.lib.find("开心"))
        self.assertIsNotNone(self.lib.find("开心一点"))  # 包含匹配
        self.assertIsNone(self.lib.find("震惊"))

    def test_extract_removes_tags(self) -> None:
        text, images = self.lib.extract("哈哈[图:开心]你赢了")
        self.assertEqual(text, "哈哈你赢了")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0], _PNG)

    def test_unknown_tag_is_dropped(self) -> None:
        text, images = self.lib.extract("哦[图:不存在]")
        self.assertEqual(text, "哦")
        self.assertEqual(images, [])

    def test_segment_is_base64(self) -> None:
        segment = StickerLibrary.to_segment(_PNG)
        self.assertEqual(segment["type"], "image")
        self.assertTrue(segment["data"]["file"].startswith("base64://"))


class TestStickerSending(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dir = new_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        cfg = AppConfig(
            bot=BotConfig(name="小D", cooldown_seconds=0, stickers_dir=str(self.dir)),
            llm=LLMConfig(api_key="sk-test"),
            memory=MemoryConfig(db_path=str(self.dir / "m.db")),
        )
        self.bot = ChatBot(cfg, FakeLLM(), MemoryStore(cfg.memory.db_path), logging.getLogger("test"))
        self.bot.client = FakeClient()
        self.bot.self_id = SELF_ID
        (self.dir / "开心.png").write_bytes(_PNG)
        self.bot.stickers.reload()

    async def test_reply_with_sticker_sends_image_segment(self) -> None:
        self.bot.llm.reply = "好呀[图:开心]"
        await self.bot.handle_event(private_payload("讲个笑话"))
        calls = self.bot.client.calls
        self.assertEqual(len(calls), 2)  # 一条文字 + 一条图片
        self.assertEqual(calls[0][1]["message"][-1]["data"]["text"], "好呀")
        self.assertEqual(calls[1][1]["message"][-1]["type"], "image")
        self.assertTrue(calls[1][1]["message"][-1]["data"]["file"].startswith("base64://"))

    async def test_unknown_sticker_is_ignored(self) -> None:
        self.bot.llm.reply = "好呀[图:不存在]"
        await self.bot.handle_event(private_payload("讲个笑话"))
        self.assertEqual(len(self.bot.client.calls), 1)


class TestCustomFaceSync(unittest.IsolatedAsyncioTestCase):
    """QQ 收藏表情同步：假客户端 + 本地图床。"""

    def setUp(self) -> None:
        self.dir = new_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.lib = StickerLibrary(self.dir)
        self.server = _start_png_server()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/face.png"

    async def test_download_and_name(self) -> None:
        client = FakeFaceClient([self.url])
        added, failed = await sync_custom_faces(
            self.lib, client, describer=_fake_describer("猫猫大笑")
        )
        self.assertEqual((added, failed), (1, 0))
        self.assertIn("猫猫大笑", self.lib.names)

    async def test_second_sync_skips_duplicates(self) -> None:
        client = FakeFaceClient([self.url])
        await sync_custom_faces(self.lib, client, describer=_fake_describer("无语"))
        added, _ = await sync_custom_faces(self.lib, client, describer=_fake_describer("无语"))
        self.assertEqual(added, 0)

    async def test_bad_url_counts_as_failure(self) -> None:
        client = FakeFaceClient(["http://127.0.0.1:1/nope.png"])
        added, failed = await sync_custom_faces(self.lib, client, describer=_fake_describer("x"))
        self.assertEqual(added, 0)
        self.assertEqual(failed, 1)


class TestInstanceLock(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = new_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_second_instance_is_rejected(self) -> None:
        from qqbot.lock import InstanceLock

        first = InstanceLock(self.dir / "bot.lock")
        second = InstanceLock(self.dir / "bot.lock")
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())  # 第二个必须被挡住
        first.release()
        self.assertTrue(second.acquire())  # 释放后可以再拿
        second.release()


if __name__ == "__main__":
    unittest.main()
