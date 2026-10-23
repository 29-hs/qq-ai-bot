"""端到端联调脚本：假装一个 NapCat，假装一个 DeepSeek，把整条链路跑通。

不需要真 QQ、不需要真 API Key：
    python scripts/e2e_smoke.py

覆盖：
  1. forward 模式：本程序连假 NapCat → 收私聊/群消息 → 调假模型 → 发回消息
  2. 群里没 @ 不说话、@ 了才说话
  3. 指令不走模型
  4. reverse 模式：假 NapCat 连本程序，校验 token
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 没装依赖时也试试项目内的 .deps（scripts/offline_deps.py 的产物）
_DEPS = ROOT / ".deps"
if _DEPS.is_dir():
    sys.path.insert(0, str(_DEPS))

# 别让本机代理把本地 mock 请求吃了
for key in ("NO_PROXY", "no_proxy"):
    os.environ.setdefault(key, "127.0.0.1,localhost")

import websockets  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from qqbot.bot import ChatBot  # noqa: E402
from qqbot.config import AppConfig, BotConfig, LLMConfig, MemoryConfig, QQConfig  # noqa: E402
from qqbot.llm import LLMClient  # noqa: E402
from qqbot.memory import MemoryStore  # noqa: E402
from qqbot.onebot import OneBotClient  # noqa: E402

BOT_QQ = 10000
REPLY_TEXT = "收到，我是假模型"
LOG = logging.getLogger("e2e")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


# --------------------------------------------------------------------------- #
# 假 DeepSeek（OpenAI 兼容 /chat/completions）
# --------------------------------------------------------------------------- #
class MockLLM:
    def __init__(self, status: int = 200) -> None:
        self.requests: list[dict] = []
        self.status = status
        self.port = free_port()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = {}
                outer.requests.append(body)

                if outer.status != 200:
                    payload = {"error": {"message": "Invalid API key", "type": "authentication_error"}}
                    data = json.dumps(payload).encode("utf-8")
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                payload = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": REPLY_TEXT},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                }
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:  # 安静点
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------- #
# 假 NapCat
# --------------------------------------------------------------------------- #
class FakeNapCat:
    """既能当 WS 服务端（forward 模式用），也能当 WS 客户端（reverse 模式用）。"""

    def __init__(self) -> None:
        self.port = free_port()
        self.connection = None
        self.sent: list[dict] = []
        self.connected = asyncio.Event()

    async def handle_connection(self, websocket) -> None:
        self.connection = websocket
        self.connected.set()
        await self._send_event(
            {
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
                "self_id": BOT_QQ,
                "time": 0,
            }
        )
        async for raw in websocket:
            message = json.loads(raw)
            await self._handle_api_call(websocket, message)
        self.connection = None

    async def _handle_api_call(self, websocket, message: dict) -> None:
        action = message.get("action")
        echo = message.get("echo")
        if action == "get_login_info":
            data = {"user_id": BOT_QQ, "nickname": "测试机器人"}
        elif action in ("send_private_msg", "send_group_msg"):
            self.sent.append(message)
            data = {"message_id": len(self.sent)}
        else:
            data = {}
        await websocket.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": echo}))

    async def _send_event(self, event: dict) -> None:
        if self.connection is not None:
            await self.connection.send(json.dumps(event, ensure_ascii=False))

    async def push_private(self, text: str, user_id: int = 20001) -> None:
        await self._send_event(
            {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": len(self.sent) + 100,
                "user_id": user_id,
                "self_id": BOT_QQ,
                "time": 0,
                "message": [{"type": "text", "data": {"text": text}}],
                "sender": {"user_id": user_id, "nickname": "小红", "card": ""},
            }
        )

    async def push_group(self, segments: list, user_id: int = 20002, group_id: int = 30001) -> None:
        await self._send_event(
            {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": len(self.sent) + 100,
                "user_id": user_id,
                "group_id": group_id,
                "self_id": BOT_QQ,
                "time": 0,
                "message": segments,
                "sender": {"user_id": user_id, "nickname": "李四", "card": "四哥"},
            }
        )

    async def wait_for(self, count: int, timeout: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.sent) < count:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"等了 {timeout}s 只收到 {len(self.sent)} 条回复（期望 {count}）")
            await asyncio.sleep(0.05)


def build_bot(llm_url: str, db_path: Path, qq: QQConfig, logger: logging.Logger) -> tuple[ChatBot, OneBotClient]:
    cfg = AppConfig(
        qq=qq,
        bot=BotConfig(name="小D", cooldown_seconds=0, max_reply_chars=1200),
        llm=LLMConfig(base_url=llm_url, api_key="sk-test", model="mock-model", temperature=0.5),
        memory=MemoryConfig(db_path=str(db_path)),
    )
    memory = MemoryStore(cfg.memory.db_path)
    llm = LLMClient(cfg.llm)
    bot = ChatBot(cfg, llm, memory, logger)
    client = OneBotClient(cfg.qq, bot.handle_event, logger)
    bot.client = client
    return bot, client


# --------------------------------------------------------------------------- #
# 场景 1：forward 模式（最常用）
# --------------------------------------------------------------------------- #
async def scenario_forward(llm: MockLLM, db_path: Path) -> None:
    print("\n[1] forward 模式：机器人主动连 NapCat")
    napcat = FakeNapCat()
    logger = logging.getLogger("e2e.forward")
    logger.setLevel(logging.WARNING)

    bot, client = build_bot(
        f"http://127.0.0.1:{llm.port}",
        db_path,
        QQConfig(mode="forward", ws_url=f"ws://127.0.0.1:{napcat.port}", self_id=0),
        logger,
    )

    server = await websockets.serve(napcat.handle_connection, "127.0.0.1", napcat.port)
    runner = asyncio.create_task(client.run_forever())
    try:
        await asyncio.wait_for(napcat.connected.wait(), timeout=10)
        await asyncio.sleep(0.2)  # 等 get_login_info 回来
        check(bot.self_id == BOT_QQ, f"自动识别 bot 的 QQ 号 = {bot.self_id}")

        await napcat.push_private("你好")
        await napcat.wait_for(1)
        first = napcat.sent[0]
        check(first["action"] == "send_private_msg", "私聊消息走 send_private_msg")
        check(first["params"]["user_id"] == 20001, "发给正确的 QQ 号")
        check(
            first["params"]["message"][-1]["data"]["text"] == REPLY_TEXT,
            "私聊回复内容与模型输出一致",
        )
        check(len(llm.requests) == 1, "模型被调用 1 次")
        messages = llm.requests[0]["messages"]
        check(messages[0]["role"] == "system", "带上 system 人设")
        check(messages[-1]["content"] == "你好", "用户原话完整传给模型")
        check(llm.requests[0]["model"] == "mock-model", "模型名来自配置")

        # 群里不 @ 就不理
        await napcat.push_group([{"type": "text", "data": {"text": "今天吃什么"}}])
        await asyncio.sleep(1.0)
        check(len(napcat.sent) == 1, "群聊没 @ 时不回复")
        check(len(llm.requests) == 1, "群聊没 @ 时不消耗模型调用")

        # @ 了就回，并且回复里 @ 回去
        await napcat.push_group(
            [
                {"type": "at", "data": {"qq": str(BOT_QQ)}},
                {"type": "text", "data": {"text": " 今天几号？"}},
            ]
        )
        await napcat.wait_for(2)
        second = napcat.sent[1]
        check(second["action"] == "send_group_msg", "群聊消息走 send_group_msg")
        check(second["params"]["message"][0]["type"] == "at", "回复开头 @ 对方")
        check(second["params"]["message"][0]["data"]["qq"] == "20002", " @ 的是提问的人")
        check(
            llm.requests[1]["messages"][-1]["content"] == "四哥：今天几号？",
            "群聊会把昵称一起带给模型",
        )

        # 指令不进模型
        await napcat.push_private("/status")
        await napcat.wait_for(3)
        status_text = napcat.sent[2]["params"]["message"][-1]["data"]["text"]
        check("运行状态" in status_text, "/status 能返回运行状态")
        check(len(llm.requests) == 2, "指令没有消耗模型调用")

        # 记忆落库
        stats = bot.memory.stats()
        check(stats["messages"] == 4, f"两轮对话共 4 条记忆（实际 {stats['messages']}）")
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await client.close()
        server.close()
        await server.wait_closed()


# --------------------------------------------------------------------------- #
# 场景 2：reverse 模式
# --------------------------------------------------------------------------- #
async def scenario_reverse(llm: MockLLM, db_path: Path) -> None:
    print("\n[2] reverse 模式：NapCat 反向连机器人")
    port = free_port()
    logger = logging.getLogger("e2e.reverse")
    logger.setLevel(logging.WARNING)

    napcat = FakeNapCat()
    bot, client = build_bot(
        f"http://127.0.0.1:{llm.port}",
        db_path,
        QQConfig(
            mode="reverse",
            reverse_host="127.0.0.1",
            reverse_port=port,
            reverse_path="/onebot",
            access_token="secret-token",
        ),
        logger,
    )

    runner = asyncio.create_task(client.run_forever())
    try:
        # 等反向服务端口起来
        for _ in range(50):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)

        # token 不对：应该被 4001 拒绝
        rejected = False
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/onebot?access_token=wrong") as bad:
                await bad.recv()
        except ConnectionClosed as exc:
            rejected = exc.code == 4001
        check(rejected, "token 不对时以 4001 拒绝连接")

        # token 正确：正常收发
        async with websockets.connect(f"ws://127.0.0.1:{port}/onebot?access_token=secret-token") as ws:
            reader_task = asyncio.create_task(napcat.handle_connection(ws))
            await napcat.connected.wait()
            await napcat.push_private("反向连接测试")
            await napcat.wait_for(1, timeout=10)
            check(napcat.sent[0]["params"]["user_id"] == 20001, "反向连接也能正常回复")
            check(
                napcat.sent[0]["params"]["message"][-1]["data"]["text"] == REPLY_TEXT,
                "回复内容正确",
            )
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await client.close()


# --------------------------------------------------------------------------- #
# 场景 3：模型鉴权失败时也要回人话，而不是崩掉
# --------------------------------------------------------------------------- #
async def scenario_llm_error(db_path: Path) -> None:
    print("\n[3] 模型鉴权失败：回人话，不崩、不记脏数据")
    napcat = FakeNapCat()
    logger = logging.getLogger("e2e.errors")
    logger.setLevel(logging.CRITICAL)

    bad_llm = MockLLM(status=401)
    bad_llm.start()
    server = await websockets.serve(napcat.handle_connection, "127.0.0.1", napcat.port)
    bot, client = build_bot(
        f"http://127.0.0.1:{bad_llm.port}",
        db_path,
        QQConfig(mode="forward", ws_url=f"ws://127.0.0.1:{napcat.port}"),
        logger,
    )
    runner = asyncio.create_task(client.run_forever())
    try:
        await asyncio.wait_for(napcat.connected.wait(), timeout=10)
        await asyncio.sleep(0.2)
        await napcat.push_private("在吗")
        await napcat.wait_for(1, timeout=15)
        text = napcat.sent[0]["params"]["message"][-1]["data"]["text"]
        check("API Key" in text, f"错误提示是给人看的：{text}")
        check(bot.stats["errors"] == 1, "错误计数 +1")
        check(bot.memory.stats()["messages"] == 0, "失败的那轮不写进记忆")
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await client.close()
        server.close()
        await server.wait_closed()
        bad_llm.stop()


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print("=" * 60)
    print("  QQ AI Bot 端到端联调（全部本地 mock，不联网）")
    print("=" * 60)

    llm = MockLLM()
    base_url = llm.start()
    print(f"假模型服务：{base_url}/chat/completions")

    workdir = ROOT / "tests" / ".tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    # 清掉上次跑剩下的数据库，否则记忆条数会对不上
    for pattern in ("e2e_forward.db*", "e2e_reverse.db*", "e2e_error.db*"):
        for stale in workdir.glob(pattern):
            stale.unlink(missing_ok=True)

    try:
        await scenario_forward(llm, workdir / "e2e_forward.db")
        await scenario_reverse(llm, workdir / "e2e_reverse.db")
        await scenario_llm_error(workdir / "e2e_error.db")
    finally:
        llm.stop()

    print("\n" + "=" * 60)
    print("✅ 全部通过：WebSocket 收发、事件解析、模型调用、记忆、指令都正常")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
