#!/usr/bin/env python
# -*- encoding:utf-8 -*-

""" 对企业微信发送给企业后台的消息加解密示例代码.
@copyright: Copyright (c) 1998-2014 Tencent Inc.

"""
import base64
import hashlib

# ------------------------------------------------------------------------
import logging
import random
import socket
import struct
import time
import xml.etree.cElementTree as ET
from typing import NoReturn, Optional, cast

from Crypto.Cipher import AES

# Description:定义错误码含义
#########################################################################
WXBizMsgCrypt_OK = 0
WXBizMsgCrypt_ValidateSignature_Error = -40001
WXBizMsgCrypt_ParseXml_Error = -40002
WXBizMsgCrypt_ComputeSignature_Error = -40003
WXBizMsgCrypt_IllegalAesKey = -40004
WXBizMsgCrypt_ValidateCorpid_Error = -40005
WXBizMsgCrypt_EncryptAES_Error = -40006
WXBizMsgCrypt_DecryptAES_Error = -40007
WXBizMsgCrypt_IllegalBuffer = -40008
WXBizMsgCrypt_EncodeBase64_Error = -40009
WXBizMsgCrypt_DecodeBase64_Error = -40010
WXBizMsgCrypt_GenReturnXml_Error = -40011

"""
关于Crypto.Cipher模块，ImportError: No module named 'Crypto'解决方案
请到官方网站 https://www.dlitz.net/software/pycrypto/ 下载pycrypto。
下载后，按照README中的“Installation”小节的提示进行pycrypto安装。
"""


class FormatException(Exception):
    """企业微信编码密钥的格式错误。"""
    pass


def throw_exception(message: str, exception_class: type[Exception] = FormatException) -> NoReturn:
    """my define raise exception function"""
    raise exception_class(message)


class SHA1:
    """计算企业微信的消息签名接口"""

    @staticmethod
    def getSHA1(token: Optional[str], timestamp: str | int | None, nonce: Optional[str], encrypt: Optional[str]) -> tuple[int, Optional[str]]:
        """用SHA1算法生成安全签名
        @param token:  票据
        @param timestamp: 时间戳
        @param encrypt: 密文
        @param nonce: 随机字符串
        @return: 安全签名
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
    """提供提取消息格式中的密文及生成回复消息格式的接口"""

    # xml消息模板
    AES_TEXT_RESPONSE_TEMPLATE = """<xml>
<Encrypt><![CDATA[%(msg_encrypt)s]]></Encrypt>
<MsgSignature><![CDATA[%(msg_signaturet)s]]></MsgSignature>
<TimeStamp>%(timestamp)s</TimeStamp>
<Nonce><![CDATA[%(nonce)s]]></Nonce>
</xml>"""

    @staticmethod
    def extract(xmltext: str | bytes) -> tuple[int, Optional[str]]:
        """提取出xml数据包中的加密消息
        @param xmltext: 待提取的xml字符串
        @return: 提取出的加密消息字符串
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
        """生成xml消息
        @param encrypt: 加密后的消息密文
        @param signature: 安全签名
        @param timestamp: 时间戳
        @param nonce: 随机字符串
        @return: 生成的xml字符串
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
    """提供基于PKCS7算法的加解密接口"""

    block_size = 32

    def encode(self, text: bytes) -> bytes:
        """ 对需要加密的明文进行填充补位
        @param text: 需要进行填充补位操作的明文
        @return: 补齐明文字符串
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
        """删除解密后明文的补位字符
        @param decrypted: 解密后的明文
        @return: 删除补位字符后的明文
        """
        pad = ord(decrypted[-1])
        if pad < 1 or pad > 32:
            pad = 0
        return decrypted[:-pad]


class Prpcrypt(object):
    """提供接收和推送给企业微信消息的加解密接口"""

    def __init__(self, key: bytes) -> None:
        """保存已解码的 AES 密钥与 CBC 模式。"""

        # self.key = base64.b64decode(key+"=")
        self.key = key
        # 设置加解密模式为AES的CBC模式
        self.mode = AES.MODE_CBC

    def encrypt(self, text: str, receiveid: Optional[str]) -> tuple[int, Optional[bytes]]:
        """对明文进行加密
        @param text: 需要加密的明文
        @param receiveid: receiveid
        @return: 加密得到的字符串
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
        """对解密后的明文进行补位删除
        @param text: 密文
        @param receiveid: receiveid
        @return: 删除填充补位后的明文
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
            # pkcs7 = PKCS7Encoder()
            # plain_text = pkcs7.encode(plain_text)
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
        """ 随机生成16位字符串
        @return: 16位字符串
        """
        return str(random.randint(1000000000000000, 9999999999999999)).encode()


class WXBizMsgCrypt(object):
    """验证企业微信回调并封装加解密错误码。"""
    # 构造函数
    def __init__(self, sToken: Optional[str], sEncodingAESKey: Optional[str], sReceiveId: Optional[str]) -> None:
        """解码并验证 AES 密钥，保留配置错误的格式异常。"""
        try:
            if sEncodingAESKey is None:
                raise TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")
            self.key = base64.b64decode(sEncodingAESKey + "=")
            assert len(self.key) == 32
        except Exception as err:
            print(str(err))
            throw_exception("[error]: EncodingAESKey unvalid !", FormatException)
            # return WXBizMsgCrypt_IllegalAesKey,None
        self.m_sToken = sToken
        self.m_sReceiveId = sReceiveId

        # 验证URL
        # @param sMsgSignature: 签名串，对应URL参数的msg_signature
        # @param sTimeStamp: 时间戳，对应URL参数的timestamp
        # @param sNonce: 随机串，对应URL参数的nonce
        # @param sEchoStr: 随机串，对应URL参数的echostr
        # @param sReplyEchoStr: 解密之后的echostr，当return返回0时有效
        # @return：成功0，失败返回对应的错误码

    def VerifyURL(self, sMsgSignature: str, sTimeStamp: str | int, sNonce: str, sEchoStr: str) -> tuple[int, Optional[bytes]]:
        """校验 URL 签名后返回解密的验证内容及原始错误码。"""
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
        """加密回复并生成签名 XML，加密失败直接返回对应错误码。"""
        # 将企业回复用户的消息加密打包
        # @param sReplyMsg: 企业号待回复用户的消息，xml格式的字符串
        # @param sTimeStamp: 时间戳，可以自己生成，也可以用URL参数的timestamp,如为None则自动用当前时间
        # @param sNonce: 随机串，可以自己生成，也可以用URL参数的nonce
        # sEncryptMsg: 加密后的可以直接回复用户的密文，包括msg_signature, timestamp, nonce, encrypt的xml格式的字符串,
        # return：成功0，sEncryptMsg,失败返回对应的错误码None
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
        """先验证 XML 与消息签名，再按协议解密并检查接收方。"""
        # 检验消息的真实性，并且获取解密后的明文
        # @param sMsgSignature: 签名串，对应URL参数的msg_signature
        # @param sTimeStamp: 时间戳，对应URL参数的timestamp
        # @param sNonce: 随机串，对应URL参数的nonce
        # @param sPostData: 密文，对应POST请求的数据
        #  xml_content: 解密后的原文，当return返回0时有效
        # @return: 成功0，失败返回对应的错误码
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
