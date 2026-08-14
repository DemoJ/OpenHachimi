"""单测：CDP 探测必须能处理 Chrome 的 keep-alive 响应。

新版 Chrome 收到 ``Connection: close`` 请求后仍保持连接不关闭（响应带
Content-Length），探测逻辑必须按 Content-Length 精确读 body 而不是等 EOF，
否则端口已就绪也会被误判为"未就绪"，导致浏览器启动反复失败。
"""

import asyncio
import json

import pytest

from openhachimi_agent.service.browser.cdp import (
    fetch_cdp_websocket_url,
    find_free_port,
    normalize_cdp_websocket_url,
)


class _KeepAliveCdpServer:
    """模拟 Chrome /json/version：返回 Content-Length 响应，但保持连接不关闭。"""

    def __init__(self):
        self._server = None
        self.port = None

    async def start(self):
        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
            except asyncio.IncompleteReadError:
                writer.close()
                return
            payload = json.dumps({
                "Browser": "Chrome/test",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/test",
            }).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=UTF-8\r\n"
                b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
            ) + payload
            writer.write(response)
            await writer.drain()
            # 关键：不 close writer——模拟 Chrome 保持连接打开的行为。
            # 等客户端断开后清理（服务进程退出时也会被回收）。

        self._server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            # 注意：不能 await wait_closed()——Python 3.12 起它会等待所有活跃
            # 连接关闭，而 keep-alive 测试连接不会主动断开，会永久挂起。
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            except asyncio.TimeoutError:
                pass


@pytest.mark.asyncio
async def test_fetch_cdp_websocket_url_parses_keepalive_response():
    """Chrome 保持连接不关闭时，仍能按 Content-Length 正确解析 ws 地址。"""
    server = await _KeepAliveCdpServer().start()
    try:
        ws = await fetch_cdp_websocket_url(server.port, timeout=2.0)
        assert ws == f"ws://127.0.0.1:{server.port}/devtools/browser/test"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_fetch_cdp_websocket_url_returns_none_when_port_closed():
    """端口未监听时返回 None（不应抛异常）。"""
    port = find_free_port()
    ws = await fetch_cdp_websocket_url(port, timeout=0.5)
    assert ws is None


def test_normalize_cdp_websocket_url_fills_missing_port():
    url = normalize_cdp_websocket_url("ws://127.0.0.1/devtools/browser/x", 9222)
    assert url == "ws://127.0.0.1:9222/devtools/browser/x"


def test_normalize_cdp_websocket_url_keeps_existing_port():
    url = normalize_cdp_websocket_url("ws://127.0.0.1:9333/devtools/browser/x", 9222)
    assert url == "ws://127.0.0.1:9333/devtools/browser/x"
