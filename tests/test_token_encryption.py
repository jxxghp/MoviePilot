"""
AES 文本加密的 UTF-8 字节填充与既有密文兼容契约。
"""

import base64

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from app.application.security.token import aes_decrypt, aes_encrypt

# 公开固定测试材料，不用于真实账户或生产加密。
TEST_KEY = "0123456789abcdef0123456789abcdef"
TEST_IV = b"public-test-iv!!"


@pytest.mark.parametrize("key", [TEST_KEY[:16], TEST_KEY[:24], TEST_KEY])
@pytest.mark.parametrize("text", ["中文", "a中", "中文123", "🎵电影", "é", "中" * 16])
def test_aes_encrypt_uses_utf8_byte_padding(key, text):
    """
    多字节文本必须可被既有解密器和独立的 PKCS7 解填充还原。
    """
    encrypted = aes_encrypt(text, key)
    raw = base64.b64decode(encrypted)

    assert aes_decrypt(encrypted, key) == text
    assert len(raw[16:]) % AES.block_size == 0
    plaintext = AES.new(key.encode(), AES.MODE_CBC, raw[:16]).decrypt(raw[16:])
    assert unpad(plaintext, AES.block_size).decode("utf-8") == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MoviePilot", "cHVibGljLXRlc3QtaXYhIZgnVmGhRmHez6IkIjC3wxM="),
        ("a" * 15, "cHVibGljLXRlc3QtaXYhIaAx+T1J80WDWJTHAY/KalA="),
        ("a" * 16, "cHVibGljLXRlc3QtaXYhIaSOoHnQHNvUDkzvBGls2ZBLZ/17cnQTjeyp4aiVZKwK"),
        ("a" * 17, "cHVibGljLXRlc3QtaXYhIaSOoHnQHNvUDkzvBGls2ZCha8byuNUxqZhv6tTohAr4"),
    ],
)
def test_aes_ascii_ciphertext_matches_legacy_vectors(monkeypatch, text, expected):
    """
    固定 IV 后与修复前的 ASCII 密文逐字节对照，覆盖整块前后边界。
    """
    monkeypatch.setattr("Crypto.Cipher._mode_cbc.get_random_bytes", lambda _size: TEST_IV)

    assert aes_encrypt(text, TEST_KEY) == expected
    assert aes_decrypt(expected, TEST_KEY) == text


@pytest.mark.parametrize("key", [TEST_KEY, "invalid"])
def test_empty_aes_text_preserves_early_return(key):
    """
    空文本继续直接返回空值，不先校验密钥或创建加密器。
    """
    assert aes_encrypt("", key) == ""
    assert aes_decrypt("", key) == ""


@pytest.mark.parametrize("text", ["ASCII", "中文"])
def test_aes_invalid_key_still_raises(text):
    """
    非空文本使用非法长度密钥时仍向调用方报告错误。
    """
    with pytest.raises(ValueError, match="Incorrect AES key length"):
        aes_encrypt(text, "invalid")
