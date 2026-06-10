"""视觉模型能力判断。"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Literal

from openhachimi_agent.content.prompts import load_system_prompt
from openhachimi_agent.core.config import AppConfig, VisionConfig
from openhachimi_agent.vision.openai_compatible import VisionImagePayload, request_vision

logger = logging.getLogger(__name__)

VisionMode = Literal["none", "direct", "fallback", "unavailable"]

VISION_CAPABILITY_CACHE_TTL_SECONDS = 60 * 60
_VISION_CAPABILITY_CACHE: dict[tuple[str, str, str], tuple[bool, float]] = {}


def _api_key_fingerprint(api_key: str | None) -> str:
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _cache_key(config: AppConfig) -> tuple[str, str, str]:
    return (config.openai_base_url.rstrip("/"), config.model_name, _api_key_fingerprint(config.openai_api_key))


def get_cached_model_vision_support(config: AppConfig) -> bool | None:
    """读取当前进程内缓存的主模型视觉能力，过期则忽略。"""
    cached = _VISION_CAPABILITY_CACHE.get(_cache_key(config))
    if cached is None:
        return None
    supports_vision, cached_at = cached
    if time.monotonic() - cached_at > VISION_CAPABILITY_CACHE_TTL_SECONDS:
        _VISION_CAPABILITY_CACHE.pop(_cache_key(config), None)
        return None
    return supports_vision


def mark_model_vision_support(config: AppConfig, supports_vision: bool) -> None:
    """在当前进程内标记主模型是否支持视觉输入。"""
    _VISION_CAPABILITY_CACHE[_cache_key(config)] = (supports_vision, time.monotonic())


def model_supports_vision(config: AppConfig) -> bool:
    """根据显式配置与已缓存探测结果判断主模型是否支持图片输入。"""
    if config.llm_supports_vision == "true":
        return True
    if config.llm_supports_vision == "false":
        return False

    cached = get_cached_model_vision_support(config)
    return bool(cached)


async def probe_model_supports_vision(config: AppConfig, image: VisionImagePayload) -> bool:
    """用一张真实图片轻量探测 OpenAI 兼容主模型是否支持视觉输入，并缓存结果。"""
    cached = get_cached_model_vision_support(config)
    if cached is not None:
        return cached
    if not config.openai_api_key:
        mark_model_vision_support(config, False)
        return False

    probe_config = VisionConfig(
        enabled=True,
        fallback_enabled=False,
        model=config.model_name,
        base_url=config.openai_base_url,
        api_key=config.openai_api_key,
        detail=config.vision.detail,
        prompt=config.vision.prompt,
        max_images_per_message=1,
        max_image_size_bytes=config.vision.max_image_size_bytes,
    )
    try:
        await request_vision(
            probe_config,
            image,
            load_system_prompt("vision/probe"),
        )
    except Exception as exc:
        logger.info("main model vision probe failed model=%s base_url_configured=%s: %s", config.model_name, bool(config.openai_base_url), exc)
        mark_model_vision_support(config, False)
        return False

    logger.info("main model vision probe succeeded model=%s", config.model_name)
    mark_model_vision_support(config, True)
    return True


def _fallback_available(config: AppConfig) -> bool:
    return bool(config.vision.fallback_enabled and config.vision.model and config.vision.api_key)


def resolve_vision_mode(config: AppConfig, *, has_images: bool) -> VisionMode:
    """决定本轮图片附件的处理模式。auto 未探测时会先按不支持处理。"""
    if not has_images:
        return "none"
    if not config.vision.enabled:
        return "unavailable"
    if model_supports_vision(config):
        return "direct"
    if _fallback_available(config):
        return "fallback"
    return "unavailable"


async def resolve_vision_mode_auto(
    config: AppConfig,
    *,
    has_images: bool,
    probe_image: VisionImagePayload | None = None,
) -> VisionMode:
    """决定图片处理模式；auto 探测失败且没有 fallback 时仍乐观直传给主模型。"""
    if not has_images:
        return "none"
    if not config.vision.enabled:
        return "unavailable"
    if config.llm_supports_vision == "true":
        return "direct"
    if config.llm_supports_vision == "false":
        return "fallback" if _fallback_available(config) else "unavailable"

    if probe_image is not None and await probe_model_supports_vision(config, probe_image):
        return "direct"
    if _fallback_available(config):
        return "fallback"

    logger.info(
        "main model vision probe did not confirm support, but no fallback vision model is configured; trying direct multimodal input model=%s",
        config.model_name,
    )
    return "direct"
