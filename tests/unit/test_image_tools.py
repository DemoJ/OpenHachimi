from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.tools.attachments import inspect_image


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def make_ctx(tmp_path, session_state=None):
    config = SimpleNamespace(base_dir=tmp_path, attachments_dir=tmp_path / ".tmp" / "attachments", max_attachment_size_bytes=1024)
    deps = SimpleNamespace(base_dir=tmp_path, skills_dirs=[], config=config, session_state=session_state or {})
    return SimpleNamespace(deps=deps)


def processed_state(tmp_path, image):
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


def test_inspect_image_returns_metadata(tmp_path):
    image = tmp_path / ".tmp" / "attachments" / "telegram" / "u1" / "a.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)

    result = inspect_image(make_ctx(tmp_path), ".tmp/attachments/telegram/u1/a.png")

    assert result["format"] == "PNG"
    assert result["size_bytes"] == len(PNG_BYTES)
    assert result["path"] == ".tmp/attachments/telegram/u1/a.png"

def test_inspect_image_resolves_relative_path_from_cwd(tmp_path):
    image = tmp_path / "tmp" / "doubao-seedream" / "cute_kitten.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)

    result = inspect_image(make_ctx(tmp_path), "cute_kitten.png", cwd="tmp/doubao-seedream")

    assert result["format"] == "PNG"
    assert result["path"] == "tmp/doubao-seedream/cute_kitten.png"


    file_path = tmp_path / "a.txt"
    file_path.write_text("hello", encoding="utf-8")

    with pytest.raises(ModelRetry):
        inspect_image(make_ctx(tmp_path), "a.txt")


def test_inspect_image_blocks_processed_fallback_image(tmp_path):
    image = tmp_path / ".tmp" / "attachments" / "telegram" / "u1" / "a.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    session_state = processed_state(tmp_path, image)

    with pytest.raises(ModelRetry) as exc_info:
        inspect_image(make_ctx(tmp_path, session_state), ".tmp/attachments/telegram/u1/a.png")

    assert "已由辅助视觉模型成功识别" in str(exc_info.value)
    assert "图中有一只猫" in str(exc_info.value)
    assert session_state["vision_tool_blocks"][0]["tool"] == "inspect_image"


def test_inspect_image_allows_direct_image(tmp_path):
    image = tmp_path / ".tmp" / "attachments" / "telegram" / "u1" / "a.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    key = str(image.resolve()).casefold()
    session_state = {
        "vision_attachments": {"att_1": {"attachment_id": "att_1", "mode": "direct", "status": "direct", "summary": "图中有猫。"}},
        "vision_attachment_paths": {key: "att_1"},
    }

    result = inspect_image(make_ctx(tmp_path, session_state), ".tmp/attachments/telegram/u1/a.png")

    assert result["format"] == "PNG"


def test_inspect_image_rejects_path_escape(tmp_path):
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(PNG_BYTES)

    with pytest.raises(ModelRetry):
        inspect_image(make_ctx(tmp_path), "../outside.png")
