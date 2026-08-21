import base64

import pytest

from openhachimi_agent.interface.weixin import media as weixin_media
from openhachimi_agent.interface.weixin.ilink_client import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_VIDEO,
    MEDIA_TYPE_FILE,
    MEDIA_TYPE_IMAGE,
    MEDIA_TYPE_VIDEO,
    _aes128_ecb_decrypt,
    _aes_padded_size,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def test_encode_aes_key_for_api_uses_base64_of_hex_string():
    aes_key = bytes(range(16))

    encoded = weixin_media.encode_aes_key_for_api(aes_key)
    decoded = base64.b64decode(encoded)

    assert len(decoded) == 32
    assert decoded.decode("ascii") == aes_key.hex()


def test_media_type_for_maps_by_mime(tmp_path):
    assert weixin_media.media_type_for("photo.jpeg") == MEDIA_TYPE_IMAGE
    assert weixin_media.media_type_for("clip.mp4") == MEDIA_TYPE_VIDEO
    assert weixin_media.media_type_for("report.pdf") == MEDIA_TYPE_FILE
    assert weixin_media.media_type_for("data.bin") == MEDIA_TYPE_FILE


def test_build_media_item_image():
    item = weixin_media.build_media_item(
        filename="photo.jpg",
        encrypt_query_param="dl-param",
        aes_key_b64="aes-b64",
        ciphertext_size=128,
        plaintext_size=100,
        rawfilemd5="md5",
    )

    assert item["type"] == ITEM_IMAGE
    assert item["image_item"]["media"] == {
        "encrypt_query_param": "dl-param",
        "aes_key": "aes-b64",
        "encrypt_type": 1,
    }
    assert item["image_item"]["mid_size"] == 128


def test_build_media_item_file():
    item = weixin_media.build_media_item(
        filename="report.pdf",
        encrypt_query_param="dl-param",
        aes_key_b64="aes-b64",
        ciphertext_size=128,
        plaintext_size=100,
        rawfilemd5="md5",
    )

    assert item["type"] == ITEM_FILE
    assert item["file_item"]["file_name"] == "report.pdf"
    assert item["file_item"]["len"] == "100"


def test_build_media_item_video():
    item = weixin_media.build_media_item(
        filename="clip.mp4",
        encrypt_query_param="dl-param",
        aes_key_b64="aes-b64",
        ciphertext_size=128,
        plaintext_size=100,
        rawfilemd5="md5",
    )

    assert item["type"] == ITEM_VIDEO
    assert item["video_item"]["video_size"] == 128
    assert item["video_item"]["video_md5"] == "md5"


class FakeUploadClient:
    def __init__(self, upload_response=None):
        default = {"upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?x=1"}
        self.upload_response = default if upload_response is None else upload_response
        self.get_upload_url_calls = []
        self.upload_calls = []
        self.media_messages = []

    async def get_upload_url(self, **kwargs):
        self.get_upload_url_calls.append(kwargs)
        return self.upload_response

    async def upload_ciphertext(self, *, ciphertext, upload_url):
        self.upload_calls.append({"ciphertext": ciphertext, "upload_url": upload_url})
        return "dl-param"

    async def send_media_message(self, to_user_id, item, context_token, client_id):
        self.media_messages.append(
            {"to_user_id": to_user_id, "item": item, "context_token": context_token, "client_id": client_id}
        )
        return {}


@pytest.mark.asyncio
async def test_send_artifact_roundtrip_with_fake_client(tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(PNG_BYTES)
    client = FakeUploadClient()

    await weixin_media.send_artifact(
        client,
        to_user_id="wxid_user",
        context_token="ctx",
        path=path,
    )

    upload_call = client.get_upload_url_calls[0]
    assert upload_call["media_type"] == MEDIA_TYPE_IMAGE
    assert upload_call["rawsize"] == len(PNG_BYTES)
    assert upload_call["filesize"] == _aes_padded_size(len(PNG_BYTES))
    assert len(upload_call["aeskey_hex"]) == 32

    assert client.upload_calls[0]["upload_url"].startswith("https://novac2c.cdn.weixin.qq.com/c2c/upload")
    ciphertext = client.upload_calls[0]["ciphertext"]
    assert ciphertext != PNG_BYTES
    assert len(ciphertext) % 16 == 0
    aes_key = bytes.fromhex(upload_call["aeskey_hex"])
    assert _aes128_ecb_decrypt(ciphertext, aes_key) == PNG_BYTES

    message = client.media_messages[0]
    assert message["to_user_id"] == "wxid_user"
    assert message["context_token"] == "ctx"
    item = message["item"]
    assert item["type"] == ITEM_IMAGE
    assert item["image_item"]["media"]["encrypt_query_param"] == "dl-param"
    decoded_key = base64.b64decode(item["image_item"]["media"]["aes_key"])
    assert decoded_key.decode("ascii") == upload_call["aeskey_hex"]


@pytest.mark.asyncio
async def test_client_get_upload_url_payload_includes_thumb_flag(monkeypatch):
    from openhachimi_agent.interface.weixin.ilink_client import WeixinClient

    captured = {}

    async def fake_api_post(self, endpoint, payload, timeout_ms):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {"upload_param": "up"}

    monkeypatch.setattr(WeixinClient, "_api_post", fake_api_post)
    client = WeixinClient()

    response = await client.get_upload_url(
        to_user_id="wxid_user",
        media_type=MEDIA_TYPE_IMAGE,
        filekey="filekey",
        rawsize=10,
        rawfilemd5="md5",
        filesize=16,
        aeskey_hex="ab" * 16,
    )

    assert response == {"upload_param": "up"}
    assert captured["endpoint"] == "ilink/bot/getuploadurl"
    assert captured["payload"]["no_need_thumb"] is True
    assert captured["payload"]["aeskey"] == "ab" * 16


@pytest.mark.asyncio
async def test_send_artifact_falls_back_to_upload_param_url(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"pdf-bytes")
    client = FakeUploadClient(upload_response={"upload_param": "up-param"})

    await weixin_media.send_artifact(
        client,
        to_user_id="wxid_user",
        context_token="",
        path=path,
    )

    assert "encrypted_query_param=up-param" in client.upload_calls[0]["upload_url"]
    assert "filekey=" in client.upload_calls[0]["upload_url"]
    item = client.media_messages[0]["item"]
    assert item["type"] == ITEM_FILE


@pytest.mark.asyncio
async def test_send_artifact_raises_without_upload_credential(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"pdf-bytes")
    client = FakeUploadClient(upload_response={})

    with pytest.raises(RuntimeError, match="上传凭证"):
        await weixin_media.send_artifact(
            client,
            to_user_id="wxid_user",
            context_token="ctx",
            path=path,
        )
