"""
生成与验证基于时间的一次性密码，并构造绑定 URI。
"""

from typing import Tuple

import pyotp


class OtpUtils:
    """
    提供基于 TOTP 的二次验证辅助能力。
    """

    @staticmethod
    def generate_secret_key(username: str) -> Tuple[str, str]:
        """
        生成 TOTP 密钥及其配置 URI。
        """
        try:
            secret = pyotp.random_base32()
            uri = pyotp.totp.TOTP(secret).provisioning_uri(name='MoviePilot',
                                                           issuer_name='MoviePilot(' + username + ')')
            return secret, uri
        except Exception as err:
            print(str(err))
            return "", ""

    @staticmethod
    def is_legal(otp_uri: str, password: str) -> bool:
        """
        校验二次验证是否正确。
        """
        try:
            valid: bool = pyotp.TOTP(pyotp.parse_uri(otp_uri).secret).verify(password)
            return valid
        except Exception as err:
            print(str(err))
            return False

    @staticmethod
    def check(secret: str, password: str) -> bool:
        """
        校验二次验证是否正确。
        """
        try:
            totp = pyotp.TOTP(secret)
            valid: bool = totp.verify(password)
            return valid
        except Exception as err:
            print(str(err))
            return False

    @staticmethod
    def get_secret(otp_uri: str) -> str:
        """
        获取uri中的secret。
        """
        try:
            secret: str = pyotp.parse_uri(otp_uri).secret
            return secret
        except Exception as err:
            print(str(err))
            return ""
