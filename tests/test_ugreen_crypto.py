import base64
import hashlib
import json
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.modules.ugreen.crypto import UgreenCrypto


def _generate_rsa_keys() -> tuple[str, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.PKCS1,
    ).decode("utf-8")
    return public_pem, private_key


def _rsa_decrypt_long(private_key: rsa.RSAPrivateKey, payload_b64: str) -> str:
    encrypted = base64.b64decode(payload_b64)
    chunk_size = private_key.key_size // 8
    plain_chunks = []
    for start in range(0, len(encrypted), chunk_size):
        chunk = encrypted[start : start + chunk_size]
        plain_chunks.append(private_key.decrypt(chunk, padding.PKCS1v15()))
    return b"".join(plain_chunks).decode("utf-8")


class UgreenCryptoTest(unittest.TestCase):
    def setUp(self):
        self.public_key, self.private_key = _generate_rsa_keys()
        self.token = "demo-token-for-test"
        self.crypto = UgreenCrypto(
            public_key=self.public_key,
            token=self.token,
            client_id="test-client-id",
        )

    def test_rsa_encrypt_long(self):
        plain = "A" * 400
        encrypted = self.crypto.rsa_encrypt_long(plain)
        self.assertEqual(plain, _rsa_decrypt_long(self.private_key, encrypted))

    def test_build_encrypted_request_and_decrypt_response(self):
        req = self.crypto.build_encrypted_request(
            url="http://127.0.0.1:9999/ugreen/v1/video/homepage/media_list",
            params={"page": 1, "page_size": 50},
            data={"foo": "bar", "count": 2},
        )

        self.assertEqual(
            req.plain_query,
            "page=1&page_size=50",
        )
        self.assertEqual(
            req.plain_query,
            self.crypto.aes_gcm_decrypt(req.params["encrypt_query"], req.aes_key),
        )

        self.assertEqual(
            req.headers["X-Ugreen-Security-Key"],
            hashlib.md5(self.token.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            req.aes_key,
            _rsa_decrypt_long(self.private_key, req.headers["X-Ugreen-Security-Code"]),
        )
        self.assertEqual(
            self.token,
            _rsa_decrypt_long(self.private_key, req.headers["X-Ugreen-Token"]),
        )

        encrypted_body = req.json["encrypt_req_body"]
        body_plain = self.crypto.aes_gcm_decrypt(encrypted_body, req.aes_key)
        self.assertEqual(json.loads(body_plain), {"foo": "bar", "count": 2})
        self.assertEqual(
            req.json["req_body_sha256"],
            hashlib.sha256(body_plain.encode("utf-8")).hexdigest(),
        )

        server_payload = {"code": 0, "msg": "ok", "data": {"items": [1, 2, 3]}}
        resp = {
            "encrypt_resp_body": self.crypto.aes_gcm_encrypt(
                json.dumps(server_payload, ensure_ascii=False, separators=(",", ":")),
                req.aes_key,
            )
        }
        decoded = self.crypto.decrypt_response(resp, req.aes_key)
        self.assertEqual(decoded, server_payload)
