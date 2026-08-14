import base64
import hashlib
import hmac
import struct
import sys
import time

from app.runtime.log import logger


class TwoFactorAuth:
    """解析已有验证码或根据共享密钥生成 TOTP 验证码。"""

    def __init__(self, code_or_secret: str):
        """按长度区分用户验证码和 TOTP 共享密钥。"""
        if code_or_secret and len(code_or_secret) >= 16:
            self.code = None
            self.secret = code_or_secret
        else:
            self.code = code_or_secret
            self.secret = None

    @staticmethod
    def __calc(secret_key: str) -> str:
        """按 30 秒时间窗计算六位 TOTP 验证码。"""
        if not secret_key:
            return ""
        try:
            input_time = int(time.time()) // 30
            key = base64.b32decode(secret_key)
            msg = struct.pack(">Q", input_time)
            google_code = hmac.new(key, msg, hashlib.sha1).digest()
            o = (
                google_code[19] & 15
                if sys.version_info > (2, 7)
                else ord(str(google_code[19])) & 15
            )
            google_code = str(
                (struct.unpack(">I", google_code[o: o + 4])[0] & 0x7FFFFFFF) % 1000000
            )
            return f"0{google_code}" if len(google_code) == 5 else google_code
        except Exception as e:
            logger.error(f"计算动态验证码失败：{str(e)}")
            return ""

    def get_code(self) -> str:
        """返回显式验证码，或从共享密钥实时计算。"""
        return self.code or self.__calc(self.secret)
