"""微信渠道出站媒体：把生成的文件经 iLink 加密 CDN 发送给用户。"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import secrets
import uuid
from pathlib import Path
from typing import Any

from openhachimi_agent.interface.weixin.ilink_client import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_VIDEO,
    MEDIA_TYPE_FILE,
    MEDIA_TYPE_IMAGE,
    MEDIA_TYPE_VIDEO,
    WeixinClient,
    _aes128_ecb_encrypt,
    _aes_padded_size,
    _cdn_upload_url,
)


def encode_aes_key_for_api(aes_key: bytes) -> str:
    # iLink 客户端的解密链路是 base64 解码 → 得到 32 位 hex 字符串 → fromhex
    # 还原密钥，因此必须编码为 base64(hex字符串字节)；直接 base64(原始字节)
    # 会导致接收端解密失败（表现为图片灰框）。
    return base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")


def resolve_artifact_path(base_dir: Path, local_path: str) -> Path:
    path = Path(local_path)
    if not path.is_absolute():
        path = base_dir / path
    resolved = path.resolve()
    # 防御纵深:artifact.local_path 若被污染成工作区外绝对路径,会读任意文件并
    # 上传到微信 CDN —— 与 Telegram 渠道保持一致的 contain 校验。
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"artifact 路径不在工作区内：{local_path}") from exc
    return resolved


def media_type_for(filename: str) -> int:
    mime = mimetypes.guess_type(filename)[0] or ""
    if mime.startswith("image/"):
        return MEDIA_TYPE_IMAGE
    if mime.startswith("video/"):
        return MEDIA_TYPE_VIDEO
    return MEDIA_TYPE_FILE


def build_media_item(
    *,
    filename: str,
    encrypt_query_param: str,
    aes_key_b64: str,
    ciphertext_size: int,
    plaintext_size: int,
    rawfilemd5: str,
) -> dict[str, Any]:
    media = {
        "encrypt_query_param": encrypt_query_param,
        "aes_key": aes_key_b64,
        "encrypt_type": 1,
    }
    mime = mimetypes.guess_type(filename)[0] or ""
    if mime.startswith("image/"):
        return {"type": ITEM_IMAGE, "image_item": {"media": media, "mid_size": ciphertext_size}}
    if mime.startswith("video/"):
        return {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": media,
                "video_size": ciphertext_size,
                "play_length": 0,
                "video_md5": rawfilemd5,
            },
        }
    return {
        "type": ITEM_FILE,
        "file_item": {"media": media, "file_name": filename, "len": str(plaintext_size)},
    }


async def send_artifact(
    client: WeixinClient,
    *,
    to_user_id: str,
    context_token: str,
    path: Path,
    filename: str | None = None,
) -> None:
    """将本地文件加密上传到微信 CDN 并作为媒体消息发送。"""
    plaintext = path.read_bytes()
    name = filename or path.name
    filekey = secrets.token_hex(16)
    aes_key = secrets.token_bytes(16)
    rawsize = len(plaintext)
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()

    upload_response = await client.get_upload_url(
        to_user_id=to_user_id,
        media_type=media_type_for(name),
        filekey=filekey,
        rawsize=rawsize,
        rawfilemd5=rawfilemd5,
        filesize=_aes_padded_size(rawsize),
        aeskey_hex=aes_key.hex(),
    )
    upload_full_url = str(upload_response.get("upload_full_url") or "")
    upload_param = str(upload_response.get("upload_param") or "")
    if not upload_full_url and not upload_param:
        raise RuntimeError(f"getuploadurl 未返回上传凭证: {upload_response}")
    upload_url = upload_full_url or _cdn_upload_url(upload_param, filekey)

    ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)
    encrypted_query_param = await client.upload_ciphertext(ciphertext=ciphertext, upload_url=upload_url)

    item = build_media_item(
        filename=name,
        encrypt_query_param=encrypted_query_param,
        aes_key_b64=encode_aes_key_for_api(aes_key),
        ciphertext_size=len(ciphertext),
        plaintext_size=rawsize,
        rawfilemd5=rawfilemd5,
    )
    await client.send_media_message(
        to_user_id,
        item,
        context_token=context_token or None,
        client_id=f"openhachimi-{uuid.uuid4().hex[:8]}",
    )
