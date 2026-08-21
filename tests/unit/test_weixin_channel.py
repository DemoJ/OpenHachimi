import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from openhachimi_agent.interface.weixin import channel as weixin_channel
from openhachimi_agent.interface.weixin.channel import (
    WeixinChannel,
    _ensure_weixin_line_breaks,
    _extract_text_content,
)
from openhachimi_agent.storage.attachments import AttachmentStorage
from openhachimi_agent.transport.api_models import ArtifactRef


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def make_channel(mock_config, service=None, client=None):
    channel = WeixinChannel.__new__(WeixinChannel)
    channel.service = service or SimpleNamespace()
    channel.config = mock_config
    channel.client = client or SimpleNamespace()
    channel.attachment_storage = AttachmentStorage(
        mock_config.attachments_dir,
        mock_config.max_attachment_size_bytes,
        mock_config.allowed_attachment_mime_types,
        mock_config.base_dir,
    )
    channel.media_batch_delay_seconds = 0
    channel.recent_media_ttl_seconds = 600
    channel.recent_media_max_attachments = mock_config.vision.max_images_per_message
    channel._pending_media_messages = {}
    channel._pending_media_tasks = {}
    channel._recent_media = {}
    channel._media_context_lock = asyncio.Lock()
    return channel


def test_extract_text_content_includes_voice_transcription():
    items = [
        {"type": 1, "text_item": {"text": "先看这个"}},
        {"type": 3, "voice_item": {"text": "今天天气怎么样", "voice_url": "https://example.com/a.silk"}},
    ]

    result = _extract_text_content(items)

    assert "先看这个" in result
    assert "微信语音消息" in result
    assert "今天天气怎么样" in result


def test_extract_text_content_ignores_voice_without_transcription():
    assert _extract_text_content([{"type": 3, "voice_item": {"voice_url": "https://example.com/a.silk"}}]) == ""


def test_ensure_weixin_line_breaks_separates_plain_lines():
    assert _ensure_weixin_line_breaks("第一行\n第二行") == "第一行\n\n第二行"


def test_ensure_weixin_line_breaks_keeps_existing_blank_lines():
    text = "第一行\n\n第二行"
    assert _ensure_weixin_line_breaks(text) == text


def test_ensure_weixin_line_breaks_keeps_code_fence_intact():
    text = "说明如下\n```python\nprint('a')\nprint('b')\n```\n结束"
    formatted = _ensure_weixin_line_breaks(text)
    assert "print('a')\nprint('b')" in formatted
    assert formatted.startswith("说明如下\n\n")
    assert formatted.endswith("\n\n结束")


def test_ensure_weixin_line_breaks_keeps_markdown_lists_tight():
    text = "- 项目一\n- 项目二\n1. 步骤一\n2. 步骤二"
    assert _ensure_weixin_line_breaks(text) == text


def test_ensure_weixin_line_breaks_separates_list_from_plain_text():
    assert _ensure_weixin_line_breaks("- 列表项\n普通正文") == "- 列表项\n\n普通正文"


def test_ensure_weixin_line_breaks_formats_new_session_reply():
    lines = [
        "✨ 新对话已准备好",
        "",
        "✅ 上一段对话已保存",
        "📝 已为你开启一段全新的上下文",
        "",
        "━━ 当前配置 ━━",
        "🤖 模型:hachimi",
        "🌐 模型服务:http://10.8.8.13:3002/v1",
        "🎭 角色:default",
        "🧩 会话:20260821-123906-3ec0d74a",
        "",
        "💬 直接输入内容并回车,即可继续对话。",
    ]
    formatted = _ensure_weixin_line_breaks("\n".join(lines))
    non_blank = [line for line in formatted.splitlines() if line.strip()]
    assert non_blank[0] == "✨ 新对话已准备好"
    assert non_blank[-1] == "💬 直接输入内容并回车,即可继续对话。"
    for prev, nxt in zip(formatted.splitlines(), formatted.splitlines()[1:]):
        if prev.strip() and nxt.strip():
            raise AssertionError(f"相邻非空行之间缺少空行: {prev!r} -> {nxt!r}")


@pytest.mark.asyncio
async def test_handle_voice_only_message_reaches_agent_service(mock_config):
    class FakeService:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output="收到", artifacts=[])

    class FakeWeixinClient:
        def __init__(self):
            self.sent = []

        async def get_typing_ticket(self, to_user_id):
            return None

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {}

    service = FakeService()
    client = FakeWeixinClient()
    channel = make_channel(mock_config, service=service, client=client)

    await channel._handle_message(
        {
            "message_type": 1,
            "from_user_id": "wxid_user",
            "context_token": "ctx",
            "item_list": [
                {
                    "type": 3,
                    "voice_item": {
                        "text": "帮我查一下今天的安排",
                        "voice_url": "https://example.com/a.silk",
                    },
                }
            ],
        }
    )

    assert len(service.calls) == 1
    assert "帮我查一下今天的安排" in service.calls[0]["message"]
    assert service.calls[0]["channel"] == "weixin"
    assert client.sent[0]["to_user_id"] == "wxid_user"
    assert client.sent[0]["text"] == "收到"


@pytest.mark.asyncio
async def test_download_media_attachments_saves_weixin_image(mock_config):
    class FakeWeixinClient:
        async def download_media(self, url, max_size_bytes):
            return PNG_BYTES, "image/png"

    channel = make_channel(mock_config, client=FakeWeixinClient())

    attachments, hints = await channel._download_media_attachments(
        [{"type": 2, "image_item": {"image_url": "https://example.com/photo.png"}}],
        namespace="wxid_user_msg1",
    )

    assert len(attachments) == 1
    assert attachments[0].source == "weixin"
    assert attachments[0].kind == "image"
    assert attachments[0].content_type == "image/png"
    assert "微信图片" in hints[0]
    assert (mock_config.base_dir / attachments[0].local_path).read_bytes() == PNG_BYTES


@pytest.mark.asyncio
async def test_download_media_attachments_prefers_encrypted_weixin_media(mock_config):
    class FakeWeixinClient:
        def __init__(self):
            self.calls = []

        async def download_encrypted_media(self, **kwargs):
            self.calls.append(kwargs)
            return PNG_BYTES, "application/octet-stream"

    client = FakeWeixinClient()
    channel = make_channel(mock_config, client=client)

    attachments, hints = await channel._download_media_attachments(
        [
            {
                "type": 2,
                "image_item": {
                    "aeskey": "00112233445566778899aabbccddeeff",
                    "media": {"encrypt_query_param": "encrypted-query"},
                },
            }
        ],
        namespace="wxid_user_msg2",
    )

    assert client.calls[0]["encrypted_query_param"] == "encrypted-query"
    assert client.calls[0]["aes_key"] == "00112233445566778899aabbccddeeff"
    assert len(attachments) == 1
    assert attachments[0].kind == "image"
    assert attachments[0].content_type == "image/png"
    assert attachments[0].filename.endswith(".png")
    assert "微信图片" in hints[0]


@pytest.mark.asyncio
async def test_download_media_attachments_rejects_non_image_bytes(mock_config):
    class FakeWeixinClient:
        async def download_encrypted_media(self, **kwargs):
            return b"encrypted-or-html-response", "application/octet-stream"

    channel = make_channel(mock_config, client=FakeWeixinClient())

    attachments, hints = await channel._download_media_attachments(
        [
            {
                "type": 2,
                "image_item": {
                    "media": {"encrypt_query_param": "encrypted-query"},
                },
            }
        ],
        namespace="wxid_user_msg3",
    )

    assert attachments == []
    assert "不是支持的图片格式" in hints[0]


@pytest.mark.asyncio
async def test_handle_image_only_message_reaches_agent_with_attachment(mock_config):
    class FakeService:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output="图里有内容", artifacts=[])

    class FakeWeixinClient:
        def __init__(self):
            self.sent = []

        async def get_typing_ticket(self, to_user_id):
            return None

        async def download_media(self, url, max_size_bytes):
            return PNG_BYTES, "application/octet-stream"

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {}

    service = FakeService()
    client = FakeWeixinClient()
    channel = make_channel(mock_config, service=service, client=client)

    await channel._handle_message(
        {
            "message_type": 1,
            "from_user_id": "wxid_user",
            "item_list": [
                {
                    "type": 2,
                    "image_item": {"cdn_url": "https://example.com/photo"},
                }
            ],
        }
    )

    assert len(service.calls) == 1
    assert "微信图片" in service.calls[0]["message"]
    assert len(service.calls[0]["attachments"]) == 1
    assert service.calls[0]["attachments"][0].kind == "image"
    assert service.calls[0]["attachments"][0].content_type == "image/png"
    assert client.sent[0]["text"] == "图里有内容"


@pytest.mark.asyncio
async def test_image_then_text_within_batch_window_merges_once(mock_config):
    class FakeService:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output="合并好了", artifacts=[])

    class FakeWeixinClient:
        def __init__(self):
            self.sent = []

        async def get_typing_ticket(self, to_user_id):
            return None

        async def download_media(self, url, max_size_bytes):
            return PNG_BYTES, "image/png"

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {}

    service = FakeService()
    client = FakeWeixinClient()
    channel = make_channel(mock_config, service=service, client=client)
    channel.media_batch_delay_seconds = 0.05

    await channel._handle_message(
        {
            "message_type": 1,
            "from_user_id": "wxid_user",
            "item_list": [{"type": 2, "image_item": {"cdn_url": "https://example.com/photo"}}],
        }
    )
    assert service.calls == []

    await channel._handle_message(
        {
            "message_type": 1,
            "from_user_id": "wxid_user",
            "item_list": [{"type": 1, "text_item": {"text": "这张图里是什么？"}}],
        }
    )
    await asyncio.sleep(0.08)

    assert len(service.calls) == 1
    assert "这张图里是什么" in service.calls[0]["message"]
    assert "微信图片" in service.calls[0]["message"]
    assert len(service.calls[0]["attachments"]) == 1
    assert len(client.sent) == 1


@pytest.mark.asyncio
async def test_text_after_image_reuses_recent_image_context(mock_config):
    class FakeService:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output="看到了", artifacts=[])

    class FakeWeixinClient:
        def __init__(self):
            self.sent = []

        async def get_typing_ticket(self, to_user_id):
            return None

        async def download_media(self, url, max_size_bytes):
            return PNG_BYTES, "image/png"

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {}

    service = FakeService()
    channel = make_channel(mock_config, service=service, client=FakeWeixinClient())

    await channel._handle_message(
        {
            "message_type": 1,
            "from_user_id": "wxid_user",
            "item_list": [{"type": 2, "image_item": {"cdn_url": "https://example.com/photo"}}],
        }
    )
    await channel._handle_message(
        {
            "message_type": 1,
            "from_user_id": "wxid_user",
            "item_list": [{"type": 1, "text_item": {"text": "这张图第二行写了什么？"}}],
        }
    )

    assert len(service.calls) == 2
    assert len(service.calls[1]["attachments"]) == 1
    assert service.calls[1]["attachments"][0].kind == "image"
    assert "最近发送过微信图片" in service.calls[1]["message"]


@pytest.mark.asyncio
async def test_artifact_notice_falls_back_to_text_when_upload_unavailable(mock_config):
    class FakeService:
        async def send_message(self, **kwargs):
            return SimpleNamespace(
                output="文件已生成",
                artifacts=[
                    ArtifactRef(
                        id="art_1",
                        filename="report.pdf",
                        content_type="application/pdf",
                        size_bytes=2048,
                        local_path=".tmp/artifacts/art_1/report.pdf",
                        download_url="/artifacts/art_1/download",
                    )
                ],
            )

    class FakeWeixinClient:
        def __init__(self):
            self.sent = []

        async def get_typing_ticket(self, to_user_id):
            return None

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {}

    client = FakeWeixinClient()
    channel = make_channel(mock_config, service=FakeService(), client=client)

    await channel._handle_message(
        {
            "message_type": 1,
            "from_user_id": "wxid_user",
            "item_list": [{"type": 1, "text_item": {"text": "生成报告"}}],
        }
    )

    assert len(client.sent) == 2
    assert "文件已生成" in client.sent[0]["text"]
    assert "report.pdf" not in client.sent[0]["text"]
    assert "未能通过微信发送" in client.sent[1]["text"]
    assert "report.pdf" in client.sent[1]["text"]
    assert "/artifacts/art_1/download" in client.sent[1]["text"]


@pytest.mark.asyncio
async def test_artifacts_are_sent_as_weixin_media_messages(mock_config, tmp_path):
    artifact_file = tmp_path / "photo.png"
    artifact_file.write_bytes(PNG_BYTES)

    class FakeService:
        async def send_message(self, **kwargs):
            return SimpleNamespace(
                output="图片已生成",
                artifacts=[
                    ArtifactRef(
                        id="art_2",
                        filename="photo.png",
                        content_type="image/png",
                        size_bytes=len(PNG_BYTES),
                        local_path=artifact_file.relative_to(mock_config.base_dir).as_posix(),
                        download_url="/artifacts/art_2/download",
                    )
                ],
            )

    class FakeWeixinClient:
        def __init__(self):
            self.sent = []
            self.media_messages = []

        async def get_typing_ticket(self, to_user_id):
            return None

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return {}

        async def get_upload_url(self, **kwargs):
            return {"upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?x=1"}

        async def upload_ciphertext(self, *, ciphertext, upload_url):
            return "dl-param"

        async def send_media_message(self, to_user_id, item, context_token, client_id):
            self.media_messages.append(
                {"to_user_id": to_user_id, "item": item, "context_token": context_token}
            )
            return {}

    client = FakeWeixinClient()
    channel = make_channel(mock_config, service=FakeService(), client=client)

    await channel._handle_message(
        {
            "message_type": 1,
            "from_user_id": "wxid_user",
            "context_token": "ctx",
            "item_list": [{"type": 1, "text_item": {"text": "画一张图"}}],
        }
    )

    assert len(client.sent) == 1
    assert "图片已生成" in client.sent[0]["text"]
    assert "未能通过微信发送" not in client.sent[0]["text"]
    assert len(client.media_messages) == 1
    media = client.media_messages[0]
    assert media["to_user_id"] == "wxid_user"
    assert media["context_token"] == "ctx"
    assert media["item"]["type"] == 2
    assert media["item"]["image_item"]["media"]["encrypt_query_param"] == "dl-param"


@pytest.mark.asyncio
async def test_weixin_supervisor_starts_channel_when_account_file_appears(mock_config, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    class FakeClient:
        async def close(self):
            pass

    class FakeWeixinChannel:
        def __init__(self, service, config):
            self.service = service
            self.config = config
            self.client = FakeClient()

        async def run_loop(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr(weixin_channel, "WeixinChannel", FakeWeixinChannel)
    task = asyncio.create_task(
        weixin_channel._weixin_channel_supervisor(
            SimpleNamespace(),
            mock_config,
            poll_interval=0.01,
        )
    )

    try:
        await asyncio.sleep(0.03)
        assert not started.is_set()

        account_path = weixin_channel._account_file(mock_config)
        account_path.parent.mkdir(parents=True, exist_ok=True)
        account_path.write_text('{"token": "token", "account_id": "bot"}', "utf-8")

        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    await asyncio.wait_for(stopped.wait(), timeout=1)
