import base64

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from openhachimi_agent.interface.weixin.ilink_client import WeixinClient, _parse_aes_key


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _aes128_ecb_encrypt_pkcs7(data: bytes, key: bytes) -> bytes:
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len]) * pad_len
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def test_parse_aes_key_accepts_weixin_hex_and_base64_forms():
    key = bytes.fromhex("00112233445566778899aabbccddeeff")

    assert _parse_aes_key(key.hex()) == key
    assert _parse_aes_key(base64.b64encode(key).decode("ascii")) == key
    assert _parse_aes_key(base64.b64encode(key.hex().encode("ascii")).decode("ascii")) == key


@pytest.mark.asyncio
async def test_download_encrypted_media_decrypts_cdn_bytes():
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    encrypted = _aes128_ecb_encrypt_pkcs7(PNG_BYTES, key)

    class FakeWeixinClient(WeixinClient):
        def __init__(self):
            self.urls = []

        async def _download_bytes(self, url, max_size_bytes, headers=None):
            self.urls.append(url)
            return encrypted, "application/octet-stream"

    client = FakeWeixinClient()

    data, content_type = await client.download_encrypted_media(
        encrypted_query_param="fileid=abc&token=123",
        aes_key=key.hex(),
        full_url=None,
        max_size_bytes=1024,
    )

    assert data == PNG_BYTES
    assert content_type == "application/octet-stream"
    assert "encrypted_query_param=fileid%3Dabc%26token%3D123" in client.urls[0]
