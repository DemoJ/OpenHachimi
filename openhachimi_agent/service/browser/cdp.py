"""CDP 调试协议探测工具（全异步、非阻塞）。

原实现用同步 socket 在事件循环线程里探测 Chrome 调试端口,冷启动或端口未
就绪期间会冻结整个服务的所有协程（HTTP API / agent 会话 / webui 心跳）。
本模块改用 asyncio 原语,单次探测最多耗时 ``timeout`` 秒,不阻塞事件循环。

注意：新版 Chrome 收到 ``Connection: close`` 请求后并不会主动关闭连接
（响应带 Content-Length 且保持 keep-alive），因此探测必须按
Content-Length 精确读取响应体，绝不能依赖 read() 等到 EOF——
否则端口即使就绪也会被判定为"未就绪"。
"""

import asyncio
import json
import socket
from urllib.parse import urlsplit, urlunsplit


def find_free_port() -> int:
    """让系统分配一个当前可用的本地端口，避免固定 9222 端口冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def normalize_cdp_websocket_url(websocket_url: str, port: int) -> str:
    """有些代理/Host 场景下 Chrome 返回的 ws 地址缺端口，这里补回实际 CDP 端口。"""
    parsed = urlsplit(websocket_url)
    if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port is None:
        netloc = f"{parsed.hostname}:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return websocket_url


async def fetch_cdp_websocket_url(port: int, timeout: float = 1.0) -> str | None:
    """异步读取 CDP /json/version 的 webSocketDebuggerUrl;端口未就绪返回 None。

    按 Content-Length 精确读取响应体（Chrome 不主动断连，不能等 EOF）。
    """
    request = (
        "GET /json/version HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        # 读到响应头结束（\r\n\r\n）；Chrome 会一次性发送头与 body，
        # readuntil 只取头，剩余 body 留在缓冲供 readexactly 读取。
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        return await _parse_cdp_response(reader, header, port, timeout)
    except (OSError, asyncio.TimeoutError, ConnectionError,
            asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    finally:
        writer.close()
        # 服务器端（Chrome）可能保持连接不关闭，wait_closed 会一直等 FIN 确认；
        # 必须带短超时兜底，否则探测函数本身会被挂住。
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        except (Exception, asyncio.TimeoutError):
            pass


async def _parse_cdp_response(reader, header: bytes, port: int, timeout: float) -> str | None:
    status_line = header.split(b"\r\n", 1)[0]
    if not (b" 200 " in status_line or status_line.startswith(b"HTTP/1.1 200")):
        return None

    content_length = 0
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                content_length = int(line.split(b":", 1)[1].strip())
            except ValueError:
                return None
    if content_length <= 0:
        return None

    try:
        body = await asyncio.wait_for(reader.readexactly(content_length), timeout=timeout)
    except (OSError, asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
        return None

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    websocket_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(websocket_url, str) or not websocket_url.startswith("ws"):
        return None
    return normalize_cdp_websocket_url(websocket_url, port)