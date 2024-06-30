import base64
import time
from hashlib import md5
from typing import Any

from Crypto import Random
from Crypto.Cipher import AES

from app.schemas.exception import ImmediateException


def retry(ExceptionToCheck: Any,
          tries: int = 3, delay: int = 3, backoff: int = 2, logger: Any = None):
    """
    :param ExceptionToCheck: 需要捕获的异常
    :param tries: 重试次数
    :param delay: 延迟时间
    :param backoff: 延迟倍数
    :param logger: 日志对象
    """

    def deco_retry(f):
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except ImmediateException:
                    raise
                except ExceptionToCheck as e:
                    msg = f"{str(e)}, {mdelay} 秒后重试 ..."
                    if logger:
                        logger.warn(msg)
                    else:
                        print(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)

        return f_retry

    return deco_retry


def bytes_to_key(data: bytes, salt: bytes, output=48) -> bytes:
    # extended from https://gist.github.com/gsakkis/4546068
    assert len(salt) == 8, len(salt)
    data += salt
    key = md5(data).digest()
    final_key = key
    while len(final_key) < output:
        key = md5(key + data).digest()
        final_key += key
    return final_key[:output]


def encrypt(message: bytes, passphrase: bytes) -> bytes:
    """
    CryptoJS 加密原文
    
    This is a modified copy of https://stackoverflow.com/questions/36762098/how-to-decrypt-password-from-javascript-cryptojs-aes-encryptpassword-passphras
    """
    salt = Random.new().read(8)
    key_iv = bytes_to_key(passphrase, salt, 32 + 16)
    key = key_iv[:32]
    iv = key_iv[32:]
    aes = AES.new(key, AES.MODE_CBC, iv)
    length = 16 - (len(message) % 16)
    data = message + (chr(length) * length).encode()
    return base64.b64encode(b"Salted__" + salt + aes.encrypt(data))


def decrypt(encrypted: str | bytes, passphrase: bytes) -> bytes:
    """
    CryptoJS 解密密文
    
    来源同encrypt
    """
    encrypted = base64.b64decode(encrypted)
    assert encrypted[0:8] == b"Salted__"
    salt = encrypted[8:16]
    key_iv = bytes_to_key(passphrase, salt, 32 + 16)
    key = key_iv[:32]
    iv = key_iv[32:]
    aes = AES.new(key, AES.MODE_CBC, iv)
    data = aes.decrypt(encrypted[16:])
    return data[:-(data[-1] if type(data[-1]) == int else ord(data[-1]))]
