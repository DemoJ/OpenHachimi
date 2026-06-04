from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.tools.browser import browser_navigate


class FakeBrowserManager:
    def __init__(self):
        self.urls = []

    async def navigate(self, url: str) -> str:
        self.urls.append(url)
        return f"navigated:{url}"


def make_ctx(tmp_path, session_state=None):
    browser_manager = FakeBrowserManager()
    deps = SimpleNamespace(base_dir=tmp_path, skills_dirs=[], browser_manager=browser_manager, session_state=session_state or {})
    return SimpleNamespace(deps=deps), browser_manager


def processed_state(image):
    key = str(image.resolve()).casefold()
    return {
        "vision_attachments": {
            "att_1": {
                "attachment_id": "att_1",
                "mode": "fallback",
                "status": "succeeded",
                "summary": "图中有一只猫。",
                "size_bytes": image.stat().st_size,
            }
        },
        "vision_attachment_paths": {key: "att_1"},
    }


@pytest.mark.asyncio
async def test_browser_navigate_blocks_processed_fallback_file_url(tmp_path):
    image = tmp_path / ".tmp" / "attachments" / "telegram" / "u1" / "a.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
    session_state = processed_state(image)
    ctx, browser_manager = make_ctx(tmp_path, session_state)

    with pytest.raises(ModelRetry) as exc_info:
        await browser_navigate(ctx, image.as_uri())

    assert "已由辅助视觉模型成功识别" in str(exc_info.value)
    assert browser_manager.urls == []
    assert session_state["vision_tool_blocks"][0]["tool"] == "browser_navigate"


@pytest.mark.asyncio
async def test_browser_navigate_allows_regular_https_url(tmp_path):
    ctx, browser_manager = make_ctx(tmp_path)

    result = await browser_navigate(ctx, "https://example.com")

    assert result == "navigated:https://example.com"
    assert browser_manager.urls == ["https://example.com"]
