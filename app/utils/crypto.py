import base64

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding


class RSAUtils:

    @staticmethod
    def generate_rsa_key_pair() -> (str, str):
        """
        生成RSA密钥对并返回Base64编码的公钥和私钥（DER格式）

        :return: Tuple containing Base64 encoded public key and private key
        """
        # 生成RSA密钥对
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        public_key = private_key.public_key()

        # 导出私钥为DER格式
        private_key_der = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )

        # 导出公钥为DER格式
        public_key_der = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )

        # 将DER格式的密钥编码为Base64
        private_key_b64 = base64.b64encode(private_key_der).decode('utf-8')
        public_key_b64 = base64.b64encode(public_key_der).decode('utf-8')

        return private_key_b64, public_key_b64

    @staticmethod
    def verify_rsa_keys(private_key: str, public_key: str) -> bool:
        """
        使用 RSA 验证公钥和私钥是否匹配

        :param private_key: 私钥字符串 (Base64 编码，无标识符)
        :param public_key: 公钥字符串 (Base64 编码，无标识符)
        :return: 如果匹配则返回 True，否则返回 False
        """
        if not private_key or not public_key:
            return False

        try:
            # 解码 Base64 编码的公钥和私钥
            public_key_bytes = base64.b64decode(public_key)
            private_key_bytes = base64.b64decode(private_key)

            # 加载公钥
            public_key = serialization.load_der_public_key(public_key_bytes, backend=default_backend())

            # 加载私钥
            private_key = serialization.load_der_private_key(private_key_bytes, password=None,
                                                             backend=default_backend())

            # 测试加解密
            message = b'test'
            encrypted_message = public_key.encrypt(
                message,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None
                )
            )

            decrypted_message = private_key.decrypt(
                encrypted_message,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None
                )
            )

            return message == decrypted_message
        except Exception as e:
            print(f"RSA 密钥验证失败: {e}")
            return False
