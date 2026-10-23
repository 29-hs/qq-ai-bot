"""OneBot v11 连接层。

支持两种接法：
  forward（正向）：本程序作为客户端，连 NapCat 的 WebSocket 服务器（默认）
  reverse（反向）：本程序开 WebSocket 服务器，等 NapCat 连过来

两种模式对外都只暴露一个 `call_api()`，上层不用关心连法。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, urlparse, urlunparse

import websockets
from websockets.exceptions import ConnectionClosed

from .config import QQConfig

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotError(RuntimeError):
    """调用 OneBot 接口失败。"""


def mask_uri(uri: str) -> str:
    """日志里别把 access_token 打出来。"""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return uri
    if not parsed.query:
        return uri
    return urlunparse(parsed._replace(query="access_token=***"))


class OneBotClient:
    def __init__(
        self,
        cfg: QQConfig,
        on_event: EventHandler,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.logger = logger or logging.getLogger("qqbot.onebot")

        self.self_id: int = cfg.self_id
        self._ws: Any = None
        self._server: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._seq = itertools.count(1)
        self._closing = False
        self._backoff = cfg.reconnect_min

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    async def call_api(self, action: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        """调用 OneBot API（send_msg / get_group_list …），返回 data 字段。"""
        ws = self._ws
        if ws is None:
            raise OneBotError("还没连上 OneBot 服务，消息发不出去")

        echo = str(next(self._seq))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise OneBotError(f"调用 {action} 超时（{timeout:g}s）") from exc
        except ConnectionClosed as exc:
            raise OneBotError(f"调用 {action} 时连接已断开") from exc
        finally:
            self._pending.pop(echo, None)

        if not isinstance(response, dict):
            raise OneBotError(f"{action} 返回了非预期数据：{response!r}")
        status = response.get("status")
        retcode = response.get("retcode", 0)
        if status == "failed" or retcode not in (0, 1):
            reason = response.get("wording") or response.get("message") or response.get("msg") or ""
            raise OneBotError(f"{action} 失败：retcode={retcode} {reason}".strip())
        return response.get("data")

    async def run_forever(self) -> None:
        """连接 → 断开 → 退避重连，循环直到 close()。"""
        while not self._closing:
            try:
                if self.cfg.mode == "reverse":
                    await self._serve()
                else:
                    await self._connect_once()
                self._backoff = self.cfg.reconnect_min
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("连接出错：%s", exc)
            finally:
                self._ws = None

            if self._closing:
                break
            self.logger.info("%.1f 秒后重连…", self._backoff)
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, self.cfg.reconnect_max)

    async def close(self) -> None:
        self._closing = True
        for task in list(self._tasks):
            task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # ------------------------------------------------------------------ #
    # 正向连接
    # ------------------------------------------------------------------ #
    def _build_url(self) -> str:
        url = self.cfg.ws_url
        if self.cfg.access_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={quote(self.cfg.access_token, safe='')}"
        return url

    async def _connect_once(self) -> None:
        url = self._build_url()
        try:
            ws = await websockets.connect(
                url,
                open_timeout=15,
                ping_interval=20,
                ping_timeout=20,
                max_size=None,
                close_timeout=5,
            )
        except OSError as exc:
            raise OneBotError(f"连不上 {mask_uri(url)}：{exc}（NapCat 开了吗？端口对吗？）") from exc

        self._ws = ws
        self._backoff = self.cfg.reconnect_min
        self.logger.info("已连接 OneBot：%s", mask_uri(url))
        # 必须先起读循环，否则 call_api 的响应没人收，会一直等到超时
        reader = asyncio.create_task(self._read_loop(ws))
        try:
            await self._after_connected(ws)
            await reader
            self.logger.warning("与 OneBot 的连接已断开")
        except ConnectionClosed:
            self.logger.warning("与 OneBot 的连接已断开")
        finally:
            reader.cancel()
            if self._ws is ws:
                self._ws = None
            await self._fail_pending("连接已断开")
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # 反向连接（NapCat 连过来）
    # ------------------------------------------------------------------ #
    async def _serve(self) -> None:
        async def handler(websocket: Any, *_: Any) -> None:
            await self._on_reverse_connection(websocket)

        async with websockets.serve(
            handler,
            self.cfg.reverse_host,
            self.cfg.reverse_port,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as server:
            self._server = server
            self.logger.info(
                "反向 WebSocket 服务已启动：ws://%s:%d%s（在 NapCat 里填这个地址）",
                self.cfg.reverse_host,
                self.cfg.reverse_port,
                self.cfg.reverse_path,
            )
            await asyncio.Future()  # 一直跑着

    async def _on_reverse_connection(self, websocket: Any) -> None:
        path = self._connection_path(websocket)
        if not self._check_token(websocket, path):
            self.logger.warning("反向连接鉴权失败，已拒绝：%s", path)
            await websocket.close(code=4001, reason="invalid access token")
            return

        self._ws = websocket
        self._backoff = self.cfg.reconnect_min
        self.logger.info("NapCat 已连入：%s", path)
        reader = asyncio.create_task(self._read_loop(websocket))
        try:
            await self._after_connected(websocket)
            await reader
            self.logger.warning("NapCat 断开了连接")
        except ConnectionClosed:
            pass
        finally:
            reader.cancel()
            if self._ws is websocket:
                self._ws = None
            await self._fail_pending("连接已断开")

    @staticmethod
    def _connection_path(websocket: Any) -> str:
        request = getattr(websocket, "request", None)
        if request is not None and getattr(request, "path", None):
            return str(request.path)
        return str(getattr(websocket, "path", "") or "")

    def _check_token(self, websocket: Any, path: str) -> bool:
        token = self.cfg.access_token
        if not token:
            return True
        query = parse_qs(urlparse(path).query)
        if query.get("access_token", [""])[0] == token:
            return True
        request = getattr(websocket, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None:
            auth = headers.get("Authorization") or ""
            if auth in (f"Bearer {token}", token):
                return True
        return False

    # ------------------------------------------------------------------ #
    # 公共部分
    # ------------------------------------------------------------------ #
    async def _after_connected(self, ws: Any) -> None:
        """连上后问一下登录的是哪个号。"""
        try:
            info = await self.call_api("get_login_info", timeout=10)
        except (OneBotError, asyncio.TimeoutError) as exc:
            self.logger.debug("get_login_info 失败：%s", exc)
            return
        if isinstance(info, dict) and info.get("user_id"):
            try:
                self.self_id = int(info["user_id"])
            except (TypeError, ValueError):
                pass
            self.logger.info("当前登录账号：%s（%s）", info.get("nickname") or "?", self.self_id)

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self.logger.warning("收到无法解析的报文：%.200s", raw)
                continue
            if not isinstance(data, dict):
                continue
            if "post_type" in data:
                task = asyncio.create_task(self._dispatch(data))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                self._resolve(data)

    async def _dispatch(self, event: dict[str, Any]) -> None:
        try:
            await self.on_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.logger.exception("处理事件时出错：%s", str(event)[:200])

    def _resolve(self, data: dict[str, Any]) -> None:
        echo = data.get("echo")
        future = self._pending.pop(echo, None) if echo is not None else None
        if future is None and self._pending:
            # 某些实现不回传 echo，按先进先出配对
            future = self._pending.pop(next(iter(self._pending)))
        if future is not None and not future.done():
            future.set_result(data)

    async def _fail_pending(self, reason: str) -> None:
        while self._pending:
            _, future = self._pending.popitem()
            if not future.done():
                future.set_exception(OneBotError(reason))

    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._ws is not None
