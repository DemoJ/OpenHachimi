# pyrefly: ignore [missing-import]
from types import SimpleNamespace

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from pydantic_ai.exceptions import ModelRetry

_TOOLS_DIR = Path(__file__).parents[2] / "openhachimi_agent" / "tools"
_tools_pkg = types.ModuleType("openhachimi_agent.tools")
_tools_pkg.__path__ = [str(_TOOLS_DIR)]
sys.modules["openhachimi_agent.tools"] = _tools_pkg

_utils_spec = importlib.util.spec_from_file_location(
    "openhachimi_agent.tools.utils",
    _TOOLS_DIR / "utils.py",
)
_utils_module = importlib.util.module_from_spec(_utils_spec)
assert _utils_spec.loader is not None
sys.modules["openhachimi_agent.tools.utils"] = _utils_module
_utils_spec.loader.exec_module(_utils_module)

_filesystem_spec = importlib.util.spec_from_file_location(
    "openhachimi_agent.tools.filesystem",
    _TOOLS_DIR / "filesystem.py",
)
_filesystem_module = importlib.util.module_from_spec(_filesystem_spec)
assert _filesystem_spec.loader is not None
_filesystem_spec.loader.exec_module(_filesystem_module)

read_file = _filesystem_module.read_file
MAX_READ_LINES = _utils_module.MAX_READ_LINES
MAX_READ_LINES_PER_CALL = _utils_module.MAX_READ_LINES_PER_CALL


def make_ctx(base_dir, session_state=None):
    config = SimpleNamespace(attachments_dir=base_dir / ".tmp" / "attachments")
    return SimpleNamespace(deps=SimpleNamespace(base_dir=base_dir, skills_dirs=[], config=config, session_state=session_state or {}))


def write_lines(path, count: int) -> None:
    path.write_text("\n".join(f"line {i}" for i in range(1, count + 1)), encoding="utf-8")


def test_read_file_defaults_to_500_lines(tmp_path):
    file_path = tmp_path / "large.py"
    write_lines(file_path, MAX_READ_LINES + 50)

    result = read_file(make_ctx(tmp_path), "large.py")

    assert result["start_line"] == 1
    assert result["end_line"] == MAX_READ_LINES
    assert result["total_lines"] == MAX_READ_LINES + 50
    assert result["truncated"] is True
    assert result["next_start_line"] == MAX_READ_LINES + 1


def test_read_file_allows_explicit_range_up_to_hard_limit(tmp_path):
    file_path = tmp_path / "large.py"
    write_lines(file_path, MAX_READ_LINES_PER_CALL + 50)

    result = read_file(make_ctx(tmp_path), "large.py", start_line=1, end_line=MAX_READ_LINES_PER_CALL + 50)

    assert result["end_line"] == MAX_READ_LINES_PER_CALL
    assert result["truncated"] is True
    assert result["next_start_line"] == MAX_READ_LINES_PER_CALL + 1


def test_read_file_reports_not_truncated_when_range_reaches_end(tmp_path):
    file_path = tmp_path / "small.py"
    write_lines(file_path, 20)

    result = read_file(make_ctx(tmp_path), "small.py")

    assert result["end_line"] == 20
    assert result["truncated"] is False
    assert result["next_start_line"] is None


def test_read_file_blocks_processed_fallback_image(tmp_path):
    image = tmp_path / ".tmp" / "attachments" / "telegram" / "u1" / "a.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
    key = str(image.resolve()).casefold()
    session_state = {
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

    with pytest.raises(ModelRetry) as exc_info:
        read_file(make_ctx(tmp_path, session_state), ".tmp/attachments/telegram/u1/a.png")

    assert "已由辅助视觉模型成功识别" in str(exc_info.value)
    assert session_state["vision_tool_blocks"][0]["tool"] == "read_file"
