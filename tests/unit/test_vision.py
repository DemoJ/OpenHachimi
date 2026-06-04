from dataclasses import replace

import pytest
from pydantic_ai.messages import BinaryContent

from openhachimi_agent.core.config import VisionConfig
from openhachimi_agent.transport.api_models import AttachmentRef
from openhachimi_agent.vision.capabilities import (
    _VISION_CAPABILITY_CACHE,
    mark_model_vision_support,
    model_supports_vision,
    resolve_vision_mode,
)
from openhachimi_agent.vision.preprocess import preprocess_vision_attachments


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _image_attachment(path: str = ".tmp/attachments/telegram/u1/photo.png") -> AttachmentRef:
    return AttachmentRef(
        id="att_1",
        filename="photo.png",
        content_type="image/png",
        size_bytes=len(PNG_BYTES),
        local_path=path,
        source="telegram",
        kind="image",
    )


def _with_vision(config, **kwargs):
    vision = replace(config.vision, **kwargs.pop("vision", {}))
    return replace(config, vision=vision, **kwargs)


def test_model_supports_vision_respects_explicit_config(mock_config):
    assert model_supports_vision(_with_vision(mock_config, llm_supports_vision="true", model_name="text-only")) is True
    assert model_supports_vision(_with_vision(mock_config, llm_supports_vision="false", model_name="gpt-4o")) is False


def test_model_supports_vision_auto_uses_cached_probe_result(mock_config):
    _VISION_CAPABILITY_CACHE.clear()
    config = _with_vision(mock_config, model_name="gpt-4o-mini")

    assert model_supports_vision(config) is False
    mark_model_vision_support(config, True)
    assert model_supports_vision(config) is True
    mark_model_vision_support(config, False)
    assert model_supports_vision(config) is False


def test_resolve_vision_mode_selects_direct_or_fallback(mock_config):
    direct = _with_vision(mock_config, llm_supports_vision="true")
    fallback = _with_vision(mock_config, llm_supports_vision="false", vision={"model": "vision-test", "api_key": "key", "base_url": "http://test"})
    unavailable = _with_vision(mock_config, llm_supports_vision="false", vision={"api_key": None})

    assert resolve_vision_mode(direct, has_images=True) == "direct"
    assert resolve_vision_mode(fallback, has_images=True) == "fallback"
    assert resolve_vision_mode(unavailable, has_images=True) == "unavailable"
    assert resolve_vision_mode(direct, has_images=False) == "none"


@pytest.mark.asyncio
async def test_preprocess_auto_probes_main_model_once_then_direct(mock_config, monkeypatch):
    _VISION_CAPABILITY_CACHE.clear()
    image = mock_config.base_dir / ".tmp" / "attachments" / "telegram" / "u1" / "photo.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    config = _with_vision(mock_config, llm_supports_vision="auto", model_name="hachimi")
    calls = 0

    async def fake_probe_request_vision(_config: VisionConfig, _image, user_prompt: str) -> str:
        nonlocal calls
        calls += 1
        assert _config.model == "hachimi"
        assert "视觉能力探测" in user_prompt
        return "我能看到图片。"

    monkeypatch.setattr("openhachimi_agent.vision.capabilities.request_vision", fake_probe_request_vision)

    first = await preprocess_vision_attachments(config=config, message="看看", attachments=[_image_attachment()])
    second = await preprocess_vision_attachments(config=config, message="再看看", attachments=[_image_attachment()])

    assert first.mode == "direct"
    assert second.mode == "direct"
    assert calls == 1


@pytest.mark.asyncio
async def test_preprocess_auto_probe_failure_uses_fallback_and_caches(mock_config, monkeypatch):
    _VISION_CAPABILITY_CACHE.clear()
    image = mock_config.base_dir / ".tmp" / "attachments" / "telegram" / "u1" / "photo.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    config = _with_vision(
        mock_config,
        llm_supports_vision="auto",
        model_name="hachimi",
        vision={"model": "vision-test", "api_key": "key", "base_url": "http://test"},
    )
    probe_calls = 0
    fallback_calls = 0

    async def fake_probe_request_vision(_config: VisionConfig, _image, _user_prompt: str) -> str:
        nonlocal probe_calls
        probe_calls += 1
        raise RuntimeError("unsupported image_url")

    async def fake_fallback_request_vision(_config: VisionConfig, _image, _user_prompt: str) -> str:
        nonlocal fallback_calls
        fallback_calls += 1
        return "辅助识别结果。"

    monkeypatch.setattr("openhachimi_agent.vision.capabilities.request_vision", fake_probe_request_vision)
    monkeypatch.setattr("openhachimi_agent.vision.preprocess.request_vision", fake_fallback_request_vision)

    first = await preprocess_vision_attachments(config=config, message="看看", attachments=[_image_attachment()])
    second = await preprocess_vision_attachments(config=config, message="再看看", attachments=[_image_attachment()])

    assert first.mode == "fallback"
    assert second.mode == "fallback"
    assert "辅助识别结果" in first.text_prefix
    assert first.consumed_attachment_ids == ["att_1"]
    assert first.attachment_statuses[0].status == "succeeded"
    assert first.attachment_statuses[0].mode == "fallback"
    assert first.attachment_statuses[0].model == "vision-test"
    assert "辅助识别结果" in first.attachment_statuses[0].summary
    assert probe_calls == 1
    assert fallback_calls == 2


@pytest.mark.asyncio
async def test_preprocess_auto_probe_failure_without_fallback_still_tries_direct(mock_config, monkeypatch):
    _VISION_CAPABILITY_CACHE.clear()
    image = mock_config.base_dir / ".tmp" / "attachments" / "telegram" / "u1" / "photo.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    config = _with_vision(mock_config, llm_supports_vision="auto", model_name="hachimi", vision={"api_key": None})
    probe_calls = 0

    async def fake_probe_request_vision(_config: VisionConfig, _image, _user_prompt: str) -> str:
        nonlocal probe_calls
        probe_calls += 1
        raise RuntimeError("unsupported image_url")

    monkeypatch.setattr("openhachimi_agent.vision.capabilities.request_vision", fake_probe_request_vision)

    first = await preprocess_vision_attachments(config=config, message="看看", attachments=[_image_attachment()])
    second = await preprocess_vision_attachments(config=config, message="再看看", attachments=[_image_attachment()])

    assert first.mode == "direct"
    assert second.mode == "direct"
    assert len(first.direct_parts) == 1
    assert isinstance(first.direct_parts[0], BinaryContent)
    assert probe_calls == 1


@pytest.mark.asyncio
async def test_preprocess_direct_builds_binary_parts(mock_config):
    image = mock_config.base_dir / ".tmp" / "attachments" / "telegram" / "u1" / "photo.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    config = _with_vision(mock_config, llm_supports_vision="true")

    result = await preprocess_vision_attachments(config=config, message="看看", attachments=[_image_attachment()])

    assert result.mode == "direct"
    assert result.consumed_attachment_ids == ["att_1"]
    assert len(result.direct_parts) == 1
    assert isinstance(result.direct_parts[0], BinaryContent)
    assert result.direct_parts[0].media_type == "image/png"


@pytest.mark.asyncio
async def test_preprocess_fallback_injects_vision_description(mock_config, monkeypatch):
    image = mock_config.base_dir / ".tmp" / "attachments" / "telegram" / "u1" / "photo.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    config = _with_vision(
        mock_config,
        llm_supports_vision="false",
        vision={"model": "vision-test", "api_key": "key", "base_url": "http://test"},
    )

    async def fake_request_vision(_config: VisionConfig, _image, user_prompt: str) -> str:
        assert "用户随图提出的问题：看看" in user_prompt
        return "图中有一只猫。"

    monkeypatch.setattr("openhachimi_agent.vision.preprocess.request_vision", fake_request_vision)

    result = await preprocess_vision_attachments(config=config, message="看看", attachments=[_image_attachment()])

    assert result.mode == "fallback"
    assert "vision-test" in result.text_prefix
    assert "图中有一只猫" in result.text_prefix
    assert "主模型不能直接看到原图" in result.text_prefix


@pytest.mark.asyncio
async def test_preprocess_unavailable_warns_without_guessing(mock_config):
    image = mock_config.base_dir / ".tmp" / "attachments" / "telegram" / "u1" / "photo.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    config = _with_vision(mock_config, llm_supports_vision="false", vision={"api_key": None})

    result = await preprocess_vision_attachments(config=config, message="看看", attachments=[_image_attachment()])

    assert result.mode == "unavailable"
    assert "系统没有识别图片内容" in result.text_prefix
    assert "请不要假装已经看到了图片" in result.text_prefix
