from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry

from openhachimi_agent.tools.attachments import inspect_image


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def make_ctx(tmp_path):
    config = SimpleNamespace(base_dir=tmp_path, attachments_dir=tmp_path / ".tmp" / "attachments", max_attachment_size_bytes=1024)
    deps = SimpleNamespace(base_dir=tmp_path, skills_dirs=[], config=config)
    return SimpleNamespace(deps=deps)


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


def test_inspect_image_rejects_path_escape(tmp_path):
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(PNG_BYTES)

    with pytest.raises(ModelRetry):
        inspect_image(make_ctx(tmp_path), "../outside.png")
