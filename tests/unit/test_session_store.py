"""``storage.session_store.SessionStore`` 单元测试。

覆盖范围:
- 消息 round-trip(空 / 多条 / 含 v3 metadata)
- save 全量覆盖语义(5 条 → 3 条)
- turn_index 严格有序,不依赖 created_at
- 渠道首写定终身、未知渠道兜底到 DEFAULT_CHANNEL 并保留 channel_raw
- list_sessions 顺序与 channel 过滤
- 最新指针按 (role, scope) 隔离
- TODO state round-trip 与 corrupt 兜底
- session_exists 在 save 前后的真假切换
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from openhachimi_agent.storage.session_store import (
    CHANNEL_CODES,
    DEFAULT_CHANNEL,
    SessionStore,
    is_known_channel,
)
from openhachimi_agent.core.identifiers import validate_session_id
from openhachimi_agent.tools.planning import TodoState, TodoTask


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.sqlite3")


def _new_sid() -> str:
    return SessionStore.new_session_id()


def _msgs_pair(user_text: str = "hi", reply_text: str = "ok") -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content=user_text)]),
        ModelResponse(parts=[TextPart(content=reply_text)]),
    ]


# ── 基本常量与工具 ──────────────────────────────────────────────────────────


def test_channel_constants_match_legacy():
    """渠道枚举与历史 session_meta.py 完全一致 —— 上下游对齐的硬不变量。"""
    assert CHANNEL_CODES == ("webui", "cli", "telegram", "weixin")
    assert DEFAULT_CHANNEL == "webui"
    assert is_known_channel("webui") is True
    assert is_known_channel("nonsense") is False
    assert is_known_channel(None) is False


def test_new_session_id_format():
    """sid 必须形如 YYYYMMDD-HHMMSS-{hex8},agent_service.list_sessions 解析依赖此。"""
    sid = SessionStore.new_session_id()
    assert len(sid) == 24  # 8+1+6+1+8
    date_part, time_part, hex_part = sid.split("-")
    assert len(date_part) == 8 and date_part.isdigit()
    assert len(time_part) == 6 and time_part.isdigit()
    assert len(hex_part) == 8 and all(c in "0123456789abcdef" for c in hex_part)


# ── 消息 round-trip ─────────────────────────────────────────────────────────


def test_load_empty_session_returns_resolved_sid(store: SessionStore):
    """没存过的 sid 也能 load,返回空 list —— 复刻旧 load_message_history 的行为。"""
    sid = _new_sid()
    resolved, msgs = store.load_messages("default", sid)
    assert resolved == sid
    assert msgs == []


def test_load_without_session_id_mints_new_when_no_pointer(store: SessionStore):
    """无指针场景下 load(session_id=None) 必须新建 sid 返回空列表(turn.py 依赖此)。"""
    resolved, msgs = store.load_messages("default", None)
    assert msgs == []
    # 仍是合法的 sid 格式
    assert len(resolved) == 24


def test_save_load_roundtrip_simple(store: SessionStore):
    sid = _new_sid()
    msgs = _msgs_pair()
    store.save_messages("default", sid, msgs, channel="webui")
    resolved, loaded = store.load_messages("default", sid)
    assert resolved == sid
    assert len(loaded) == 2
    # parts 文本得保留
    assert isinstance(loaded[0], ModelRequest)
    assert any(getattr(p, "content", None) == "hi" for p in loaded[0].parts)
    assert isinstance(loaded[1], ModelResponse)
    assert any(getattr(p, "content", None) == "ok" for p in loaded[1].parts)


def test_save_load_preserves_v3_metadata(store: SessionStore):
    """ModelRequest.metadata 必须 round-trip —— v3 静态 hash 优化的根基。"""
    sid = _new_sid()
    req = ModelRequest(
        parts=[UserPromptPart(content="real user msg")],
        metadata={
            "openhachimi_user_message": "real user msg",
            "openhachimi_ctx_dynamic": "[时间] xxx",
            "openhachimi_ctx_static_hash": "abcd1234abcd1234",
        },
    )
    rsp = ModelResponse(parts=[TextPart(content="reply")])
    store.save_messages("default", sid, [req, rsp])
    _, loaded = store.load_messages("default", sid)
    meta = loaded[0].metadata
    assert meta == {
        "openhachimi_user_message": "real user msg",
        "openhachimi_ctx_dynamic": "[时间] xxx",
        "openhachimi_ctx_static_hash": "abcd1234abcd1234",
    }


def test_save_appends_not_overwrites(store: SessionStore):
    """append-only:save 5 条 → save 3 条 → load 8 条(不覆盖)。

    原始消息一旦写入永不删 —— 压缩只记元数据,不破坏历史。
    """
    sid = _new_sid()
    long = [
        ModelRequest(parts=[UserPromptPart(content=f"u{i}")]) for i in range(5)
    ]
    start0 = store.save_messages("default", sid, long)
    assert start0 == 0  # 首次写入从 turn_index 0 起
    short = [ModelRequest(parts=[UserPromptPart(content="only-u0")])] * 3
    start1 = store.save_messages("default", sid, short)
    assert start1 == 5  # 追加:从已有 MAX+1 = 5 起
    _, loaded = store.load_messages("default", sid)
    assert len(loaded) == 8  # 5 + 3,不覆盖


def test_save_append_false_overwrites(store: SessionStore):
    """append=False 兼容旧覆盖语义:清空后从 0 重编。"""
    sid = _new_sid()
    long = [ModelRequest(parts=[UserPromptPart(content=f"u{i}")]) for i in range(5)]
    store.save_messages("default", sid, long)
    short = [ModelRequest(parts=[UserPromptPart(content="only-u0")])] * 3
    start = store.save_messages("default", sid, short, append=False)
    assert start == 0
    _, loaded = store.load_messages("default", sid)
    assert len(loaded) == 3


def test_turn_index_preserves_save_order(store: SessionStore):
    """save 时的 list 顺序就是 load 顺序,与 created_at 同秒粒度无关。"""
    sid = _new_sid()
    msgs = []
    for i in range(20):
        msgs.append(ModelRequest(parts=[UserPromptPart(content=f"u{i}")]))
        msgs.append(ModelResponse(parts=[TextPart(content=f"r{i}")]))
    store.save_messages("default", sid, msgs)
    _, loaded = store.load_messages("default", sid)
    assert len(loaded) == 40
    # 抽样检查内容顺序
    contents = []
    for m in loaded:
        for p in m.parts:
            c = getattr(p, "content", None)
            if c:
                contents.append(c)
    assert contents[:6] == ["u0", "r0", "u1", "r1", "u2", "r2"]
    assert contents[-2:] == ["u19", "r19"]


# ── 指针 ────────────────────────────────────────────────────────────────────


def test_latest_pointer_unscoped(store: SessionStore):
    assert store.get_latest_session_id("default") is None
    sid = _new_sid()
    store.set_latest_session_id("default", sid)
    assert store.get_latest_session_id("default") == sid


def test_latest_pointer_per_scope_isolated(store: SessionStore):
    """unscoped 与 ``cli`` / ``webui`` 三个 scope 互不影响。"""
    sid_global = _new_sid()
    sid_cli = _new_sid()
    sid_webui = _new_sid()
    store.set_latest_session_id("default", sid_global, scope=None)
    store.set_latest_session_id("default", sid_cli, scope="cli")
    store.set_latest_session_id("default", sid_webui, scope="webui")
    assert store.get_latest_session_id("default", scope=None) == sid_global
    assert store.get_latest_session_id("default", scope="cli") == sid_cli
    assert store.get_latest_session_id("default", scope="webui") == sid_webui


def test_save_messages_updates_pointer(store: SessionStore):
    """save_messages 必须把 (role, scope) 指针指向当前 sid —— turn.py 依赖此。"""
    sid = _new_sid()
    store.save_messages("default", sid, _msgs_pair(), scope="webui")
    assert store.get_latest_session_id("default", scope="webui") == sid


# ── 渠道 ────────────────────────────────────────────────────────────────────


def test_start_new_session_with_channel_records_row(store: SessionStore):
    sid = store.start_new_session("default", scope="webui", channel="webui", scope_key="webui")
    assert store.session_exists("default", sid)
    assert store.get_channel("default", sid) == "webui"


def test_channel_first_write_wins_via_start_then_save(store: SessionStore):
    """start_new_session 标了 telegram 之后,save_messages 传 webui 不应覆盖。"""
    sid = store.start_new_session("default", channel="telegram")
    store.save_messages("default", sid, _msgs_pair(), channel="webui")
    assert store.get_channel("default", sid) == "telegram"


def test_channel_first_write_wins_via_save_only(store: SessionStore):
    """没经 start_new_session 也成立:首次 save 的 channel 一直保留。"""
    sid = _new_sid()
    store.save_messages("default", sid, _msgs_pair(), channel="weixin")
    store.save_messages("default", sid, _msgs_pair(), channel="webui")
    assert store.get_channel("default", sid) == "weixin"


def test_unknown_channel_falls_back_and_records_raw(store: SessionStore):
    sid = _new_sid()
    store.save_messages("default", sid, _msgs_pair(), channel="bogus-channel")
    # 暴露面:get_channel 返回兜底值
    assert store.get_channel("default", sid) == DEFAULT_CHANNEL
    # 内部记账:channel_raw 保留原值 —— 直接打开 DB 看
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT channel, channel_raw FROM sessions WHERE role=? AND session_id=?",
            ("default", sid),
        ).fetchone()
    assert row["channel"] == DEFAULT_CHANNEL
    assert row["channel_raw"] == "bogus-channel"


def test_get_channel_missing_session_defaults(store: SessionStore):
    assert store.get_channel("default", _new_sid()) == DEFAULT_CHANNEL


# ── list_sessions ───────────────────────────────────────────────────────────


def test_list_sessions_order_and_channel_filter(store: SessionStore):
    """3 个会话跨 2 个渠道 + updated_at DESC 排序 + channel 过滤。"""
    sid_w1 = _new_sid()
    sid_t = _new_sid()
    sid_w2 = _new_sid()
    # 按时间顺序保存,确保 updated_at 单调
    store.save_messages("default", sid_w1, _msgs_pair("a"), channel="webui")
    store.save_messages("default", sid_t, _msgs_pair("b"), channel="telegram")
    store.save_messages("default", sid_w2, _msgs_pair("c"), channel="webui")

    all_ = store.list_sessions("default")
    sids_in_order = [s["session_id"] for s in all_]
    assert sids_in_order == [sid_w2, sid_t, sid_w1]
    assert {s["channel"] for s in all_} == {"webui", "telegram"}
    # size_bytes > 0 一句兜底,具体数额不做强断言
    assert all(s["size_bytes"] > 0 for s in all_)
    # mtime 单调
    mts = [s["mtime"] for s in all_]
    assert mts == sorted(mts, reverse=True)

    only_w = store.list_sessions("default", channel="webui")
    assert [s["session_id"] for s in only_w] == [sid_w2, sid_w1]
    only_t = store.list_sessions("default", channel="telegram")
    assert [s["session_id"] for s in only_t] == [sid_t]


def test_list_sessions_unknown_channel_filter_no_match(store: SessionStore):
    """非法 channel 参数(不在 CHANNEL_CODES 内)应被忽略 → 返回全部,而非空。

    这与 agent_service.list_sessions 的"未知 channel 视为不过滤"语义一致。
    """
    sid = _new_sid()
    store.save_messages("default", sid, _msgs_pair(), channel="webui")
    out = store.list_sessions("default", channel="not-a-real-channel")
    assert len(out) == 1


def test_list_sessions_empty(store: SessionStore):
    assert store.list_sessions("default") == []


# ── 分页 ────────────────────────────────────────────────────────────────────


def test_list_sessions_limit_truncates(store: SessionStore):
    """``limit=N`` 必须只返回前 N 条(按 updated_at DESC)。"""
    sids = []
    for i in range(5):
        sid = _new_sid()
        store.save_messages("default", sid, _msgs_pair(f"u{i}"))
        sids.append(sid)
    # 最后存的在最前
    expected_order = list(reversed(sids))

    out = store.list_sessions("default", limit=3)
    assert [s["session_id"] for s in out] == expected_order[:3]


def test_list_sessions_offset_skips(store: SessionStore):
    """``offset=N`` 必须跳过前 N 条;limit+offset 拼出无重叠的连续两页。"""
    sids = []
    for i in range(5):
        sid = _new_sid()
        store.save_messages("default", sid, _msgs_pair(f"u{i}"))
        sids.append(sid)
    expected_order = list(reversed(sids))

    page1 = store.list_sessions("default", limit=2, offset=0)
    page2 = store.list_sessions("default", limit=2, offset=2)
    page3 = store.list_sessions("default", limit=2, offset=4)
    assert [s["session_id"] for s in page1] == expected_order[0:2]
    assert [s["session_id"] for s in page2] == expected_order[2:4]
    assert [s["session_id"] for s in page3] == expected_order[4:5]


def test_list_sessions_limit_none_means_unbounded(store: SessionStore):
    """``limit=None``(默认)保留旧行为:不分页全量返回。"""
    for i in range(7):
        store.save_messages("default", _new_sid(), _msgs_pair(f"u{i}"))
    out = store.list_sessions("default")  # limit 不传
    assert len(out) == 7
    out2 = store.list_sessions("default", limit=None)
    assert len(out2) == 7


def test_count_sessions_matches_list(store: SessionStore):
    """``count_sessions`` 与无 limit 的 ``list_sessions`` 长度必须一致。"""
    assert store.count_sessions("default") == 0
    for i in range(4):
        store.save_messages("default", _new_sid(), _msgs_pair(f"u{i}"), channel="webui")
    for i in range(2):
        store.save_messages("default", _new_sid(), _msgs_pair(f"t{i}"), channel="telegram")

    assert store.count_sessions("default") == 6
    assert store.count_sessions("default", channel="webui") == 4
    assert store.count_sessions("default", channel="telegram") == 2
    # 未知 channel 等同不过滤(与 list_sessions 行为对齐)
    assert store.count_sessions("default", channel="bogus") == 6


def test_list_sessions_pagination_with_channel_filter(store: SessionStore):
    """channel 过滤 + 分页:total 和分页基线必须只针对该 channel。"""
    webui_sids = []
    for i in range(4):
        sid = _new_sid()
        store.save_messages("default", sid, _msgs_pair(f"w{i}"), channel="webui")
        webui_sids.append(sid)
    # 中间穿插一些 telegram,验证不串
    for i in range(3):
        store.save_messages("default", _new_sid(), _msgs_pair(f"t{i}"), channel="telegram")

    page1 = store.list_sessions("default", channel="webui", limit=2, offset=0)
    page2 = store.list_sessions("default", channel="webui", limit=2, offset=2)
    expected = list(reversed(webui_sids))
    assert [s["session_id"] for s in page1] == expected[0:2]
    assert [s["session_id"] for s in page2] == expected[2:4]
    # 全是 webui,不串
    assert all(s["channel"] == "webui" for s in page1 + page2)


def test_list_sessions_size_bytes_only_for_returned_page(store: SessionStore):
    """size_bytes 必须只针对返回的那几条 sid 算,不应受表外 session 干扰。

    回归:如果实现退回到 LEFT JOIN+GROUP BY+LIMIT(LIMIT 在 GROUP 之后),
    其它会话的消息也会被 JOIN 进来再丢弃,虽然结果对,但性能差。这条用例
    侧面通过断言"分页后 size_bytes 仍正确"覆盖正确性,性能由实现守。
    """
    sid_target = _new_sid()
    store.save_messages("default", sid_target, _msgs_pair("only one"))
    # 多塞几条干扰会话
    for _ in range(5):
        store.save_messages("default", _new_sid(), _msgs_pair("x" * 200))

    page = store.list_sessions("default", limit=1, offset=5)
    # offset=5 拿到的应该是最早那一条 —— 即 sid_target
    assert len(page) == 1
    assert page[0]["session_id"] == sid_target
    assert page[0]["size_bytes"] > 0


# ── session_exists ──────────────────────────────────────────────────────────


def test_session_exists_before_and_after_save(store: SessionStore):
    sid = _new_sid()
    assert store.session_exists("default", sid) is False
    store.save_messages("default", sid, _msgs_pair())
    assert store.session_exists("default", sid) is True


# ── TODO state ──────────────────────────────────────────────────────────────


def test_todo_state_roundtrip(store: SessionStore):
    sid = _new_sid()
    state = TodoState(
        goal="ship the feature",
        invariants=["tests must pass", "lint clean"],
        tool_calls_since_update=5,
        is_active=True,
        tasks={
            1: TodoTask(id=1, description="design", status="done"),
            2: TodoTask(
                id=2,
                description="impl",
                status="in-progress",
                depends_on=[1],
                risk_level="medium",
                evidence="WIP",
            ),
        },
    )
    store.save_todo_state(sid, state)
    loaded = store.load_todo_state(sid)
    assert loaded.goal == "ship the feature"
    assert loaded.invariants == ["tests must pass", "lint clean"]
    assert loaded.tool_calls_since_update == 5
    assert loaded.is_active is True
    assert set(loaded.tasks.keys()) == {1, 2}
    assert loaded.tasks[2].depends_on == [1]
    assert loaded.tasks[2].risk_level == "medium"


def test_todo_state_load_ignores_legacy_allowed_tools(store: SessionStore):
    """旧库可能仍带 ``allowed_tools`` 字段;现在 TodoTask 已经没有这个字段,
    反序列化时应静默丢弃,不抛 TypeError(保持向后兼容)。"""
    sid = _new_sid()
    # 直接造一条带 legacy 字段的 JSON,模拟 v1 时期遗留状态
    legacy_payload = json.dumps({
        "goal": "legacy goal",
        "invariants": [],
        "tool_calls_since_update": 0,
        "is_active": True,
        "tasks": {
            "1": {
                "id": 1,
                "description": "legacy task",
                "status": "pending",
                "allowed_tools": ["write_file", "*"],
                "risk_level": "low",
            },
        },
    }, ensure_ascii=False)
    safe_sid = validate_session_id(sid, allow_legacy=False)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO session_todos (session_id, state_json, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (safe_sid, legacy_payload),
        )

    loaded = store.load_todo_state(sid)
    assert loaded.goal == "legacy goal"
    assert set(loaded.tasks.keys()) == {1}
    assert loaded.tasks[1].description == "legacy task"
    # 关键:没抛 TypeError,且 TodoTask 实例不再带 allowed_tools 属性
    assert not hasattr(loaded.tasks[1], "allowed_tools")


def test_todo_state_missing_returns_empty(store: SessionStore):
    assert store.load_todo_state(_new_sid()) == TodoState()


def test_todo_state_corrupt_returns_empty(store: SessionStore):
    """直接往表里塞坏 JSON,load 应兜底返回空 TodoState(不抛)。"""
    sid = _new_sid()
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.execute(
            "INSERT INTO session_todos (session_id, state_json, updated_at) "
            "VALUES (?, ?, ?)",
            (sid, "{not valid json", "2026-06-24T00:00:00+00:00"),
        )
        conn.commit()
    # 不抛
    assert store.load_todo_state(sid) == TodoState()


def test_todo_state_overwrites(store: SessionStore):
    sid = _new_sid()
    store.save_todo_state(sid, TodoState(goal="v1"))
    store.save_todo_state(sid, TodoState(goal="v2"))
    assert store.load_todo_state(sid).goal == "v2"


# ── schema 落地 ─────────────────────────────────────────────────────────────


def test_schema_creates_all_tables_and_wal(tmp_path: Path):
    db_path = tmp_path / "sessions.sqlite3"
    SessionStore(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert {"sessions", "session_messages", "session_pointers", "session_todos"}.issubset(names)
    assert mode.lower() == "wal"


def test_message_kind_column_records_request_response(store: SessionStore):
    """``session_messages.kind`` 列应正确记录 'request' / 'response'(debug 用)。"""
    sid = _new_sid()
    store.save_messages("default", sid, _msgs_pair())
    with closing(sqlite3.connect(store.db_path)) as conn:
        kinds = [
            row[0]
            for row in conn.execute(
                "SELECT kind FROM session_messages WHERE role=? AND session_id=? ORDER BY turn_index",
                ("default", sid),
            )
        ]
    assert kinds == ["request", "response"]


def test_message_json_round_trip_via_typeadapter(store: SessionStore):
    """字节级:存进表的每行 message_json 拼回数组后,validate_json 不应抛。

    这条用例是 round-trip 健康度的硬证据 —— 不依赖 load_messages 的封装。
    """
    from pydantic_ai import ModelMessagesTypeAdapter

    sid = _new_sid()
    msgs = _msgs_pair()
    store.save_messages("default", sid, msgs)
    with closing(sqlite3.connect(store.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT message_json FROM session_messages "
            "WHERE role=? AND session_id=? ORDER BY turn_index",
            ("default", sid),
        ).fetchall()
    arr_json = b"[" + b",".join(r["message_json"].encode("utf-8") for r in rows) + b"]"
    # 不抛即通过
    parsed = list(ModelMessagesTypeAdapter.validate_json(arr_json))
    assert len(parsed) == 2


# ── 压缩元数据:append-only + session_compressions + load_context ──────────


def _build_history(n: int) -> list:
    """造 n 条 user/response 交替的原始历史。"""
    msgs = []
    for i in range(n):
        msgs.append(ModelRequest(parts=[UserPromptPart(content=f"u{i}")]))
        msgs.append(ModelResponse(parts=[TextPart(content=f"r{i}")]))
    return msgs


def test_record_compression_and_load_context(store: SessionStore):
    """记录压缩元数据后,load_context 返回 head+summary+tail 视图,原始消息仍全在。"""
    sid = _new_sid()
    store.save_messages("default", sid, _build_history(10))  # 20 条
    assert store.current_turn_count("default", sid) == 20

    # 模拟压缩:head=前 4 条(turn 0..3),tail=后 8 条(turn 12..19),中间 turn 4..11 折叠
    comp_id = store.record_compression(
        "default", sid,
        head_end_turn=3, tail_start_turn=12,
        summary="历史压缩摘要:用户讨论了 abc",
        total_len=20,
    )
    assert comp_id == 1

    # 原始 20 条仍在(append-only,没被删)
    _, all_msgs = store.load_messages("default", sid)
    assert len(all_msgs) == 20

    # 视图 = head 4 + tail 8 = 12 条(中间 8 条折叠;summary 合并进 tail 首条 user,
    # 不独立成条 —— assemble_runtime_context 的「避免连续 user 消息」设计)
    _, view = store.load_context("default", sid)
    assert len(view) == 12
    # head 首条仍是首条 user
    assert "u0" in str(view[0].parts[0].content)
    # summary 注入:tail 首条(u6)前缀合并了摘要文本
    view_text = "\n".join(str(getattr(p, "content", "")) for m in view for p in getattr(m, "parts", ()))
    assert "历史压缩摘要" in view_text
    # tail 段 u6..u9 都在视图里
    assert "u6" in view_text and "u9" in view_text
    # 被折叠的中间段 u2..u5 不在视图里(head 只到 u1/tail 从 u6 起)
    assert "u2" not in view_text and "u5" not in view_text


def test_load_context_no_compression_equals_load_messages(store: SessionStore):
    """无压缩元数据时,load_context 等价于 load_messages。"""
    sid = _new_sid()
    store.save_messages("default", sid, _build_history(3))  # 6 条
    _, raw = store.load_messages("default", sid)
    _, view = store.load_context("default", sid)
    assert len(view) == len(raw) == 6


def test_load_context_latest_generation_wins(store: SessionStore):
    """多代压缩:load_context 只取最新一代(compression_id 最大)的边界。"""
    sid = _new_sid()
    store.save_messages("default", sid, _build_history(10))  # 20 条
    # 第一代:折叠 turn 4..11
    store.record_compression("default", sid, 3, 12, "摘要v1", total_len=20)
    # 第二代:在已有 tail 上再压缩 —— 折叠 turn 4..17(把第一代的 tail 的一部分也折进去)
    store.record_compression("default", sid, 3, 18, "摘要v2", total_len=20)
    _, view = store.load_context("default", sid)
    view_text = "\n".join(str(getattr(p, "content", "")) for m in view for p in getattr(m, "parts", ()))
    # 最新一代的摘要胜出
    assert "摘要v2" in view_text
    assert "摘要v1" not in view_text


def test_record_compression_rejects_bad_bounds(store: SessionStore):
    """非法边界(head_end >= tail_start,或越过 total_len)应抛 ValueError。"""
    sid = _new_sid()
    store.save_messages("default", sid, _build_history(2))  # 4 条
    with pytest.raises(ValueError):
        store.record_compression("default", sid, 2, 2, "x", total_len=4)  # head_end == tail_start
    with pytest.raises(ValueError):
        store.record_compression("default", sid, 2, 5, "x", total_len=4)  # tail_start > total_len
    with pytest.raises(ValueError):
        store.record_compression("default", sid, 2, 3, "", total_len=4)  # 空 summary


def test_get_folded_messages_returns_middle_window(store: SessionStore):
    """取折叠区原始消息:返回 head_end+1 .. tail_start-1 区间。"""
    sid = _new_sid()
    store.save_messages("default", sid, _build_history(10))  # turn 0..19
    store.record_compression("default", sid, 3, 12, "sum", total_len=20)
    folded = store.get_folded_messages("default", sid, 1)
    assert len(folded) == 8  # turn 4..11
    folded_text = "\n".join(str(getattr(p, "content", "")) for m in folded for p in getattr(m, "parts", ()))
    # 中间段 u2..u5 对应的回复/请求都在
    assert "u2" in folded_text and "u5" in folded_text
    # head/tail 段不在折叠区
    assert "u0" not in folded_text
    assert "u6" not in folded_text


def test_get_folded_messages_unknown_compression(store: SessionStore):
    """不存在的 compression_id 返回空列表。"""
    sid = _new_sid()
    store.save_messages("default", sid, _build_history(2))
    assert store.get_folded_messages("default", sid, 999) == []


def test_list_compressions_ordered(store: SessionStore):
    """list_compressions 按 compression_id 升序返回。"""
    sid = _new_sid()
    store.save_messages("default", sid, _build_history(10))
    store.record_compression("default", sid, 3, 12, "v1", total_len=20)
    store.record_compression("default", sid, 3, 18, "v2", total_len=20)
    comps = store.list_compressions("default", sid)
    assert [c["compression_id"] for c in comps] == [1, 2]
    assert comps[1]["summary_text"] == "v2"


def test_current_turn_count_after_appends(store: SessionStore):
    """current_turn_count 反映 append-only 累计条数,而非单次 save。"""
    sid = _new_sid()
    store.save_messages("default", sid, _build_history(2))  # 4 条
    assert store.current_turn_count("default", sid) == 4
    store.save_messages("default", sid, _build_history(1))  # 追加 2 条
    assert store.current_turn_count("default", sid) == 6

