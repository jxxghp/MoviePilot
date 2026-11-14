"""
绿联私有接口的查询串、请求体及响应加解密协议。
"""

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
    """
    绿联请求加密完成后的传输参数与响应解密上下文。

    :param url: 移除原始查询串后的请求 URL，保留路径和片段
    :param headers: 包含设备信息及可选加密认证信息的请求头
    :param params: 只包含 encrypt_query 的查询参数，值为加密后的完整查询串
    :param json: 加密请求体封装或调用方指定的明文请求体；无请求体时为 None
    :param aes_key: 当前请求使用的 AES 密钥文本，用于解密对应响应，不应写入日志
    :param plain_query: 合并 URL 查询串与 params 后的明文，仅供调用链处理，不应写入日志
    """
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
        """
        加载服务端 RSA 公钥，并保存请求认证及客户端标识配置。

        公钥格式无效时由加密库抛出异常，不在此处降级为明文通信。

        :param public_key: 服务端 RSA 公钥，接受 PEM 或不带边界的 Base64 文本
        :param token: 默认认证令牌，单次请求可覆盖
        :param client_id: 可选客户端标识，对应 Client-Id 请求头
        :param client_version: 客户端版本标识，默认 76363
        :param ug_agent: 客户端类型，默认 PC/WEB
        :param language: 接口语言，默认 zh-CN
        """
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
        """
        统一公钥文本中的转义换行并补齐 PEM 边界。

        :param public_key: 可能带引号、转义换行或缺少 PEM 边界的公钥文本
        :return: 以换行结尾的 PEM 文本；已有 BEGIN 标记时保留其边界，不验证密钥有效性
        """
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
        """
        生成单次请求使用的 32 字符密钥文本。

        :return: 随机 UUID 的十六进制文本；加密时直接使用这 32 个 UTF-8 字节
        """
        return uuid.uuid4().hex

    @staticmethod
    def _flatten_query(prefix: str, value: Any) -> list[tuple[str, str]]:
        """
        将嵌套查询结构展开为绿联接口接受的键值对。

        :param prefix: 当前层字段名；映射使用方括号嵌套，序列使用空方括号
        :param value: 待展开的字段值；布尔值转为小写文本，None 转为空字符串
        :return: 保留遍历顺序的键值对列表
        """
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
        """
        按绿联约定将查询参数展开并进行 URL 编码。

        :param params: 可嵌套映射和序列的明文参数；空值表示无查询参数
        :return: 使用百分号编码的查询串；空参数返回空字符串
        """
        if not params:
            return ""
        pairs: list[tuple[str, str]] = []
        for key, value in params.items():
            pairs.extend(cls._flatten_query(str(key), value))
        return urlencode(pairs, doseq=False, quote_via=quote, safe="")

    def rsa_encrypt_long(self, plaintext: str) -> str:
        """
        将 UTF-8 文本按 RSA 公钥容量分块加密。

        每块最多使用密钥字节数减 11 个字节，保持绿联服务端的分块协议。

        :param plaintext: 需要加密的文本，空文本直接返回空字符串
        :return: 各分块使用 PKCS#1 v1.5 填充加密后拼接的 Base64 文本
        """
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
        """
        使用 AES-GCM 加密文本，并把随机向量放在密文前。

        :param plaintext: 需要加密的 UTF-8 文本
        :param aes_key: AES 密钥文本，按 UTF-8 字节作为算法密钥
        :return: Base64 编码的 12 字节随机向量、密文和认证标签；密钥不合法时传播加密库异常
        """
        iv = os.urandom(12)
        cipher = AESGCM(aes_key.encode("utf-8"))
        encrypted = cipher.encrypt(iv, plaintext.encode("utf-8"), None)
        # 加密库返回的结果已经包含密文和认证标签。
        return base64.b64encode(iv + encrypted).decode("utf-8")

    @staticmethod
    def aes_gcm_decrypt(payload_b64: str, aes_key: str) -> str:
        """
        解码并验证绿联 AES-GCM 响应密文。

        :param payload_b64: 包含 12 字节随机向量、密文和认证标签的 Base64 文本
        :param aes_key: AES 密钥文本，按 UTF-8 字节作为算法密钥
        :return: 通过认证校验的 UTF-8 明文；编码无效、密钥错误或认证标签不匹配时传播异常
        """
        raw = base64.b64decode(payload_b64)
        iv = raw[:12]
        encrypted = raw[12:]
        cipher = AESGCM(aes_key.encode("utf-8"))
        plain = cipher.decrypt(iv, encrypted, None)
        decoded: str = plain.decode("utf-8")
        return decoded

    @staticmethod
    def build_security_key(token: str) -> str:
        """
        按绿联协议计算令牌的安全标识。

        :param token: 非空会话令牌文本
        :return: 令牌 UTF-8 字节的 MD5 十六进制摘要，用于 X-Ugreen-Security-Key
        """
        return hashlib.md5(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_body(data: Any) -> str:
        """
        将请求体转换成用于加密与摘要计算的同一份文本。

        :param data: 字符串保持原样，字节按 UTF-8 解码，其他对象以紧凑 JSON 序列化
        :return: 请求体明文；无法解码或序列化时向调用方抛出异常
        """
        if isinstance(data, str):
            return data
        if isinstance(data, (bytes, bytearray)):
            return bytes(data).decode("utf-8")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    def encrypt_body(self, data: Any, aes_key: str) -> dict[str, str]:
        """
        加密请求体并附上同一明文的 SHA-256 摘要。

        :param data: 可转换为文本的请求体
        :param aes_key: AES 密钥文本，按 UTF-8 字节作为算法密钥
        :return: 包含 encrypt_req_body 密文和 req_body_sha256 摘要的字典
        """
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
        """
        合并客户端标识与绿联安全认证请求头。

        :param aes_key: AES 密钥文本，按 UTF-8 字节作为算法密钥
        :param token: 单次请求令牌；None 使用实例令牌，空字符串禁用本次认证头
        :param extra_headers: 调用方额外请求头；客户端通用头保留显式值，安全头会重新计算
        :param encrypt_token: 是否用 RSA 加密 X-Ugreen-Token；默认启用
        :return: 新的请求头字典，不修改传入映射；未提供有效令牌时不添加安全认证头
        """
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
        合并明文查询参数并生成可直接发送的绿联加密请求。

        每次调用生成独立密钥和随机向量。查询串始终加密；请求方法不影响是否加密请求体。

        :param url: 请求地址，其已有查询串会与 params 合并后加密
        :param method: 为统一调用保留的方法名，当前不参与加密决策
        :param params: 明文查询参数，不应预先构造 encrypt_query
        :param data: 请求体；None 表示无请求体
        :param extra_headers: 需要合并的额外请求头
        :param token: 本次令牌；None 使用实例默认令牌，空字符串表示不添加认证信息
        :param encrypt_token: 是否加密认证头中的令牌，默认启用
        :param encrypt_body: 是否加密请求体；关闭时请求体原样保留
        :return: 包含传输参数、AES 密钥和明文查询串的请求上下文
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

        # 请求方法不参与加密，保留参数以兼容统一调用。
        _ = method

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
        """
        解密包含 encrypt_resp_body 的响应，并尝试解析 JSON。

        :param response_json: 已解析的响应对象；非映射或不含加密正文时原样返回
        :param aes_key: AES 密钥文本，按 UTF-8 字节作为算法密钥
        :return: 解密后的 JSON 值；明文不是有效 JSON 时返回文本，解密失败时传播异常
        """
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
