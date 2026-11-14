"""
验证绿联接口 RSA 分块、AES 请求加密与响应解密。
"""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.modules.ugreen.crypto import UgreenCrypto


@pytest.fixture
def crypto_context() -> SimpleNamespace:
    """
    为每个加密用例创建独立 RSA 密钥和固定身份的绿联加密实例。

    :return: 包含公钥 public_key、私钥 private_key、令牌 token 与加密器 crypto 的上下文
    """
    public_key, private_key = _generate_rsa_keys()
    token = "demo-token-for-test"
    return SimpleNamespace(
        public_key=public_key,
        private_key=private_key,
        token=token,
        crypto=UgreenCrypto(public_key=public_key, token=token, client_id="test-client-id"),
    )


def _generate_rsa_keys() -> tuple[str, rsa.RSAPrivateKey]:
    """
    为测试生成独立 RSA 密钥对，并返回 PEM 公钥与私钥对象。
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.PKCS1,
        )
        .decode("utf-8")
    )
    return public_pem, private_key


def _rsa_decrypt_long(private_key: rsa.RSAPrivateKey, payload_b64: str) -> str:
    """
    按 RSA 块长度解密测试载荷，验证长文本分块加密可逆。
    """
    encrypted = base64.b64decode(payload_b64)
    chunk_size = private_key.key_size // 8
    plain_chunks = []
    for start in range(0, len(encrypted), chunk_size):
        chunk = encrypted[start : start + chunk_size]
        plain_chunks.append(private_key.decrypt(chunk, padding.PKCS1v15()))
    return b"".join(plain_chunks).decode("utf-8")


def test_rsa_encrypt_long(crypto_context) -> None:
    """
    超过单个 RSA 块的长文本可完整加解密还原。
    """
    plain = "A" * 400
    encrypted = crypto_context.crypto.rsa_encrypt_long(plain)
    assert plain == _rsa_decrypt_long(crypto_context.private_key, encrypted)


def test_build_encrypted_request_and_decrypt_response(crypto_context) -> None:
    """
    加密请求的查询、正文与令牌可还原，响应也按同一密钥解密。
    """
    req = crypto_context.crypto.build_encrypted_request(
        url="http://127.0.0.1:9999/ugreen/v1/video/homepage/media_list",
        params={"page": 1, "page_size": 50},
        data={"foo": "bar", "count": 2},
    )

    assert req.plain_query == "page=1&page_size=50"
    assert req.plain_query == crypto_context.crypto.aes_gcm_decrypt(req.params["encrypt_query"], req.aes_key)

    assert req.headers["X-Ugreen-Security-Key"] == hashlib.md5(crypto_context.token.encode("utf-8")).hexdigest()
    assert req.aes_key == _rsa_decrypt_long(crypto_context.private_key, req.headers["X-Ugreen-Security-Code"])
    assert crypto_context.token == _rsa_decrypt_long(crypto_context.private_key, req.headers["X-Ugreen-Token"])

    encrypted_body = req.json["encrypt_req_body"]
    body_plain = crypto_context.crypto.aes_gcm_decrypt(encrypted_body, req.aes_key)
    assert json.loads(body_plain) == {"foo": "bar", "count": 2}
    assert req.json["req_body_sha256"] == hashlib.sha256(body_plain.encode("utf-8")).hexdigest()

    server_payload = {"code": 0, "msg": "ok", "data": {"items": [1, 2, 3]}}
    resp = {
        "encrypt_resp_body": crypto_context.crypto.aes_gcm_encrypt(
            json.dumps(server_payload, ensure_ascii=False, separators=(",", ":")),
            req.aes_key,
        )
    }
    decoded = crypto_context.crypto.decrypt_response(resp, req.aes_key)
    assert decoded == server_payload
