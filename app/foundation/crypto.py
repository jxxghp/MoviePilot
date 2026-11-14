"""
提供无状态摘要、RSA 和 AES 加解密算法。
"""

import base64
import hashlib
from hashlib import md5
from typing import Optional, Tuple, Union

from Crypto import Random
from Crypto.Cipher import AES
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa


class RSAUtils:
    """
    提供 RSA 密钥生成、校验和加解密辅助能力。
    """

    @staticmethod
    def generate_rsa_key_pair(key_size: int = 2048) -> Tuple[str, str]:
        """
        生成 DER 格式的 RSA 密钥对。

        :param key_size: 密钥长度，单位为位，默认 2048
        :return: 按私钥、公钥顺序返回 Base64 文本，不带 PEM 头尾标记
        """
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
        )

        public_key = private_key.public_key()

        private_key_der = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )

        public_key_der = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )

        private_key_b64 = base64.b64encode(private_key_der).decode("utf-8")
        public_key_b64 = base64.b64encode(public_key_der).decode("utf-8")

        return private_key_b64, public_key_b64

    @staticmethod
    def verify_rsa_keys(private_key: Optional[str], public_key: Optional[str]) -> bool:
        """
        使用 RSA 验证私钥和公钥是否匹配。

        :param private_key: 私钥字符串 (Base64 编码，无标识符)
        :param public_key: 公钥字符串 (Base64 编码，无标识符)
        :return: 如果匹配则返回 True，否则返回 False
        """
        if not private_key or not public_key:
            return False

        try:
            public_key_bytes = base64.b64decode(public_key)
            private_key_bytes = base64.b64decode(private_key)

            loaded_public_key = serialization.load_der_public_key(
                public_key_bytes, backend=default_backend(),
            )

            loaded_private_key = serialization.load_der_private_key(
                private_key_bytes, password=None, backend=default_backend(),
            )

            if not isinstance(loaded_public_key, rsa.RSAPublicKey):
                return False
            if not isinstance(loaded_private_key, rsa.RSAPrivateKey):
                return False

            message = b'test'
            encrypted_message = loaded_public_key.encrypt(
                message,
                asym_padding.OAEP(
                    mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None
                )
            )

            decrypted_message: bytes = loaded_private_key.decrypt(
                encrypted_message,
                asym_padding.OAEP(
                    mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None
                )
            )

            return message == decrypted_message
        except Exception:
            return False


class HashUtils:
    """
    提供常用摘要算法的统一字符串和字节接口。
    """

    @staticmethod
    def md5(data: Union[str, bytes], encoding: str = "utf-8") -> str:
        """
        生成数据的MD5哈希值，并以字符串形式返回。

        :param data: 输入的字符串或字节
        :param encoding: 字符串编码类型，默认使用UTF-8
        :return: 生成的MD5哈希字符串
        """
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.md5(data).hexdigest()

    @staticmethod
    def sha1(data: Union[str, bytes], encoding: str = "utf-8") -> str:
        """
        生成数据的SHA-1哈希值，并以字符串形式返回。

        :param data: 输入的数据，类型为字符串或字节
        :param encoding: 字符串编码类型，默认使用UTF-8
        :return: 生成的SHA-1哈希字符串
        """
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.sha1(data).hexdigest()

    @staticmethod
    def md5_bytes(data: Union[str, bytes], encoding: str = "utf-8") -> bytes:
        """
        生成数据的MD5哈希值，并以字节形式返回。

        :param data: 输入的字符串或字节
        :param encoding: 字符串编码类型，默认使用UTF-8
        :return: 生成的MD5哈希二进制数据
        """
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.md5(data).digest()


class CryptoJsUtils:
    """
    兼容 CryptoJS OpenSSL 格式的 AES 加解密工具。
    """

    @staticmethod
    def bytes_to_key(data: bytes, salt: bytes, output: int = 48) -> bytes:
        """
        按 CryptoJS 兼容算法派生密钥和初始化向量。

        :param data: 用于派生的密码短语字节
        :param salt: 长度必须为 8 字节的盐值
        :param output: 派生结果长度，单位为字节，默认 48
        :return: 拼接后的密钥与初始化向量字节
        :raises AssertionError: 盐值长度不是 8 字节
        """
        # 密钥派生算法扩展自 https://gist.github.com/gsakkis/4546068
        assert len(salt) == 8, len(salt)
        data += salt
        key = md5(data).digest()
        final_key = key
        while len(final_key) < output:
            key = md5(key + data).digest()
            final_key += key
        return final_key[:output]

    @staticmethod
    def encrypt(message: bytes, passphrase: bytes) -> bytes:
        """
        使用 CryptoJS 兼容的 AES-CBC 格式加密消息。

        :param message: 待加密的原始字节，按 PKCS#7 规则补齐
        :param passphrase: 用于派生密钥和初始化向量的密码短语
        :return: 包含 Salted__ 标记和随机盐值的 Base64 编码字节
        """
        # CryptoJS 兼容格式参考以下实现并调整：
        # https://stackoverflow.com/questions/36762098/how-to-decrypt-password-from-javascript-cryptojs-aes-encryptpassword-passphras
        salt = Random.new().read(8)
        key_iv = CryptoJsUtils.bytes_to_key(passphrase, salt, 32 + 16)
        key = key_iv[:32]
        iv = key_iv[32:]
        aes = AES.new(key, AES.MODE_CBC, iv)
        padding_length = 16 - (len(message) % 16)
        padding = bytes([padding_length] * padding_length)
        padded_message = message + padding
        encrypted = aes.encrypt(padded_message)
        # 构建加密数据格式：b"Salted__" + salt + encrypted_message
        salted_encrypted = b"Salted__" + salt + encrypted
        return base64.b64encode(salted_encrypted)

    @staticmethod
    def decrypt(encrypted: Union[str, bytes], passphrase: bytes) -> bytes:
        """
        解密 CryptoJS 兼容的 AES-CBC 消息并移除尾部填充。

        本方法沿用历史填充处理，不额外验证密码正确性或填充完整性。

        :param encrypted: 带 Salted__ 标记的 Base64 文本或字节
        :param passphrase: 加密时使用的密码短语
        :return: 去除尾部填充后的原始字节
        :raises AssertionError: 解码后的数据缺少 Salted__ 标记
        """
        if isinstance(encrypted, str):
            encrypted = encrypted.encode("utf-8")
        encrypted = base64.b64decode(encrypted)
        assert encrypted.startswith(b"Salted__"), "Invalid encrypted data format"
        salt = encrypted[8:16]
        key_iv = CryptoJsUtils.bytes_to_key(passphrase, salt, 32 + 16)
        key = key_iv[:32]
        iv = key_iv[32:]
        aes = AES.new(key, AES.MODE_CBC, iv)
        decrypted_padded = aes.decrypt(encrypted[16:])
        padding_length = decrypted_padded[-1]
        if isinstance(padding_length, str):
            padding_length = ord(padding_length)
        decrypted = decrypted_padded[:-padding_length]
        return decrypted
