"""
RSA 密钥校验的算法边界及失败返回合同。
"""

import base64
from itertools import product
from typing import Optional

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from app.foundation.crypto import RSAUtils


@pytest.fixture(scope="module")
def key_pairs() -> dict[str, tuple[str, str]]:
    """
    创建两对 RSA 和两种非 RSA 密钥，覆盖算法与身份不匹配的情况。
    """
    private_keys = {
        "rsa_first": rsa.generate_private_key(public_exponent=65537, key_size=1024),
        "rsa_second": rsa.generate_private_key(public_exponent=65537, key_size=1024),
        "ec": ec.generate_private_key(ec.SECP256R1()),
        "ed25519": ed25519.Ed25519PrivateKey.generate(),
    }
    result = {}
    for name, private_key in private_keys.items():
        private_der = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_der = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        result[name] = (
            base64.b64encode(private_der).decode("ascii"),
            base64.b64encode(public_der).decode("ascii"),
        )
    return result


@pytest.mark.parametrize(
    ("private_name", "public_name"),
    list(product(("rsa_first", "rsa_second", "ec", "ed25519"), repeat=2)),
)
def test_verify_rsa_keys_requires_matching_rsa_pair(key_pairs, private_name, public_name) -> None:
    """
    只有对应的 RSA 公私钥通过；错配或非 RSA 算法均返回 False。
    """
    private_key = key_pairs[private_name][0]
    public_key = key_pairs[public_name][1]
    expected = private_name == public_name and private_name.startswith("rsa_")

    assert RSAUtils.verify_rsa_keys(private_key, public_key) is expected


@pytest.mark.parametrize(
    ("private_key", "public_key"),
    [(None, None), ("", ""), ("invalid", None), (None, "invalid"), ("invalid", "invalid")],
)
def test_verify_rsa_keys_rejects_empty_or_invalid_data(
    private_key: Optional[str], public_key: Optional[str],
) -> None:
    """
    缺少密钥或解码失败时保留 False 返回，不向调用方泄漏解析异常。
    """
    assert RSAUtils.verify_rsa_keys(private_key, public_key) is False


@pytest.mark.parametrize("invalid", ["invalid", ""])
def test_verify_rsa_keys_rejects_one_damaged_key(key_pairs, invalid: str) -> None:
    """
    任一侧单独损坏时都应失败，即使另一侧仍是可解析的 RSA 密钥。
    """
    private_key, public_key = key_pairs["rsa_first"]

    assert RSAUtils.verify_rsa_keys(invalid, public_key) is False
    assert RSAUtils.verify_rsa_keys(private_key, invalid) is False
