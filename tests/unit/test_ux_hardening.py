# pyrefly: ignore [missing-import]
"""交互体验加固回归测试(2026-08 体验审查修复轮)。

覆盖:确认回复解析(否定词优先/序号/同义词)、clarify 编号提示、未知斜杠命令、
scheduled 轮次不劫持渠道会话指针、产物持久化回查、历史工具调用摘要渲染。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from openhachimi_agent.service.agent_runtime.command_dispatch import (
    _unknown_command_outcome,
    dispatch_command,
)
from openhachimi_agent.storage.session_store import SessionStore
from openhachimi_agent.tools.clarification import (
    format_choices_hint,
    interpret_confirmation,
    match_user_choice,
)


# ── 确认回复解析 ─────────────────────────────────────────────────────────────


class TestInterpretConfirmation:
    @pytest.mark.parametrize(
        "reply,expected",
        [
            # 否定词一票否决——此前 "不允许" 含 "允许" 子串会被误放行
            ("不允许", False),
            ("不允许执行", False),
            ("拒绝执行", False),
            ("2", False),
            ("取消", False),
            ("算了", False),
            ("no", False),
            # 序号与选项文字
            ("允许执行", True),
            ("允许", True),
            ("1", True),
            ("1.", True),
            ("选项1", True),
            # 同义肯定词——此前 "可以/好" 不含 "允许" 会被误判拒绝
            ("可以", True),
            ("好的", True),
            ("行", True),
            ("ok", True),
            ("YES", True),
            # 无法识别按拒绝处理(安全默认)
            ("随便", False),
            ("", False),
        ],
    )
    def test_interpretation(self, reply, expected):
        assert (
            interpret_confirmation(reply, affirmative="允许执行", negative="拒绝执行")
            == expected
        ), reply

    def test_match_user_choice_number_and_text(self):
        choices = ["允许删除", "拒绝删除"]
        assert match_user_choice("1", choices) == "允许删除"
        assert match_user_choice("2", choices) == "拒绝删除"
        assert match_user_choice("允许删除", choices) == "允许删除"
        assert match_user_choice("不知道", choices) is None

    def test_format_choices_hint(self):
        hint = format_choices_hint(["允许执行", "拒绝执行"])
        assert "1）允许执行" in hint
        assert "2）拒绝执行" in hint


# ── 未知斜杠命令 ─────────────────────────────────────────────────────────────


class _NoopService:
    """dispatch_command 未命中命令时不会触达 service;占位即可。"""


class TestUnknownCommand:
    @pytest.mark.asyncio
    async def test_unknown_slash_command_not_sent_to_llm(self):
        outcome = await dispatch_command(_NoopService(), "/helpp", channel="cli")
        assert outcome is not None
        assert "未知命令" in outcome.message
        assert "/help" in outcome.message

    @pytest.mark.asyncio
    async def test_unknown_command_suggests_close_matches(self):
        outcome = await dispatch_command(_NoopService(), "/rolse", channel="cli")
        assert outcome is not None
        assert "/role" in outcome.message

    def test_unknown_outcome_direct(self):
        outcome = _unknown_command_outcome("/compreess", channel="cli")
        assert "未知命令" in outcome.message

    @pytest.mark.asyncio
    async def test_plain_message_still_returns_none(self):
        assert await dispatch_command(_NoopService(), "普通聊天消息", channel="cli") is None


# ── scheduled 轮次不劫持渠道会话指针 ────────────────────────────────────────


class TestUpdatePointerFlag:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> SessionStore:
        return SessionStore(tmp_path / "sessions.sqlite3")

    def test_pointer_updated_by_default(self, store: SessionStore, tmp_path):
        store.save_messages("default", tmp_path.name + "_s1", [], scope="wx_u1", scope_key="wx_u1")
        assert store.get_latest_session_id("default", "wx_u1") == tmp_path.name + "_s1"

    def test_scheduled_turn_does_not_hijack_pointer(self, store: SessionStore, tmp_path):
        # 渠道用户已有最新指针指向 s_real
        store.save_messages("default", "s_real", [], scope="wx_u1", scope_key="wx_u1")
        # scheduled 轮次带同一 scope 落库,但不更新指针
        store.save_messages(
            "default", "s_schedule_1", [], scope="wx_u1", scope_key="wx_u1",
            update_pointer=False,
        )
        assert store.get_latest_session_id("default", "wx_u1") == "s_real"

    def test_messages_saved_even_without_pointer_update(self, store: SessionStore, tmp_path):
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        msgs = [ModelRequest(parts=[UserPromptPart(content="定时任务输出")])]
        store.save_messages(
            "default", "sched_x", msgs, scope="wx_u9", scope_key="wx_u9",
            update_pointer=False,
        )
        # load_messages 返回 (session_id, messages)
        sid, loaded = store.load_messages("default", "sched_x")
        assert sid == "sched_x"
        assert len(loaded) >= 1
        assert loaded[0].parts[0].content == "定时任务输出"
        assert store.get_latest_session_id("default", "wx_u9") is None


# ── 产物持久化 ───────────────────────────────────────────────────────────────


class TestArtifactPersistence:
    def test_save_and_get_roundtrip(self, tmp_path: Path):
        from openhachimi_agent.transport.api_models import ArtifactRef

        store = SessionStore(tmp_path / "sessions.sqlite3")
        artifact = ArtifactRef(
            id="a1b2c3d4-0000-4000-8000-000000000001",
            filename="report.md",
            content_type="text/markdown",
            size_bytes=10,
            local_path=".tmp/report.md",
            download_url="/artifacts/a1b2c3d4-0000-4000-8000-000000000001/download",
            title=None,
            description=None,
            metadata={},
        )
        store.save_artifacts("default", "sess_a", [artifact])
        loaded = store.get_artifact_by_id(artifact.id)
        assert loaded is not None
        assert loaded.filename == "report.md"

    def test_missing_id_returns_none(self, tmp_path: Path):
        store = SessionStore(tmp_path / "sessions.sqlite3")
        assert store.get_artifact_by_id("nonexistent-id") is None

    def test_malformed_id_returns_none(self, tmp_path: Path):
        store = SessionStore(tmp_path / "sessions.sqlite3")
        assert store.get_artifact_by_id("../etc/passwd") is None


# ── 历史工具调用摘要渲染 ─────────────────────────────────────────────────────


class TestToolCallsInHistory:
    def test_extract_text_parts_renders_tool_calls(self):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

        from openhachimi_agent.service.agent_runtime.session_history import extract_text_parts

        msg = ModelResponse(
            parts=[
                ToolCallPart(tool_name="run_command", args={"command": "npm test"}),
                TextPart(content="测试通过。"),
            ]
        )
        parts = extract_text_parts(SimpleNamespace(), [msg])
        assert len(parts) == 1
        item = parts[0]
        assert item["content"] == "测试通过。"
        tool_calls = item.get("tool_calls")
        assert tool_calls and len(tool_calls) == 1
        assert "npm test" in tool_calls[0]

    def test_tool_call_args_redacted_in_history(self):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

        from openhachimi_agent.service.agent_runtime.session_history import extract_text_parts

        msg = ModelResponse(
            parts=[
                ToolCallPart(tool_name="write_file", args={"path": "x.txt", "api_key": "sk-secret123456789"}),
                TextPart(content="ok"),
            ]
        )
        parts = extract_text_parts(SimpleNamespace(), [msg])
        joined = json.dumps(parts, ensure_ascii=False)
        assert "sk-secret123456789" not in joined


# ── Telegram 不支持媒体过滤器(版本兼容) ─────────────────────────────────────


class TestUnsupportedMediaFilter:
    def _make_update(self, **message_kwargs):
        import datetime as dt

        import telegram

        chat = telegram.Chat(id=1, type="private")
        msg = telegram.Message(message_id=1, date=dt.datetime.now(), chat=chat, **message_kwargs)
        return telegram.Update(update_id=1, message=msg)

    def test_filter_builds_without_attribute_error(self):
        # 回归:filters.STICKER 在 PTB 22.x 不存在,曾让服务启动即崩(status=3)。
        from openhachimi_agent.interface.telegram import _unsupported_media_filter

        assert _unsupported_media_filter() is not None

    def test_voice_video_sticker_match_but_text_not(self):
        from openhachimi_agent.interface.telegram import _unsupported_media_filter
        import telegram

        f = _unsupported_media_filter()
        voice = telegram.Voice(file_id="x", file_unique_id="x", duration=1)
        video = telegram.Video(file_id="x", file_unique_id="x", width=1, height=1, duration=1)
        sticker = telegram.Sticker(
            file_id="x", file_unique_id="x", width=1, height=1,
            is_animated=False, is_video=False, type="regular",
        )
        assert bool(f.check_update(self._make_update(voice=voice)))
        assert bool(f.check_update(self._make_update(video=video)))
        assert bool(f.check_update(self._make_update(sticker=sticker)))
        assert not bool(f.check_update(self._make_update(text="hi")))
