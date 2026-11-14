#!/usr/bin/env python
# -*- encoding:utf-8 -*-

"""
企业微信回调消息的签名验证、加解密和 XML 编解码。

版权所有 (c) 1998-2014 Tencent Inc.，沿用原示例的接口和错误码。
"""
import base64
import hashlib
import logging
import random
import socket
import struct
import time
import xml.etree.cElementTree as ET
from typing import NoReturn, Optional, cast

from Crypto.Cipher import AES

# 成功
WXBizMsgCrypt_OK = 0
# 消息签名校验失败
WXBizMsgCrypt_ValidateSignature_Error = -40001
# XML 消息解析失败
WXBizMsgCrypt_ParseXml_Error = -40002
# 签名计算失败
WXBizMsgCrypt_ComputeSignature_Error = -40003
# AES 密钥格式无效
WXBizMsgCrypt_IllegalAesKey = -40004
# 接收方标识不匹配
WXBizMsgCrypt_ValidateCorpid_Error = -40005
# AES 加密失败
WXBizMsgCrypt_EncryptAES_Error = -40006
# AES 解密失败
WXBizMsgCrypt_DecryptAES_Error = -40007
# 解密正文结构无效
WXBizMsgCrypt_IllegalBuffer = -40008
# Base64 编码失败
WXBizMsgCrypt_EncodeBase64_Error = -40009
# Base64 解码失败
WXBizMsgCrypt_DecodeBase64_Error = -40010
# 回复 XML 生成失败
WXBizMsgCrypt_GenReturnXml_Error = -40011


class FormatException(Exception):
    """
    企业微信编码密钥的格式错误。
    """
    pass


def throw_exception(message: str, exception_class: type[Exception] = FormatException) -> NoReturn:
    """
    抛出指定的消息格式异常，不返回结果。

    :param message: 异常携带的错误说明
    :param exception_class: 要构造并抛出的异常类型，默认为 FormatException
    """
    raise exception_class(message)


class SHA1:
    """
    计算企业微信的消息签名接口
    """

    @staticmethod
    def getSHA1(token: Optional[str], timestamp: str | int | None, nonce: Optional[str], encrypt: Optional[str]) -> tuple[int, Optional[str]]:
        """
        按企业微信协议排序四个字段并计算 SHA1 签名。

        :param token: 企业微信回调配置的令牌
        :param timestamp: 回调时间戳；参与字符串排序时应传入文本
        :param nonce: 回调随机串
        :param encrypt: Base64 密文文本
        :return: 错误码与十六进制签名；计算失败时签名为 None
        """
        try:
            sortlist = [token, timestamp, nonce, encrypt]
            sortlist.sort()
            sha = hashlib.sha1()
            # 其余三项只能为 str 或 None；排序成功已排除 None 和整数时间戳。
            sha.update("".join(cast(list[str], sortlist)).encode())
            return WXBizMsgCrypt_OK, sha.hexdigest()
        except Exception as e:
            logger = logging.getLogger()
            logger.error(e)
            return WXBizMsgCrypt_ComputeSignature_Error, None


class XMLParse:
    """
    提供提取消息格式中的密文及生成回复消息格式的接口
    """

    # xml消息模板
    AES_TEXT_RESPONSE_TEMPLATE = """<xml>
<Encrypt><![CDATA[%(msg_encrypt)s]]></Encrypt>
<MsgSignature><![CDATA[%(msg_signaturet)s]]></MsgSignature>
<TimeStamp>%(timestamp)s</TimeStamp>
<Nonce><![CDATA[%(nonce)s]]></Nonce>
</xml>"""

    @staticmethod
    def extract(xmltext: str | bytes) -> tuple[int, Optional[str]]:
        """
        读取 XML 数据包中的 Encrypt 字段。

        :param xmltext: 企业微信回调的 XML 文本或字节
        :return: 错误码与密文文本；解析失败时密文为 None
        """
        try:
            xml_tree = ET.fromstring(xmltext)
            encrypt = xml_tree.find("Encrypt")
            if encrypt is None:
                raise AttributeError("'NoneType' object has no attribute 'text'")
            return WXBizMsgCrypt_OK, encrypt.text
        except Exception as e:
            logger = logging.getLogger()
            logger.error(e)
            return WXBizMsgCrypt_ParseXml_Error, None

    def generate(self, encrypt: str, signature: Optional[str], timestamp: str, nonce: str) -> str:
        """
        组装企业微信要求的加密回复 XML。

        :param encrypt: Base64 编码后的消息密文
        :param signature: 计算得到的安全签名
        :param timestamp: 秒级时间戳文本
        :param nonce: 回复随机串
        :return: 包含密文、签名、时间戳和随机串的 XML 文本
        """
        resp_dict = {
            'msg_encrypt': encrypt,
            'msg_signaturet': signature,
            'timestamp': timestamp,
            'nonce': nonce,
        }
        resp_xml = self.AES_TEXT_RESPONSE_TEMPLATE % resp_dict
        return resp_xml


class PKCS7Encoder:
    """
    按企业微信约定的 32 字节分组长度填充和移除 PKCS7 补位。
    """

    # 企业微信协议使用 32 字节补位分组，与 AES 的 16 字节分组不同。
    block_size = 32

    def encode(self, text: bytes) -> bytes:
        """
        为明文字节追加 PKCS7 补位，已对齐时仍追加完整分组。

        :param text: 要填充的明文字节
        :return: 长度为 32 字节整数倍的填充结果
        """
        text_length = len(text)
        # 计算需要填充的位数
        amount_to_pad = self.block_size - (text_length % self.block_size)
        if amount_to_pad == 0:
            amount_to_pad = self.block_size
        # 获得补位所用的字符
        pad = chr(amount_to_pad)
        return text + (pad * amount_to_pad).encode()

    @staticmethod
    def decode(decrypted: str) -> str:
        """
        按末尾字符值移除补位，保留原示例的切片行为。

        :param decrypted: 含末尾补位字符的解密文本
        :return: 去掉补位后的文本；补位值不在 1 至 32 时返回空字符串
        """
        pad = ord(decrypted[-1])
        if pad < 1 or pad > 32:
            pad = 0
        return decrypted[:-pad]


class Prpcrypt(object):
    """
    提供接收和推送给企业微信消息的加解密接口
    """

    def __init__(self, key: bytes) -> None:
        """
        保存已解码的 AES 密钥与 CBC 模式。

        :param key: 企业微信提供的 32 字节 AES 密钥，前 16 字节同时用作初始向量
        """

        self.key = key
        # 设置加解密模式为AES的CBC模式
        self.mode = AES.MODE_CBC

    def encrypt(self, text: str, receiveid: Optional[str]) -> tuple[int, Optional[bytes]]:
        """
        按随机前缀、消息长度、正文和接收方标识组装明文，再进行 AES-CBC 加密。

        :param text: 待加密的回复正文
        :param receiveid: 企业微信接收方标识，本实现不接受 None
        :return: 错误码与 Base64 密文字节；加密调用失败时密文为 None
        """
        # 16位随机字符串添加到明文开头
        plaintext = text.encode()
        plaintext = self.get_random_str() + struct.pack("I", socket.htonl(len(plaintext))) + plaintext
        if receiveid is None:
            raise AttributeError("'NoneType' object has no attribute 'encode'")
        plaintext += receiveid.encode()

        # 使用自定义的填充方式对明文进行补位填充
        pkcs7 = PKCS7Encoder()
        plaintext = pkcs7.encode(plaintext)
        # 加密
        cryptor = AES.new(self.key, self.mode, self.key[:16])
        try:
            ciphertext = cryptor.encrypt(plaintext)
            # 使用BASE64对加密后的字符串进行编码
            return WXBizMsgCrypt_OK, base64.b64encode(ciphertext)
        except Exception as e:
            logger = logging.getLogger()
            logger.error(e)
            return WXBizMsgCrypt_EncryptAES_Error, None

    def decrypt(self, text: str | bytes, receiveid: Optional[str]) -> tuple[int, Optional[bytes]]:
        """
        解密企业微信密文，提取正文并核对接收方。

        :param text: Base64 编码的密文
        :param receiveid: 预期的企业微信接收方标识
        :return: 错误码与正文字节；解密、内容拆分或接收方校验失败时正文为 None
        """
        try:
            cryptor = AES.new(self.key, self.mode, self.key[:16])
            # 使用BASE64对密文进行解码，然后AES-CBC解密
            plain_text: bytes = cryptor.decrypt(base64.b64decode(text))
        except Exception as e:
            logger = logging.getLogger()
            logger.error(e)
            return WXBizMsgCrypt_DecryptAES_Error, None
        try:
            pad = plain_text[-1]
            # 去掉补位字符串
            # 去除16位随机字符串
            content = plain_text[16:-pad]
            xml_len = socket.ntohl(struct.unpack("I", content[: 4])[0])
            xml_content = content[4: xml_len + 4]
            from_receiveid = content[xml_len + 4:]
        except Exception as e:
            logger = logging.getLogger()
            logger.error(e)
            return WXBizMsgCrypt_IllegalBuffer, None

        if from_receiveid.decode('utf8') != receiveid:
            return WXBizMsgCrypt_ValidateCorpid_Error, None
        return 0, xml_content

    @staticmethod
    def get_random_str() -> bytes:
        """
        生成加密明文前缀使用的 16 位十进制随机数字。

        :return: 16 个数字字符组成的字节串
        """
        return str(random.randint(1000000000000000, 9999999999999999)).encode()


class WXBizMsgCrypt(object):
    """
    验证企业微信回调并封装加解密错误码。
    """
    def __init__(self, sToken: Optional[str], sEncodingAESKey: Optional[str], sReceiveId: Optional[str]) -> None:
        """
        解码并验证企业微信配置的 AES 密钥。

        :param sToken: 回调签名使用的令牌
        :param sEncodingAESKey: 去掉末尾填充符的 Base64 密钥文本
        :param sReceiveId: 企业微信接收方标识
        :raises FormatException: 密钥缺失、无法解码或解码结果不是 32 字节
        """
        try:
            if sEncodingAESKey is None:
                raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")
            self.key = base64.b64decode(sEncodingAESKey + "=")
            assert len(self.key) == 32
        except Exception as err:
            print(str(err))
            throw_exception("[error]: EncodingAESKey unvalid !", FormatException)
        self.m_sToken = sToken
        self.m_sReceiveId = sReceiveId


    def VerifyURL(self, sMsgSignature: str, sTimeStamp: str | int, sNonce: str, sEchoStr: str) -> tuple[int, Optional[bytes]]:
        """
        核对 URL 验证请求的签名，再解密返回验证内容。

        :param sMsgSignature: URL 查询参数 msg_signature
        :param sTimeStamp: URL 查询参数 timestamp，应传入文本以便参与签名排序
        :param sNonce: URL 查询参数 nonce
        :param sEchoStr: URL 查询参数 echostr 的密文
        :return: 错误码与解密后的验证字节；校验失败时内容为 None
        """
        sha1 = SHA1()
        ret, signature = sha1.getSHA1(self.m_sToken, sTimeStamp, sNonce, sEchoStr)
        if ret != 0:
            return ret, None
        if not signature == sMsgSignature:
            return WXBizMsgCrypt_ValidateSignature_Error, None
        pc = Prpcrypt(self.key)
        ret, sReplyEchoStr = pc.decrypt(sEchoStr, self.m_sReceiveId)
        return ret, sReplyEchoStr

    def EncryptMsg(self, sReplyMsg: str, sNonce: str, timestamp: Optional[str] = None) -> tuple[int, Optional[str]]:
        """
        加密回复正文并生成带签名的 XML 数据包。

        :param sReplyMsg: 待回复的 XML 消息文本
        :param sNonce: 回复随机串，可沿用请求的 nonce
        :param timestamp: 秒级时间戳文本；None 使用当前时间
        :return: 错误码与完整加密回复 XML；加密或签名失败时 XML 为 None
        """
        pc = Prpcrypt(self.key)
        ret, encrypted = pc.encrypt(sReplyMsg, self.m_sReceiveId)
        if ret != 0:
            return ret, None
        # encrypt 的成功分支只返回 base64 bytes，失败时不可对 None 解码。
        encrypt = cast(bytes, encrypted).decode('utf8')
        if timestamp is None:
            timestamp = str(int(time.time()))
        # 生成安全签名
        sha1 = SHA1()
        ret, signature = sha1.getSHA1(self.m_sToken, timestamp, sNonce, encrypt)
        if ret != 0:
            return ret, None
        xmlParse = XMLParse()
        return ret, xmlParse.generate(encrypt, signature, timestamp, sNonce)

    def DecryptMsg(self, sPostData: str | bytes, sMsgSignature: str, sTimeStamp: str, sNonce: str) -> tuple[int, Optional[bytes]]:
        """
        验证 XML 与消息签名，再解密正文并检查接收方。

        :param sPostData: POST 请求携带的加密 XML 数据
        :param sMsgSignature: URL 查询参数 msg_signature
        :param sTimeStamp: URL 查询参数 timestamp 文本
        :param sNonce: URL 查询参数 nonce
        :return: 错误码与原始正文字节；解析、签名或解密失败时正文为 None
        """
        # 验证安全签名
        xmlParse = XMLParse()
        ret, encrypt = xmlParse.extract(sPostData)
        if ret != 0:
            return ret, None
        sha1 = SHA1()
        ret, signature = sha1.getSHA1(self.m_sToken, sTimeStamp, sNonce, encrypt)
        if ret != 0:
            return ret, None
        if not signature == sMsgSignature:
            return WXBizMsgCrypt_ValidateSignature_Error, None
        pc = Prpcrypt(self.key)
        # SHA1 成功已证明密文为字符串；空字段会在签名计算阶段返回错误。
        ret, xml_content = pc.decrypt(cast(str, encrypt), self.m_sReceiveId)
        return ret, xml_content
