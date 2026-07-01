"""WebUI 会话历史视图。

`AgentService` 持有 `session_store` 与静态上下文池,这里提供接收 `service` 整体
作参数的纯函数,负责:把 ``ModelMessage`` 序列抽成展示用文本结构、列出/加载/删除
会话、按折叠区间还原消息流。`AgentService` 内对应方法退化为薄壳。
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

from openhachimi_agent.core.identifiers import validate_latest_scope
from openhachimi_agent.transport.api_models import CommandResponse


logger = logging.getLogger(__name__)


# WebUI 展示历史会话时需要的 metadata 键名。
#
# ``openhachimi_user_message``  —— 用户原始输入（turn.py 持久化时写入）。
# ``openhachimi_ctx_dynamic``   —— 本轮 system prompt 末尾的动态段
#     (时间/TaskFrame/记忆/技能),由 turn.py 持久化时写入。
# ``openhachimi_ctx_static_hash`` —— 稳定段(base/executor/role/config/tools)
#     的短哈希;完整文本经 _resolve_static_context 在内存池中查表回填。
# ``openhachimi_system_context``  —— 旧版本(v2)的整段快照,读取时作为兜底。
USER_MESSAGE_METADATA_KEY = "openhachimi_user_message"
CTX_DYNAMIC_METADATA_KEY = "openhachimi_ctx_dynamic"
CTX_STATIC_HASH_METADATA_KEY = "openhachimi_ctx_static_hash"
SYSTEM_CONTEXT_METADATA_KEY = "openhachimi_system_context"  # legacy


def summary_excerpt(summary: str, limit: int = 160) -> str:
    """从摘要全文取首段做预览,供折叠占位条展示。按空行取首段 + 截断。"""
    if not summary:
        return ""
    first_para = summary.split("\n\n", 1)[0].strip()
    if len(first_para) <= limit:
        return first_para
    return first_para[:limit].rstrip() + "…"


def extract_text_parts(
    service, messages: list[ModelMessage], role: str | None = None,
) -> list[dict]:
    """将 ``pydantic_ai.messages.ModelMessage`` 列表转为简单的 ``{role, content, prefix, timestamp, tokens}`` 结构。

    遍历 ``ModelRequest``（用户消息）中的 ``UserPromptPart``，
    以及 ``ModelResponse``（Agent 回复）中的 ``TextPart``，
    忽略工具调用、工具返回等中间环节。

    user 消息额外返回 ``prefix`` 字段（运行时注入的可折叠上下文）。读取优先级：
      1. v3:同时读 ``openhachimi_ctx_dynamic`` + ``openhachimi_ctx_static_hash``,
         由 ``service._resolve_static_context(role, hash)`` 查池/重建静态段;
         prefix = static + "\\n\\n" + dynamic(与实际发给模型的顺序一致)。
      2. v2 兜底:``openhachimi_system_context`` 整段(老会话历史)。
      3. v1 兜底:从 UserPromptPart 全文中拆出 ``openhachimi_user_message`` 之前的前缀。
      4. 兜底:prefix = ""。

    每条消息都会带上 ISO-8601 ``timestamp``：user 取 ``ModelRequest.timestamp``，
    assistant 取 ``ModelResponse.timestamp``，找不到时为 None。
    assistant 消息额外返回 ``tokens={"input", "output", "total", "cache_read"}``
    (来自 ``ModelResponse.usage``);旧会话 / 缺失 usage 时为 None。
    """
    from pydantic_ai.messages import TextPart, UserPromptPart

    result: list[dict] = []
    for msg in messages:
        msg_ts = getattr(msg, "timestamp", None)
        ts_iso = msg_ts.isoformat() if msg_ts is not None else None
        if isinstance(msg, ModelRequest):
            metadata = getattr(msg, "metadata", None) or {}
            if not isinstance(metadata, dict):
                metadata = {}
            user_message_meta = metadata.get(USER_MESSAGE_METADATA_KEY)
            dynamic_meta = metadata.get(CTX_DYNAMIC_METADATA_KEY)
            static_hash_meta = metadata.get(CTX_STATIC_HASH_METADATA_KEY)
            legacy_system_context_meta = metadata.get(SYSTEM_CONTEXT_METADATA_KEY)

            for part in getattr(msg, "parts", ()):
                if isinstance(part, UserPromptPart):
                    raw = part.content
                    if isinstance(raw, str):
                        text = raw
                    else:
                        # content 可能是 Sequence[UserContent]，取所有文本片段
                        text = " ".join(str(x) for x in raw if isinstance(x, str))
                    if not text.strip():
                        continue

                    # 优先采用 UserPromptPart 自带 timestamp（更接近"用户实际发送时刻"）
                    part_ts = getattr(part, "timestamp", None)
                    item_ts = part_ts.isoformat() if part_ts is not None else ts_iso

                    # ---- v3 路径：分段 metadata + 静态池回填 ----
                    # 展示顺序与实际发给模型的 system prompt 顺序保持一致:
                    # 静态段(base/role/main_agent/config/工具清单)在前,
                    # 动态段(时间/记忆/中间产物/Skills)在后。
                    prefix_v3 = ""
                    if isinstance(static_hash_meta, str) and static_hash_meta:
                        static_text = service._resolve_static_context(role, static_hash_meta)
                        if static_text:
                            prefix_v3 = static_text
                    if isinstance(dynamic_meta, str) and dynamic_meta.strip():
                        prefix_v3 = f"{prefix_v3}\n\n{dynamic_meta.strip()}" if prefix_v3 else dynamic_meta.strip()
                    if prefix_v3:
                        if isinstance(user_message_meta, str) and user_message_meta:
                            content = user_message_meta
                        else:
                            content = text.strip()
                        result.append({
                            "role": "user", "content": content, "prefix": prefix_v3,
                            "timestamp": item_ts, "tokens": None,
                        })
                        break

                    # ---- v2 路径：旧整段快照(老会话) ----
                    if isinstance(legacy_system_context_meta, str) and legacy_system_context_meta.strip():
                        prefix = legacy_system_context_meta.strip()
                        if isinstance(user_message_meta, str) and user_message_meta:
                            content = user_message_meta
                        else:
                            content = text.strip()
                        result.append({
                            "role": "user", "content": content, "prefix": prefix,
                            "timestamp": item_ts, "tokens": None,
                        })
                        break

                    # ---- 旧路径：有 user_message 但无 system_context 快照 ----
                    if isinstance(user_message_meta, str) and user_message_meta:
                        user_msg = user_message_meta
                        stripped = text.rstrip()
                        if stripped.endswith(user_msg):
                            prefix = stripped[: -len(user_msg)].rstrip("\n").rstrip()
                        else:
                            logger.debug(
                                "user_msg not at end of UserPromptPart; using metadata only "
                                "user_msg_chars=%d prompt_chars=%d prompt_preview=%r",
                                len(user_msg),
                                len(stripped),
                                stripped[:120],
                            )
                            prefix = ""
                        result.append({
                            "role": "user", "content": user_msg, "prefix": prefix,
                            "timestamp": item_ts, "tokens": None,
                        })
                        break

                    # ---- 兜底：旧会话无 metadata，整段显示 ----
                    result.append({
                        "role": "user", "content": text.strip(), "prefix": "",
                        "timestamp": item_ts, "tokens": None,
                    })
                    break
        elif isinstance(msg, ModelResponse):
            # 把 ModelResponse.usage 抽成 {input, output, total, cache_read}。
            # pydantic_ai 的 RequestUsage 字段为 input_tokens / output_tokens /
            # cache_read_tokens / cache_write_tokens 等。展示层关心:
            # - input/output:本轮净读写
            # - total:输入+输出(不含 cache 复算)
            # - cache_read:缓存命中(KV cache hit),反映省钱/提速能力
            # cache_write 不展示(噪声大,模型缓存调度对用户透明)。
            usage = getattr(msg, "usage", None)
            tokens_dict: dict[str, int] | None = None
            if usage is not None:
                try:
                    input_t = int(getattr(usage, "input_tokens", 0) or 0)
                    output_t = int(getattr(usage, "output_tokens", 0) or 0)
                    cache_read_t = int(getattr(usage, "cache_read_tokens", 0) or 0)
                    if input_t or output_t:
                        tokens_dict = {
                            "input": input_t,
                            "output": output_t,
                            "total": input_t + output_t,
                            "cache_read": cache_read_t,
                        }
                except (TypeError, ValueError):
                    tokens_dict = None
            for part in getattr(msg, "parts", ()):
                if isinstance(part, TextPart):
                    text = str(part.content).strip()
                    if text:
                        result.append({
                            "role": "assistant", "content": text, "prefix": "",
                            "timestamp": ts_iso, "tokens": tokens_dict,
                        })
    return result


def list_sessions(
    service,
    role_name: str | None = None,
    *,
    with_preview: bool = True,
    channel: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    """列出指定角色的所有历史会话(支持分页)。

    ``channel`` 非空时按渠道过滤(SessionStore 内部对未知渠道做 DEFAULT_CHANNEL 兜底)。
    ``limit/offset`` 是 offset-based 分页,``limit=None`` 时不分页(老调用方兜底)。
    返回 ``{"role": str, "sessions": [...], "total": int, "limit": int|None, "offset": int}`` —— 前端用
    ``total`` 判定 ``hasMore``;切渠道 / 切角色时前端重置 offset 重新拉第一页。
    """
    role = service._normalize_role(role_name)
    service._validate_role_exists(role)
    # store 端已经做了渠道过滤;传非法 channel 会被忽略(返回全部),与旧语义一致。
    raw = service.session_store.list_sessions(role, channel=channel, limit=limit, offset=offset)
    total = service.session_store.count_sessions(role, channel=channel)

    sessions: list[dict] = []
    for s in raw:
        sid = s["session_id"]
        created_at: str | None = None
        # 解析 session_id 前缀 "YYYYMMDD-HHMMSS-..." 还原 created_at(展示用)。
        if "-" in sid:
            try:
                dt = datetime.strptime(sid[:15], "%Y%m%d-%H%M%S")
                created_at = dt.isoformat()
            except (ValueError, IndexError):
                pass

        preview = ""
        msg_count = 0
        if with_preview:
            try:
                _, msgs = service.session_store.load_messages(role, sid)
            except Exception:
                msgs = []
            msg_count = len(msgs)
            parts = extract_text_parts(service, msgs, role=role)
            user_msgs = [p["content"] for p in parts if p["role"] == "user"]
            if user_msgs:
                preview = user_msgs[0][:80]

        sessions.append({
            "session_id": sid,
            "role": role,
            "created_at": created_at,
            "mtime": s["mtime"],
            "preview": preview,
            "message_count": msg_count,
            "channel": s.get("channel", "webui"),
        })

    return {
        "role": role,
        "sessions": sessions,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def load_session(service, role_name: str | None = None, session_id: str | None = None, latest_scope: str | None = None) -> CommandResponse:
    """切换到指定会话——只做存在性校验,不再写全局 ``latest``。

    旧实现会把目标 session_id 写到 ``latest_scope`` 对应的 latest 指针,这是
    跨渠道串号的根因:WebUI 在侧栏点开某条 IM 会话查看时,会污染全局 latest,
    导致下一次 WebUI 不带 ``session_id`` 发消息时把消息追加到 IM 的 .json。
    现在 load_session 只检查目标会话存在;前端把它写入 currentSessionId,
    后续发送时显式带上 session_id 即可。
    """
    role = service._normalize_role(role_name)
    service._validate_role_exists(role)
    resolved_session_id = service._normalize_session_id(session_id)
    if not resolved_session_id:
        raise ValueError("session_id 不能为空，请指定要加载的会话")

    # 触发存在性校验:不存在时让上层把错误透传给前端
    if not service.session_store.session_exists(role, resolved_session_id):
        raise FileNotFoundError(f"会话不存在: {resolved_session_id}")
    _ = validate_latest_scope(latest_scope)
    logger.info("loaded session role=%s session_id=%s", role, resolved_session_id)
    return CommandResponse(
        message="已切换到指定会话。",
        role=role,
        session_id=resolved_session_id,
    )


def delete_session(service, role_name: str | None = None, session_id: str | None = None) -> CommandResponse:
    """删除指定会话 —— 消息历史、TODO、最新指针一并清除。

    与 ``load_session`` 走相同的 role/session_id 校验;不存在时透传
    ``FileNotFoundError`` 给 HTTP 层返回 400。返回的 ``session_id`` 仍是被删的
    id,前端据此比对本地 ``currentSessionId`` 决定是否清空到空白页。
    """
    role = service._normalize_role(role_name)
    service._validate_role_exists(role)
    resolved_session_id = service._normalize_session_id(session_id)
    if not resolved_session_id:
        raise ValueError("session_id 不能为空，请指定要删除的会话")
    if not service.session_store.session_exists(role, resolved_session_id):
        raise FileNotFoundError(f"会话不存在: {resolved_session_id}")
    service.session_store.delete_session(role, resolved_session_id)
    logger.info("deleted session role=%s session_id=%s", role, resolved_session_id)
    return CommandResponse(
        message="会话已删除。",
        role=role,
        session_id=resolved_session_id,
    )


def get_session_messages(service, role_name: str | None = None, session_id: str | None = None, limit: int | None = None, before_turn: int | None = None) -> dict:
    role = service._normalize_role(role_name)
    service._validate_role_exists(role)
    resolved_session_id = service._normalize_session_id(session_id)
    if not resolved_session_id:
        raise ValueError("session_id 不能为空")

    # 展示用「完整原始消息序列 + 折叠占位条」:append-only 后原始消息永不删,
    # session_compressions 记录每次压缩的折叠区间。遍历 turn_index,落入折叠区间
    # [head_end_turn+1, tail_start_turn-1] 的消息跳过,在区间起始位置插一个 fold 占位条;
    # summary 不进消息流(用户点展开时另调 get_folded_messages 取回原始消息)。
    compressions = service.session_store.list_compressions(role, resolved_session_id)
    # 区间按起始 turn_index 排,便于遍历时一次性建立「当前是否在折叠区间」状态
    fold_ranges = [
        {
            "compression_id": c["compression_id"],
            "lo": c["head_end_turn"] + 1,
            "hi": c["tail_start_turn"] - 1,
            "count": c["tail_start_turn"] - c["head_end_turn"] - 1,
            "summary_excerpt": summary_excerpt(c["summary_text"]),
            "head_end_turn": c["head_end_turn"],
            "tail_start_turn": c["tail_start_turn"],
        }
        for c in compressions
        if c["tail_start_turn"] - c["head_end_turn"] - 1 > 0
    ]
    fold_starts = {f["lo"] for f in fold_ranges}
    fold_set = set()
    for f in fold_ranges:
        fold_set.update(range(f["lo"], f["hi"] + 1))

    from openhachimi_agent.transport.api_models import MessageItem

    # 直接按 turn_index 读原始行 + 折叠判定,不走 extract_text_parts 的视图路径
    # —— 视图会跳过折叠区间,但展示恰恰要把折叠条插入到该位置。
    safe_role = role
    rows, has_more = service.session_store._load_message_rows(safe_role, resolved_session_id, limit=limit, before_turn=before_turn)
    total = service.session_store.count_messages(safe_role, resolved_session_id)
    messages: list[MessageItem] = []
    folded_seen: set[int] = set()
    next_before_turn = rows[0][0] if rows and has_more else None

    for turn_idx, msg in rows:
        if turn_idx in fold_set:
            # 落在折叠区间:跳过原始消息,在区间起始处插一个占位条
            if turn_idx in fold_starts and turn_idx not in folded_seen:
                fold_info = next(f for f in fold_ranges if f["lo"] == turn_idx)
                messages.append(MessageItem(
                    role="user", content="", fold={
                        "compression_id": fold_info["compression_id"],
                        "dropped_count": fold_info["count"],
                        "summary_excerpt": fold_info["summary_excerpt"],
                        "head_end_turn": fold_info["head_end_turn"],
                        "tail_start_turn": fold_info["tail_start_turn"],
                    },
                ))
                folded_seen.add(turn_idx)
            continue
        parts = extract_text_parts(service, [msg], role=role)
        for p in parts:
            messages.append(MessageItem(**p))

    return {
        "role": role,
        "session_id": resolved_session_id,
        "messages": messages,
    }


def get_folded_messages(
    service, role_name: str | None, session_id: str, compression_id: int
) -> list[dict]:
    """返回某次压缩被折叠的原始消息(展开用),已抽成 MessageItem 文本结构。"""
    role = service._normalize_role(role_name)
    service._validate_role_exists(role)
    resolved_session_id = service._normalize_session_id(session_id)
    msgs = service.session_store.get_folded_messages(role, resolved_session_id, compression_id)
    out: list[dict] = []
    for msg in msgs:
        out.extend(extract_text_parts(service, [msg], role=role))
    return out
