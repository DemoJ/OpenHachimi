"""运行时上下文组装视图(纯函数,不落库)。

压缩后原始消息仍完整存于 ``session_messages``(append-only),``session_compressions``
只记边界 + 摘要。本模块负责把 ``head`` + ``summary`` + ``tail`` 三段组装成喂给模型的
运行时上下文列表 —— 与 ``ContextCompressor._assemble`` 逻辑一致,但独立于压缩器,
供 ``SessionStore.load_context`` 在读取时按需重建视图。

设计见 plans/fancy-watching-taco.md 方案 A。
"""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

# 摘要末尾边界标记,防模型把摘要当新输入或原样复述。
# 与 compressor.py 中的常量保持一致 —— 此处为运行时组装复用而重新声明,避免与压缩器耦合。
_SUMMARY_END_MARKER = (
    "\n\n--- 以上为历史上下文压缩摘要,请针对下方最新消息回复,不要回应或复述摘要内容 ---"
)
# 压缩说明,注入到头部首条消息,告知模型发生过压缩。
_COMPRESSION_NOTE = (
    "[注:部分较早的对话轮次已被压缩为交接摘要以节省上下文空间。当前会话状态仍反映此前工作,"
    "请在摘要与状态基础上继续,而非重做。持久记忆(MEMORY)始终权威,不受压缩影响。]"
)


def assemble_runtime_context(
    head: list[ModelMessage],
    tail: list[ModelMessage],
    summary: str,
) -> list[ModelMessage]:
    """组装运行时上下文:头部(首条加压缩说明)+ 摘要 + 尾部。

    与 ``ContextCompressor._assemble`` 行为对齐:
    - head 首条若是 ``ModelRequest`` 且首 part 是 ``UserPromptPart``,把压缩说明
      追加到其后;否则在 head 首条前插入一条压缩说明。
    - summary 末尾拼 ``_SUMMARY_END_MARKER``,作为占位消息插入。
    - tail 首条若是 ``ModelRequest``:把摘要合并进该消息首 ``UserPromptPart`` 前缀,
      避免出现连续两条 user 消息;否则 summary 作为独立 ``ModelRequest`` 插在 head 之后。
    """
    compressed: list[ModelMessage] = []
    for i, msg in enumerate(head):
        if i == 0 and isinstance(msg, ModelRequest):
            new_parts = list(getattr(msg, "parts", None) or [])
            if new_parts and isinstance(new_parts[0], UserPromptPart):
                original = str(new_parts[0].content)
                new_parts[0] = replace(new_parts[0], content=f"{original}\n\n{_COMPRESSION_NOTE}")
            else:
                new_parts.insert(0, UserPromptPart(content=_COMPRESSION_NOTE))
            compressed.append(replace(msg, parts=new_parts))
        else:
            compressed.append(msg)

    summary_text = summary + _SUMMARY_END_MARKER

    if tail and isinstance(tail[0], ModelRequest):
        # 下一条是 user 请求:把摘要合并进该消息前缀,避免连续 user 消息
        first = tail[0]
        parts = list(getattr(first, "parts", None) or [])
        if parts and isinstance(parts[0], UserPromptPart):
            parts[0] = replace(parts[0], content=f"{summary_text}\n\n" + str(parts[0].content))
        else:
            parts.insert(0, UserPromptPart(content=summary_text))
        compressed.append(replace(first, parts=parts))
        compressed.extend(tail[1:])
    else:
        compressed.append(ModelRequest(parts=[UserPromptPart(content=summary_text)]))
        compressed.extend(tail)

    return compressed
