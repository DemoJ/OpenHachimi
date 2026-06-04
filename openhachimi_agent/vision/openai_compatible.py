"""OpenAI 兼容视觉模型调用。"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openhachimi_agent.core.config import VisionConfig


class VisionModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisionImagePayload:
    path: Path
    content_type: str


def image_to_data_url(path: Path, content_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise VisionModelError("视觉模型响应缺少 choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise VisionModelError("视觉模型响应 choices[0] 格式无效")
    message = first.get("message")
    if not isinstance(message, dict):
        raise VisionModelError("视觉模型响应缺少 message")
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "\n".join(parts).strip()
        if text:
            return text
    raise VisionModelError("视觉模型响应缺少文本内容")


def _request_vision_sync(config: VisionConfig, image: VisionImagePayload, user_prompt: str) -> str:
    if not config.base_url:
        raise VisionModelError("未配置 vision.base_url，且 llm.base_url 为空")
    if not config.api_key:
        raise VisionModelError("未配置 vision.api_key，且 llm.api_key 为空")

    image_url: dict[str, str] = {"url": image_to_data_url(image.path, image.content_type)}
    if config.detail:
        image_url["detail"] = config.detail

    body = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": "你是一个可靠的图片识别助手。只描述图片中能观察到的内容，不要编造。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": image_url},
                ],
            },
        ],
    }
    request = urllib.request.Request(
        _chat_completions_url(config.base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")[:1000]
        raise VisionModelError(f"视觉模型请求失败：HTTP {exc.code} {error_text}") from exc
    except urllib.error.URLError as exc:
        raise VisionModelError(f"视觉模型请求失败：{exc.reason}") from exc

    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise VisionModelError("视觉模型响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise VisionModelError("视觉模型响应格式无效")
    return _extract_content(payload)


async def request_vision(config: VisionConfig, image: VisionImagePayload, user_prompt: str) -> str:
    return await asyncio.to_thread(_request_vision_sync, config, image, user_prompt)
