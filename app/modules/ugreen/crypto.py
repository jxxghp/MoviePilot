from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass
class UgreenEncryptedRequest:
    url: str
    headers: dict[str, str]
    params: dict[str, str]
    json: dict[str, Any] | None
    aes_key: str
    plain_query: str


class UgreenCrypto:
    """
    绿联接口请求加解密工具。
    """

    def __init__(
        self,
        public_key: str,
        token: str | None = None,
        client_id: str | None = None,
        client_version: str | None = "76363",
        ug_agent: str | None = "PC/WEB",
        language: str = "zh-CN",
    ) -> None:
        self.public_key_pem = self.normalize_public_key(public_key)
        self.public_key = serialization.load_pem_public_key(
            self.public_key_pem.encode("utf-8")
        )
        self.token = token
        self.client_id = client_id
        self.client_version = client_version
        self.ug_agent = ug_agent
        self.language = language

    @staticmethod
    def normalize_public_key(public_key: str) -> str:
        key = (public_key or "").strip().strip('"').replace("\\n", "\n")
        if "BEGIN" in key:
            return key if key.endswith("\n") else f"{key}\n"
        return (
            "-----BEGIN RSA PUBLIC KEY-----\n"
            f"{key}\n"
            "-----END RSA PUBLIC KEY-----\n"
        )

    @staticmethod
    def generate_aes_key() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _flatten_query(prefix: str, value: Any) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                next_prefix = f"{prefix}[{key}]" if prefix else str(key)
                pairs.extend(UgreenCrypto._flatten_query(next_prefix, item))
            return pairs
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for item in value:
                next_prefix = f"{prefix}[]"
                pairs.extend(UgreenCrypto._flatten_query(next_prefix, item))
            return pairs
        if isinstance(value, bool):
            pairs.append((prefix, "true" if value else "false"))
            return pairs
        if value is None:
            pairs.append((prefix, ""))
            return pairs
        pairs.append((prefix, str(value)))
        return pairs

    @classmethod
    def encode_query(cls, params: Mapping[str, Any] | None) -> str:
        if not params:
            return ""
        pairs: list[tuple[str, str]] = []
        for key, value in params.items():
            pairs.extend(cls._flatten_query(str(key), value))
        return urlencode(pairs, doseq=False, quote_via=quote, safe="")

    def rsa_encrypt_long(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        key_size = self.public_key.key_size // 8
        max_chunk = key_size - 11
        encrypted_chunks: list[bytes] = []
        raw = plaintext.encode("utf-8")
        for start in range(0, len(raw), max_chunk):
            chunk = raw[start : start + max_chunk]
            encrypted_chunks.append(
                self.public_key.encrypt(chunk, padding.PKCS1v15())
            )
        return base64.b64encode(b"".join(encrypted_chunks)).decode("utf-8")

    @staticmethod
    def aes_gcm_encrypt(plaintext: str, aes_key: str) -> str:
        iv = os.urandom(12)
        cipher = AESGCM(aes_key.encode("utf-8"))
        encrypted = cipher.encrypt(iv, plaintext.encode("utf-8"), None)
        # encrypt 返回 ciphertext + tag
        return base64.b64encode(iv + encrypted).decode("utf-8")

    @staticmethod
    def aes_gcm_decrypt(payload_b64: str, aes_key: str) -> str:
        raw = base64.b64decode(payload_b64)
        iv = raw[:12]
        encrypted = raw[12:]
        cipher = AESGCM(aes_key.encode("utf-8"))
        plain = cipher.decrypt(iv, encrypted, None)
        decoded: str = plain.decode("utf-8")
        return decoded

    @staticmethod
    def build_security_key(token: str) -> str:
        return hashlib.md5(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_body(data: Any) -> str:
        if isinstance(data, str):
            return data
        if isinstance(data, (bytes, bytearray)):
            return bytes(data).decode("utf-8")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    def encrypt_body(self, data: Any, aes_key: str) -> dict[str, str]:
        plain = self._normalize_body(data)
        return {
            "encrypt_req_body": self.aes_gcm_encrypt(plain, aes_key),
            "req_body_sha256": hashlib.sha256(plain.encode("utf-8")).hexdigest(),
        }

    def build_headers(
        self,
        aes_key: str,
        token: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
        encrypt_token: bool = True,
    ) -> dict[str, str]:
        token_value = token if token is not None else self.token
        headers: dict[str, str] = dict(extra_headers or {})

        if self.client_id:
            headers.setdefault("Client-Id", self.client_id)
        if self.client_version:
            headers.setdefault("Client-Version", self.client_version)
        if self.ug_agent:
            headers.setdefault("UG-Agent", self.ug_agent)
        headers.setdefault("X-Specify-Language", self.language)
        headers.setdefault("Accept", "application/json, text/plain, */*")

        if token_value:
            headers["X-Ugreen-Security-Key"] = self.build_security_key(token_value)
            headers["X-Ugreen-Security-Code"] = self.rsa_encrypt_long(aes_key)
            headers["X-Ugreen-Token"] = (
                self.rsa_encrypt_long(token_value) if encrypt_token else token_value
            )
        return headers

    def build_encrypted_request(
        self,
        url: str,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        data: Any | None = None,
        extra_headers: Mapping[str, str] | None = None,
        token: str | None = None,
        encrypt_token: bool = True,
        encrypt_body: bool = True,
    ) -> UgreenEncryptedRequest:
        """
        构建绿联加密请求。

        关键点：
        - 传入的是明文 `params`；
        - 方法内部会将其序列化并加密成 `encrypt_query`；
        - 业务侧不需要、也不应该手工拼接 `encrypt_query`。
        """
        parsed = urlsplit(url)
        clean_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment)
        )

        url_query_plain = parsed.query
        input_query_plain = self.encode_query(params)
        plain_query = "&".join(filter(None, [url_query_plain, input_query_plain]))

        aes_key = self.generate_aes_key()
        encrypted_query = self.aes_gcm_encrypt(plain_query, aes_key)

        req_json = None
        if data is not None:
            req_json = self.encrypt_body(data, aes_key) if encrypt_body else data

        headers = self.build_headers(
            aes_key=aes_key,
            token=token,
            extra_headers=extra_headers,
            encrypt_token=encrypt_token,
        )
        if req_json is not None:
            headers.setdefault("Content-Type", "application/json")

        _ = method  # 保留参数，便于上层统一调用

        return UgreenEncryptedRequest(
            url=clean_url,
            headers=headers,
            # 绿联接口约定：查询参数统一透传为 encrypt_query
            params={"encrypt_query": encrypted_query},
            json=req_json,
            aes_key=aes_key,
            plain_query=plain_query,
        )

    def decrypt_response(self, response_json: Any, aes_key: str) -> Any:
        if not isinstance(response_json, Mapping):
            return response_json
        encrypted = response_json.get("encrypt_resp_body")
        if not encrypted:
            return response_json
        plain = self.aes_gcm_decrypt(str(encrypted), aes_key)
        try:
            return json.loads(plain)
        except json.JSONDecodeError:
            return plain
