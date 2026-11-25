"""
企业微信加密协议与失败返回值的离线回归。
"""

import base64
import hashlib
import struct
import xml.etree.ElementTree as ET
from unittest.mock import Mock

import pytest
from Crypto.Cipher import AES

from app.adapters.external import wechat

KEY = bytes(range(32))
ENCODING_KEY = base64.b64encode(KEY).decode().rstrip("=")
RANDOM_PREFIX = b"0123456789abcdef"
MESSAGE = "<xml><Content><![CDATA[中文消息]]></Content></xml>"


@pytest.fixture
def crypt(monkeypatch):
    """
    固定协议随机前缀，使用测试密钥而非真实企业凭据。
    """
    monkeypatch.setattr(wechat.Prpcrypt, "get_random_str", staticmethod(lambda: RANDOM_PREFIX))
    return wechat.WXBizMsgCrypt("token", ENCODING_KEY, "corp-id")


def test_encrypt_failure_returns_original_error_code(crypt, monkeypatch):
    """
    AES 加密失败应返回约定错误码，不能对 None 解码而抛出额外异常。
    """
    cipher = Mock(encrypt=Mock(side_effect=ValueError("encryption failed")))
    monkeypatch.setattr(wechat.AES, "new", Mock(return_value=cipher))

    assert crypt.EncryptMsg(MESSAGE, "nonce", "1700000000") == (
        wechat.WXBizMsgCrypt_EncryptAES_Error, None,
    )


def test_encrypt_matches_independent_wire_vector_and_roundtrips(crypt):
    """
    独立构造网络字节序、32 字节填充和 SHA1，校验密文及两个解密入口。
    """
    message = MESSAGE.encode()
    plaintext = RANDOM_PREFIX + struct.pack("!I", len(message)) + message + b"corp-id"
    padding = 32 - len(plaintext) % 32
    expected = base64.b64encode(AES.new(KEY, AES.MODE_CBC, KEY[:16]).encrypt(
        plaintext + bytes([padding]) * padding,
    )).decode()
    signature = hashlib.sha1("".join(sorted(["token", "1700000000", "nonce", expected])).encode()).hexdigest()

    code, payload = crypt.EncryptMsg(MESSAGE, "nonce", "1700000000")
    assert code == 0
    root = ET.fromstring(payload)
    assert root.findtext("Encrypt") == expected
    assert root.findtext("MsgSignature") == signature
    assert root.findtext("TimeStamp") == "1700000000"
    assert root.findtext("Nonce") == "nonce"
    assert crypt.DecryptMsg(payload, signature, "1700000000", "nonce") == (0, message)
    assert crypt.VerifyURL(signature, "1700000000", "nonce", expected) == (0, message)


def test_default_timestamp_remains_integer_seconds(crypt, monkeypatch):
    """
    缺省时间戳继续按整数秒生成。
    """
    monkeypatch.setattr(wechat.time, "time", lambda: 1700000000.75)
    code, payload = crypt.EncryptMsg(MESSAGE, "nonce")
    assert code == 0
    assert ET.fromstring(payload).findtext("TimeStamp") == "1700000000"


def test_integer_verification_timestamp_keeps_signature_failure(crypt):
    """
    验证入口收到整数时间戳仍返回原签名错误，不隐式转成可通过验证的字符串。
    """
    assert crypt.VerifyURL("signature", 1700000000, "nonce", "cipher") == (
        wechat.WXBizMsgCrypt_ComputeSignature_Error, None,
    )


def test_missing_recipient_keeps_encrypt_exception_and_decrypt_error(crypt):
    """
    缺失接收方时，加密的异常和解密的接收方错误均保持兼容。
    """
    missing = wechat.WXBizMsgCrypt("token", ENCODING_KEY, None)
    with pytest.raises(AttributeError, match="has no attribute 'encode'"):
        missing.EncryptMsg(MESSAGE, "nonce", "1700000000")
    _, payload = crypt.EncryptMsg(MESSAGE, "nonce", "1700000000")
    signature = ET.fromstring(payload).findtext("MsgSignature")
    assert missing.DecryptMsg(payload, signature, "1700000000", "nonce") == (
        wechat.WXBizMsgCrypt_ValidateCorpid_Error, None,
    )


@pytest.mark.parametrize("xml, expected", [
    ("broken", wechat.WXBizMsgCrypt_ParseXml_Error),
    ("<xml/>", wechat.WXBizMsgCrypt_ParseXml_Error),
    ("<xml><Encrypt/></xml>", wechat.WXBizMsgCrypt_ComputeSignature_Error),
])
def test_invalid_xml_preserves_failure_code(crypt, xml, expected):
    """
    损坏 XML、缺失字段和空字段保持原有的不同错误码。
    """
    assert crypt.DecryptMsg(xml, "signature", "1700000000", "nonce") == (expected, None)


def test_signature_and_recipient_mismatch_preserve_failures(crypt):
    """
    签名错误在解密前拒绝，接收方错误在解密后返回原错误码。
    """
    _, payload = crypt.EncryptMsg(MESSAGE, "nonce", "1700000000")
    signature = ET.fromstring(payload).findtext("MsgSignature")
    assert crypt.DecryptMsg(payload, "bad", "1700000000", "nonce") == (
        wechat.WXBizMsgCrypt_ValidateSignature_Error, None,
    )
    other = wechat.WXBizMsgCrypt("token", ENCODING_KEY, "other-corp")
    assert other.DecryptMsg(payload, signature, "1700000000", "nonce") == (
        wechat.WXBizMsgCrypt_ValidateCorpid_Error, None,
    )


@pytest.mark.parametrize("length", [0, 1, 31, 32, 33, 64])
def test_padding_keeps_legacy_bytes_and_text_contract(length):
    """
    编码使用 bytes，旧解填充入口继续处理字符串，整块也补齐一块。
    """
    data = b"a" * length
    encoded = wechat.PKCS7Encoder().encode(data)
    padding = 32 - length % 32
    assert encoded == data + bytes([padding]) * padding
    assert wechat.PKCS7Encoder.decode(encoded.decode("latin1")) == data.decode("latin1")


@pytest.mark.parametrize("invalid", [None, "", "invalid-key"])
def test_invalid_encoding_key_keeps_format_exception(invalid):
    """
    无效密钥仍在构造阶段报告既有格式错误。
    """
    with pytest.raises(wechat.FormatException, match="EncodingAESKey unvalid"):
        wechat.WXBizMsgCrypt("token", invalid, "corp-id")


@pytest.mark.parametrize("encrypted", [None, "payload"])
def test_sha1_keeps_empty_cipher_failure_and_valid_signature(encrypted):
    """
    空密文继续返回计算失败；有效输入保持原有字典序签名。
    """
    result = wechat.SHA1.getSHA1("token", "1700000000", "nonce", encrypted)
    if encrypted is None:
        assert result == (wechat.WXBizMsgCrypt_ComputeSignature_Error, None)
    else:
        expected = hashlib.sha1("".join(sorted(["token", "1700000000", "nonce", encrypted])).encode()).hexdigest()
        assert result == (0, expected)
