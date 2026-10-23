"""图片识别链路联调：假图床 + 假多模态模型，全程本地，不联网、不花钱。

    python scripts/e2e_vision.py

验证：
  1. 图片能下载、编码成 data:image/...;base64，并以多模态格式发给视觉模型
  2. 表情包会带上「这是一张表情包」的提示
  3. 图片 404 / 拿不到地址时安静降级，不影响聊天
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_DEPS = ROOT / ".deps"
if _DEPS.is_dir():
    sys.path.insert(0, str(_DEPS))

for _key in ("NO_PROXY", "no_proxy"):
    os.environ.setdefault(_key, "127.0.0.1,localhost")

from qqbot.config import VisionConfig  # noqa: E402
from qqbot.events import Segment  # noqa: E402
from qqbot.vision import ImageAnalyst  # noqa: E402

# 1x1 透明 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


class MockServer:
    """一个端口同时当图床（GET /pic.png）和多模态模型（POST /v1/chat/completions）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.captions: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/pic.png"):
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(PNG)))
                    self.end_headers()
                    self.wfile.write(PNG)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                caption = outer.captions.pop(0) if outer.captions else "默认描述"
                payload = {
                    "id": "mock",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": caption},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


async def main() -> int:
    print("=" * 60)
    print("  图片识别联调（本地假服务）")
    print("=" * 60)

    server = MockServer()
    server.start()
    cfg = VisionConfig(
        enabled=True,
        base_url=f"{server.base}/v1",
        api_key="sk-test",
        model="mock-vl",
        timeout=10,
    )
    analyst = ImageAnalyst(cfg)
    try:
        print("\n[1] 普通图片")
        server.captions = ["一只橘猫趴在键盘上"]
        result = await analyst.describe(
            [Segment("image", {"file": "pic.png", "url": f"{server.base}/pic.png"})]
        )
        check(result == ["一只橘猫趴在键盘上"], f"拿到描述：{result[0]}")
        request = server.requests[-1]
        check(request["model"] == "mock-vl", "模型名来自配置")
        parts = request["messages"][0]["content"]
        image_part = next((p for p in parts if p.get("type") == "image_url"), None)
        check(image_part is not None, "以多模态格式发送")
        check(
            image_part["image_url"]["url"].startswith("data:image/png;base64,"),
            "图片被转成 data: URL（不依赖模型去抓图床）",
        )

        print("\n[2] 表情包")
        server.captions = ["黑猫翻白眼，表示无语"]
        result = await analyst.describe(
            [Segment("image", {"file": "s.gif", "url": f"{server.base}/pic.png", "emoji_id": "123"})]
        )
        check(result == ["黑猫翻白眼，表示无语"], "表情包也能描述")
        prompt_text = server.requests[-1]["messages"][0]["content"][0]["text"]
        check("表情包" in prompt_text, "提示里标注了这是表情包")

        print("\n[3] 失败降级")
        result = await analyst.describe([Segment("image", {"url": f"{server.base}/missing.png"})])
        check(result == [""], "图片 404 时返回空描述，不影响聊天")
        result = await analyst.describe([Segment("image", {"file": "no-url.jpg"})])
        check(result == [""], "拿不到图片地址时安静跳过")
    finally:
        await analyst.close()
        server.stop()

    print("\n" + "=" * 60)
    print("✅ 全部通过：下载 → base64 → 多模态请求 → 描述回填，链路正常")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
