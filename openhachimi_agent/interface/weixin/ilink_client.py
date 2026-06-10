"""
Weixin iLink Bot API Client.
Based on the protocol implementation from hermes-agent.
"""

import asyncio
import base64
import json
import logging
import secrets
import struct
import time
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_SEND_TYPING = "ilink/bot/sendtyping"

TYPING_STATUS_START = 1
TYPING_STATUS_CANCEL = 2

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class WeixinClient:
    def __init__(self, base_url: str = ILINK_BASE_URL, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))

    async def close(self):
        await self.client.aclose()

    async def _api_post(self, endpoint: str, payload: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
        body = json.dumps({**payload, "base_info": _base_info()}, ensure_ascii=False, separators=(",", ":"))
        url = f"{self.base_url}/{endpoint}"
        headers = _headers(self.token, body)
        
        response = await self.client.post(url, content=body, headers=headers, timeout=timeout_ms / 1000.0)
        if not response.is_success:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    async def _api_get(self, endpoint: str, timeout_ms: int) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        headers = {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        response = await self.client.get(url, headers=headers, timeout=timeout_ms / 1000.0)
        if not response.is_success:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    async def get_bot_qrcode(self, bot_type: str = "3") -> Dict[str, Any]:
        return await self._api_get(f"{EP_GET_BOT_QR}?bot_type={bot_type}", API_TIMEOUT_MS)

    async def get_qrcode_status(self, qrcode_value: str) -> Dict[str, Any]:
        try:
            return await self._api_get(f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}", LONG_POLL_TIMEOUT_MS)
        except httpx.TimeoutException:
            return {"ret": 1}

    async def get_updates(self, sync_buf: str) -> Dict[str, Any]:
        try:
            return await self._api_post(EP_GET_UPDATES, {"get_updates_buf": sync_buf}, LONG_POLL_TIMEOUT_MS)
        except httpx.TimeoutException:
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}

    async def get_typing_ticket(self, to_user_id: str) -> Optional[str]:
        """获取打字状态票据，用于发送"正在输入"指示器。"""
        try:
            payload = {"ilink_user_id": to_user_id}
            resp = await self._api_post(EP_GET_CONFIG, payload, API_TIMEOUT_MS)
            return resp.get("typing_ticket")
        except Exception as e:
            logger.debug("获取 typing_ticket 失败: %s", e)
            return None

    async def send_typing(self, to_user_id: str, typing_ticket: str, status: int = TYPING_STATUS_START) -> Dict[str, Any]:
        """发送打字状态。status=1 开始，status=2 取消。"""
        payload = {
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        }
        return await self._api_post(EP_SEND_TYPING, payload, API_TIMEOUT_MS)

    async def send_message(self, to_user_id: str, text: str, context_token: Optional[str], client_id: str) -> Dict[str, Any]:
        message: Dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
            
        return await self._api_post(EP_SEND_MESSAGE, {"msg": message}, API_TIMEOUT_MS)
