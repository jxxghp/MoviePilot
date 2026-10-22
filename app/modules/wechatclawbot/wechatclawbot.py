"""
微信 ClawBot 的 iLink 协议、扫码登录、持久化状态和消息收发。
"""

import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from app.adapters.network.http import RequestUtils
from app.application.messaging.ingress import forward_message_to_host
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.runtime.cache import FileCache
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting


@dataclass
class ILinkIncomingMessage:
    """
    保存经过 iLink 字段兼容处理的入站消息。

    :param user_id: 必需的发送者标识
    :param text: 提取后的文字；无文字时为 None
    :param username: 发送者显示名；未知时为 None，转发时回退到用户标识
    :param message_id: 可选消息标识，供模块去重
    :param chat_id: 可选来源会话标识
    :param context_token: 可选回复上下文令牌，后续发送时复用
    :param images: 图片引用及格式元数据列表；无图片时为空列表
    :param audio_refs: 语音内部引用列表；无语音时为空列表
    :param files: 文件引用、名称及格式等元数据列表；无附件时为空列表
    :param raw: 原始入站字典，保留用于内部排查，不加入转发报文
    """

    user_id: str
    text: Optional[str] = None
    username: Optional[str] = None
    message_id: Optional[str] = None
    chat_id: Optional[str] = None
    context_token: Optional[str] = None
    images: List[Dict[str, Any]] = field(default_factory=list)
    audio_refs: List[str] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_message_payload(self) -> Dict[str, Any]:
        """
        构造统一消息入口需要的渠道报文。

        :return: 含渠道、用户和内容的字典；空附件及其他 None 字段省略，文本缺失时使用空字符串
        """
        payload = {
            "__channel__": "wechatclawbot",
            "userid": self.user_id,
            "username": self.username or self.user_id,
            "text": self.text or "",
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "context_token": self.context_token,
            "images": self.images or None,
            "audio_refs": self.audio_refs or None,
            "files": self.files or None,
        }
        return {key: value for key, value in payload.items() if value is not None}


class ILinkClient:
    """
    iLink HTTP 客户端，负责二维码登录、消息发送与长轮询。
    """

    # 发送报文携带的 iLink 渠道协议版本
    channel_version = "1.0.2"
    cdn_base_url = "https://novac2c.cdn.weixin.qq.com/c2c"

    def __init__(
        self,
        base_url: str,
        bot_token: Optional[str] = None,
        account_id: Optional[str] = None,
        sync_buf: Optional[str] = None,
        timeout: int = 20,
    ):
        """
        保存一次 iLink 会话的请求地址、凭据和轮询游标。

        :param base_url: iLink API 基址；空值时使用官方服务地址
        :param bot_token: 扫码登录取得的机器人令牌；未登录时为 None
        :param account_id: 可选登录账号标识，用于构建发送报文
        :param sync_buf: 可选消息轮询游标
        :param timeout: 普通请求超时，单位为秒，默认 20
        """
        self.base_url = (base_url or "https://ilinkai.weixin.qq.com").rstrip("/")
        self.bot_token = bot_token
        self.account_id = account_id
        self.sync_buf = sync_buf
        self.timeout = timeout

    def set_credentials(
        self,
        bot_token: Optional[str],
        account_id: Optional[str] = None,
        sync_buf: Optional[str] = None,
    ) -> None:
        """
        更新登录凭据，并按需替换轮询游标。

        :param bot_token: 新的机器人令牌；None 会清空旧值
        :param account_id: 新的账号标识；None 会清空旧值
        :param sync_buf: 新轮询游标；None 表示保留旧游标
        """
        self.bot_token = bot_token
        self.account_id = account_id
        if sync_buf is not None:
            self.sync_buf = sync_buf

    def _headers(self, auth_required: bool = True) -> Dict[str, str]:
        """
        构建请求头，包含必要的身份验证信息。
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "MoviePilot-WechatClawBot/1.0",
        }
        if auth_required and self.bot_token:
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["Authorization"] = f"Bearer {self.bot_token}"
            headers["X-WECHAT-UIN"] = self._build_wechat_uin()
        return headers

    @staticmethod
    def _build_wechat_uin() -> str:
        """
        生成一个随机的微信 UIN，用于标识请求来源。
        """
        random_u32 = random.getrandbits(32)
        return base64.b64encode(str(random_u32).encode("utf-8")).decode("ascii")

    def _with_base_info(self, body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        浅复制请求体并补充缺失的渠道版本信息。

        已有 base_info 字典会复用并可能被原地补充字段。

        :param body: 原始请求体；为空时使用空字典
        :return: 含 base_info.channel_version 的请求体
        """
        payload = dict(body or {})
        base_info = payload.get("base_info")
        if not isinstance(base_info, dict):
            base_info = {}
        base_info.setdefault("channel_version", self.channel_version)
        payload["base_info"] = base_info
        return payload

    @staticmethod
    def _json(resp: Any) -> Dict[str, Any]:
        """
        尝试将响应解析为 JSON，处理异常情况。
        """
        if not resp:
            return {}
        try:
            payload = resp.json()
            return payload if isinstance(payload, dict) else {}
        except Exception:
            text = (getattr(resp, "text", "") or "").strip()
            if not text:
                return {}
            try:
                payload = json.loads(text)
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}

    @staticmethod
    def _short_text(value: Any, max_len: int = 240) -> str:
        """
        将对象转换为简短的字符串描述，用于日志记录。
        """
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            try:
                text = json.dumps(value, ensure_ascii=False)
            except Exception:
                text = str(value)
        else:
            text = str(value)
        text = text.replace("\n", " ").replace("\r", " ").strip()
        if len(text) > max_len:
            return f"{text[:max_len]}..."
        return text

    @staticmethod
    def _normalize_qrcode_url(value: Any) -> Optional[str]:
        """
        规范化二维码展示字段，兼容图片 URL、data URL 与裸 base64。
        """
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        lowered = raw.lower()
        if lowered.startswith("data:image/"):
            return raw
        if lowered.startswith("//"):
            return f"https:{raw}"
        # 某些实现会直接返回二维码图片的 base64 内容，这里自动补齐 data URL 前缀
        if len(raw) >= 128 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", raw):
            return f"data:image/png;base64,{raw}"
        return raw

    @staticmethod
    def _pick_value(obj: Dict[str, Any], keys: List[str]) -> Optional[Any]:
        """
        从字典中尝试获取指定键中的第一个非空值。
        """
        for key in keys:
            if key in obj and obj.get(key) not in (None, ""):
                return obj.get(key)
        return None

    @staticmethod
    def _pick_present_value(obj: Dict[str, Any], keys: List[str]) -> Optional[Any]:
        """
        获取已存在的字段值，允许空字符串和 0 作为有效值。

        sync_buf 这类游标字段可能会被服务端明确置为空串，表示游标已经回到初始状态。
        这里不能复用默认的“忽略空字符串”逻辑，否则会错误沿用上一轮旧游标。
        """
        for key in keys:
            if key in obj and obj.get(key) is not None:
                return obj.get(key)
        return None

    @classmethod
    def _find_first_value(
        cls, data: Any, keys: List[str], max_depth: int = 5
    ) -> Optional[Any]:
        """
        在嵌套结构中递归查找指定键中的第一个非空值。
        """
        if max_depth < 0 or data is None:
            return None
        if isinstance(data, dict):
            direct = cls._pick_value(data, keys)
            if direct not in (None, ""):
                return direct
            for value in data.values():
                found = cls._find_first_value(value, keys, max_depth - 1)
                if found not in (None, ""):
                    return found
        elif isinstance(data, list):
            for value in data:
                found = cls._find_first_value(value, keys, max_depth - 1)
                if found not in (None, ""):
                    return found
        return None

    @classmethod
    def _find_first_list(
        cls, data: Any, prefer_keys: List[str], max_depth: int = 5
    ) -> Optional[List[Any]]:
        """
        在嵌套结构中递归查找第一个符合条件的列表。
        """
        if max_depth < 0 or data is None:
            return None
        if isinstance(data, dict):
            for key in prefer_keys:
                value = data.get(key)
                if isinstance(value, list):
                    return value
            for value in data.values():
                found = cls._find_first_list(value, prefer_keys, max_depth - 1)
                if found is not None:
                    return found
        elif isinstance(data, list):
            if data and all(isinstance(item, dict) for item in data):
                return data
            for value in data:
                found = cls._find_first_list(value, prefer_keys, max_depth - 1)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _ok(payload: Dict[str, Any]) -> bool:
        """
        检查 API 响应是否表示成功。
        """
        if not payload:
            return False
        code = payload.get("errcode")
        if code is None:
            code = payload.get("code")
        if code is None:
            code = payload.get("ret")
        if code is None:
            err = payload.get("errmsg") or payload.get("error") or payload.get(
                "error_msg"
            )
            if err and str(err).strip().lower() not in {"ok", "success", "succeed"}:
                return False
            state = payload.get("status") or payload.get("state")
            if isinstance(state, str) and state.strip().lower() in {
                "error",
                "failed",
                "fail",
            }:
                return False
            return True
        try:
            return int(str(code)) == 0
        except Exception:
            return str(code).strip().lower() in {"0", "ok", "success", "succeed"}

    def _is_send_success(self, payload: Dict[str, Any]) -> bool:
        """
        检查 API 响应中是否包含明确的发送成功信号。
        """
        if not payload:
            return False
        code = self._find_first_value(
            payload, ["errcode", "code", "ret", "result_code", "status_code"]
        )
        if code is not None:
            try:
                return int(str(code)) == 0
            except Exception:
                return str(code).strip().lower() in {"0", "ok", "success", "succeed"}
        success_flag = self._find_first_value(
            payload, ["success", "ok", "is_success", "sent"]
        )
        if isinstance(success_flag, bool):
            return success_flag
        if success_flag is not None:
            if str(success_flag).strip().lower() in {
                "1",
                "true",
                "ok",
                "success",
                "succeed",
                "sent",
            }:
                return True
        state = self._find_first_value(payload, ["status", "state", "send_status"])
        if state is not None:
            if str(state).strip().lower() in {"ok", "success", "succeed", "sent", "done"}:
                return True
            if str(state).strip().lower() in {"failed", "error", "denied"}:
                return False
        err_text = self._find_first_value(payload, ["errmsg", "error", "error_msg", "detail"])
        if err_text is not None and str(err_text).strip():
            err_value = str(err_text).strip().lower()
            if err_value not in {"ok", "success", "succeed", "sent"}:
                return False
        return False

    def _is_send_explicit_failure(self, payload: Dict[str, Any]) -> bool:
        """
        检查 API 响应中是否包含明确的发送失败信号。
        """
        if not payload:
            return False
        code = self._find_first_value(
            payload, ["errcode", "code", "ret", "result_code", "status_code"]
        )
        if code is not None:
            try:
                return int(str(code)) != 0
            except Exception:
                return str(code).strip().lower() not in {"0", "ok", "success", "succeed"}
        success_flag = self._find_first_value(
            payload, ["success", "ok", "is_success", "sent"]
        )
        if isinstance(success_flag, bool):
            return not success_flag
        if success_flag is not None:
            if str(success_flag).strip().lower() in {
                "0",
                "false",
                "fail",
                "failed",
                "error",
                "denied",
            }:
                return True
        state = self._find_first_value(payload, ["status", "state", "send_status"])
        if state is not None:
            if str(state).strip().lower() in {
                "failed",
                "error",
                "denied",
                "forbidden",
                "blocked",
            }:
                return True
        err_text = self._find_first_value(payload, ["errmsg", "error", "error_msg", "detail"])
        if err_text is not None and str(err_text).strip():
            if str(err_text).strip().lower() not in {"ok", "success", "succeed", "sent"}:
                return True
        return False

    def _is_send_http_success(self, resp: Any, payload: Dict[str, Any]) -> bool:
        """
        兼容仅以 HTTP 状态确认发送结果的 iLink 服务。

        :param resp: 原始 HTTP 响应
        :param payload: 已解析的响应内容
        :return: HTTP 为 2xx 且内容没有显式失败标记时返回 True；空响应体也可判定成功
        """
        if resp is None:
            return False
        status_code = getattr(resp, "status_code", None)
        if status_code is None:
            return False
        try:
            status_ok = 200 <= int(status_code) < 300
        except Exception:
            status_ok = False
        if not status_ok:
            return False
        if not payload:
            return True
        return not self._is_send_explicit_failure(payload)

    @staticmethod
    def _build_user_candidates(to_user: str) -> List[str]:
        """
        为目标用户 ID 构建多种格式的候选列表以提高匹配成功率。
        """
        raw = str(to_user or "").strip()
        if not raw:
            return []
        candidates = [raw]
        if "@" in raw:
            candidates.append(raw.split("@", 1)[0])
        if raw.endswith("@im.wechat"):
            candidates.append(raw[: -len("@im.wechat")])
        else:
            candidates.append(f"{raw}@im.wechat")
        uniq: List[str] = []
        for item in candidates:
            value = str(item or "").strip()
            if value and value not in uniq:
                uniq.append(value)
        return uniq

    @staticmethod
    def _build_text_payloads(user_id: str, text: str) -> List[Dict[str, Any]]:
        """
        构建多种格式的纯文本消息报文以兼容不同接口。
        """
        return [
            {"to_user": user_id, "msg_type": "text", "text": {"content": text}},
            {"to_user": user_id, "msg_type": "text", "text": text},
            {"touser": user_id, "msgtype": "text", "text": {"content": text}},
            {"touser": user_id, "msgtype": "text", "text": text},
            {"to": user_id, "type": "text", "content": text},
            {"to_user_id": user_id, "msg_type": "text", "content": text},
        ]

    @staticmethod
    def _aes_ecb_padded_size(plaintext_size: int) -> int:
        """
        计算 AES ECB 加密后的填充大小。
        """
        return ((int(plaintext_size) + 1 + 15) // 16) * 16

    @staticmethod
    def _encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
        """
        使用 AES ECB 模式对明文进行加密。
        """
        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.encrypt(pad(plaintext, AES.block_size))

    @staticmethod
    def _encode_media_aes_key(aeskey: bytes) -> str:
        """
        将媒体 AES 密钥进行 Base64 编码。
        """
        return base64.b64encode(aeskey.hex().encode("ascii")).decode("ascii")

    def _build_protocol_text_payload(
        self, user_id: str, text: str, context_token: Optional[str]
    ) -> Dict[str, Any]:
        """
        构建协议层标准文本消息报文。
        """
        msg = {
            "from_user_id": str(self.account_id or ""),
            "to_user_id": user_id,
            "client_id": f"mp-{uuid.uuid4()}",
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        return {"msg": msg}

    def _build_protocol_image_payload(
        self,
        user_id: str,
        context_token: Optional[str],
        download_param: str,
        aeskey_b64: str,
        cipher_size: int,
    ) -> Dict[str, Any]:
        """
        构建协议层标准图片消息报文。
        """
        msg: Dict[str, Any] = {
            "from_user_id": str(self.account_id or ""),
            "to_user_id": user_id,
            "client_id": f"mp-{uuid.uuid4()}",
            "message_type": 2,
            "message_state": 2,
            "item_list": [
                {
                    "type": 2,
                    "image_item": {
                        "media": {
                            "encrypt_query_param": download_param,
                            "aes_key": aeskey_b64,
                            "encrypt_type": 1,
                        },
                        "mid_size": int(cipher_size),
                    },
                }
            ],
        }
        if context_token:
            msg["context_token"] = context_token
        return {"msg": msg}

    def _build_protocol_file_payloads(
        self,
        user_id: str,
        context_token: Optional[str],
        download_param: str,
        aeskey_b64: str,
        cipher_size: int,
        raw_size: int,
        file_name: str,
        mime_type: str,
        file_md5: str,
    ) -> List[Dict[str, Any]]:
        """
        构建协议层标准文件消息报文（含多种格式候选）。
        """
        media = {
            "encrypt_query_param": download_param,
            "aes_key": aeskey_b64,
            "encrypt_type": 1,
        }
        file_item = {
            "name": file_name,
            "file_name": file_name,
            "filename": file_name,
            "title": file_name,
            "size": int(raw_size),
            "file_size": int(raw_size),
            "raw_size": int(raw_size),
            "mid_size": int(cipher_size),
            "mime_type": mime_type,
            "content_type": mime_type,
            "md5": file_md5,
            "media": media,
        }
        msg_base: Dict[str, Any] = {
            "from_user_id": str(self.account_id or ""),
            "to_user_id": user_id,
            "client_id": f"mp-{uuid.uuid4()}",
            "message_type": 2,
            "message_state": 2,
        }
        if context_token:
            msg_base["context_token"] = context_token
        protocol_candidates = []
        for item_type in (6, 5, 3):
            msg = dict(msg_base)
            msg["item_list"] = [{"type": item_type, "file_item": dict(file_item)}]
            protocol_candidates.append({"msg": msg})
        simple_candidates = [
            {
                "to_user": user_id,
                "msg_type": "file",
                "file": dict(file_item),
            },
            {
                "touser": user_id,
                "msgtype": "file",
                "file": dict(file_item),
            },
            {"to": user_id, "type": "file", "file": dict(file_item)},
        ]
        return [*protocol_candidates, *simple_candidates]

    def _request_upload_param(
        self,
        to_user: str,
        plaintext: bytes,
        media_types: Optional[List[int]] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[bytes], Optional[int], Optional[str]]:
        """
        按候选媒体类型申请 CDN 上传地址与一次性加密材料。

        :param to_user: iLink 接收用户标识，兼容候选格式由发送流程处理
        :param plaintext: 待上传的原始字节，用于计算大小和摘要
        :param media_types: 按顺序尝试的媒体类型编号；为空时默认图片类型 1
        :return: 上传查询参数、完整上传 URL、AES 密钥、填充后字节数及文件标识的五元组；失败时均为 None
        """
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        filesize = self._aes_ecb_padded_size(rawsize)
        filekey = os.urandom(16).hex()
        aeskey = os.urandom(16)
        last_payload = None
        media_type_candidates = media_types or [1]
        for media_type in media_type_candidates:
            body = self._with_base_info(
                {
                    "filekey": filekey,
                    "media_type": media_type,
                    "to_user_id": to_user,
                    "rawsize": rawsize,
                    "rawfilemd5": rawfilemd5,
                    "filesize": filesize,
                    "no_need_thumb": True,
                    "aeskey": aeskey.hex(),
                }
            )
            url = f"{self.base_url}/ilink/bot/getuploadurl"
            resp = RequestUtils(
                headers=self._headers(auth_required=True), timeout=self.timeout
            ).post(url, json=body)
            payload = self._json(resp)
            last_payload = payload or getattr(resp, "text", None)
            upload_param = (
                self._find_first_value(payload, ["upload_param", "uploadParam"])
                if payload
                else None
            )
            upload_full_url = (
                self._find_first_value(payload, ["upload_full_url", "uploadFullUrl", "full_url"])
                if payload
                else None
            )
            if upload_param or upload_full_url:
                return (
                    str(upload_param) if upload_param else None,
                    str(upload_full_url) if upload_full_url else None,
                    aeskey,
                    filesize,
                    filekey,
                )
        logger.warning(f"getuploadurl 失败: resp={self._short_text(last_payload)}")
        return None, None, None, None, None

    def _upload_encrypted_to_cdn(
        self,
        upload_param: Optional[str],
        upload_full_url: Optional[str],
        filekey: str,
        plaintext: bytes,
        aeskey: bytes,
    ) -> Tuple[Optional[str], Optional[int]]:
        """
        按 AES ECB 与 PKCS#7 填充上传加密资源。

        :param upload_param: 可选 CDN 上传查询参数
        :param upload_full_url: 可选完整上传地址，优先于查询参数
        :param filekey: 申请上传时生成的文件标识
        :param plaintext: 待加密的原始字节
        :param aeskey: 申请上传时生成的 AES 密钥
        :return: 下载查询参数和密文字节数；HTTP 非 200 或缺少下载参数时返回两个 None
        """
        ciphertext = self._encrypt_aes_ecb(plaintext, aeskey)
        if upload_full_url:
            upload_url = str(upload_full_url).strip()
        elif upload_param:
            upload_url = (
                f"{self.cdn_base_url}/upload?encrypted_query_param={quote(str(upload_param), safe='')}&"
                f"filekey={quote(filekey, safe='')}"
            )
        else:
            logger.warning("CDN 上传失败: 缺少 upload_url 参数")
            return None, None
        resp = RequestUtils(
            headers={"Content-Type": "application/octet-stream"},
            timeout=self.timeout,
        ).post(upload_url, data=ciphertext)
        if getattr(resp, "status_code", None) != 200:
            logger.warning(
                f"CDN 上传失败: http={getattr(resp, 'status_code', None)}, "
                f"err={self._short_text(getattr(resp, 'text', ''))}"
            )
            return None, None
        download_param = None
        if resp is not None and getattr(resp, "headers", None):
            download_param = resp.headers.get("x-encrypted-param")
        if not download_param:
            logger.warning("CDN 上传成功但缺少 x-encrypted-param")
            return None, None
        return str(download_param), len(ciphertext)

    def _send_payload_candidates(
        self,
        to_user: str,
        payload_candidates: List[Dict[str, Any]],
        url_candidates: Optional[List[str]] = None,
    ) -> bool:
        """
        按候选接口与报文逐次尝试发送，首个成功结果即结束。

        外层按用户候选重复尝试同一组报文，兼容调用方传入的不同版本报文结构。

        :param to_user: iLink 接收用户标识，兼容候选格式由发送流程处理
        :param payload_candidates: 完整候选报文；本方法不会根据循环中的用户候选改写其接收字段
        :param url_candidates: 候选发送地址；为空时尝试默认地址及带 bot_type 的地址
        :return: 明确业务成功或 HTTP 2xx 且无显式失败时返回 True；全部尝试失败返回 False
        """
        url_candidates = url_candidates or [
            f"{self.base_url}/ilink/bot/sendmessage",
            f"{self.base_url}/ilink/bot/sendmessage?bot_type=3",
        ]
        last_error = ""
        for user_id in self._build_user_candidates(to_user):
            for url in url_candidates:
                for index, body in enumerate(payload_candidates, start=1):
                    request_body = self._with_base_info(body)
                    resp = RequestUtils(
                        headers=self._headers(auth_required=True), timeout=self.timeout
                    ).post(url, json=request_body)
                    payload = self._json(resp)
                    if self._is_send_success(payload) or self._is_send_http_success(resp, payload):
                        logger.info(f"发送消息成功: to_user={user_id}, variant={index}")
                        return True
                    http_code = getattr(resp, "status_code", None)
                    err_msg = (
                        self._find_first_value(payload, ["errmsg", "message", "error", "detail"])
                        if payload
                        else None
                    )
                    if not err_msg and resp is not None:
                        err_msg = self._short_text(getattr(resp, "text", ""))
                    last_error = f"http={http_code}, err={self._short_text(err_msg)}"
                    logger.debug(
                        f"发送候选失败: to_user={user_id}, variant={index}, "
                        f"{last_error}, req={self._short_text(request_body)}, "
                        f"resp={self._short_text(payload)}"
                    )
        logger.warning(f"发送消息失败: to_user={to_user}, {last_error}")
        return False

    def get_qrcode(self) -> Dict[str, Any]:
        """
        请求登录二维码并兼容不同响应字段名称。

        :return: 含 success、二维码标识、图片或登录地址、原始响应及可选错误说明的字典
        """
        url = f"{self.base_url}/ilink/bot/get_bot_qrcode?bot_type=3"
        logger.debug(f"请求二维码: {url}")
        resp = RequestUtils(
            headers=self._headers(auth_required=False), timeout=self.timeout
        ).get_res(url)
        payload = self._json(resp)
        if not payload:
            return {"success": False, "message": "获取二维码失败"}
        data = payload.get("data") or payload.get("result") or payload
        qrcode = data.get("qrcode") or data.get("qr_code") or data.get(
            "qrcode_id"
        ) or data.get("ticket")
        qrcode_url = (
            data.get("qrcode_url")
            or data.get("url")
            or data.get("qrcodeUrl")
            or data.get("qr_url")
            or data.get("qrcode_img_content")
            or data.get("qrcode_img_url")
            or data.get("qr_img")
        )
        qrcode_url = self._normalize_qrcode_url(qrcode_url)
        if not qrcode_url and qrcode:
            qrcode_url = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={qrcode}&bot_type=3"
        return {
            "success": self._ok(payload) and bool(qrcode or qrcode_url),
            "qrcode": qrcode,
            "qrcode_url": qrcode_url,
            "raw": payload,
            "message": payload.get("errmsg") or payload.get("message"),
        }

    def get_qrcode_status(self, qrcode: str) -> Dict[str, Any]:
        """
        查询二维码状态，并把返回的登录凭据写入当前客户端。

        :param qrcode: 待查询的二维码标识或票据
        :return: 含 success、状态、令牌、账号和原始响应的字典；空响应使用 waiting 状态并标记失败
        """
        url = f"{self.base_url}/ilink/bot/get_qrcode_status"
        resp = RequestUtils(
            headers=self._headers(auth_required=False), timeout=self.timeout
        ).get_res(url, params={"qrcode": qrcode})
        payload = self._json(resp)
        if not payload:
            retry_resp = RequestUtils(
                headers=self._headers(auth_required=False), timeout=self.timeout
            ).get_res(f"{url}?qrcode={qrcode}")
            payload = self._json(retry_resp)
        if not payload:
            return {
                "success": False,
                "status": "waiting",
                "token": None,
                "account_id": None,
                "raw": {},
                "message": "二维码状态接口返回空响应",
            }
        data = payload.get("data") or payload.get("result") or payload
        token = (
            data.get("bot_token")
            or data.get("token")
            or data.get("access_token")
            or self._find_first_value(
                data, ["bot_token", "access_token", "token", "jwt", "auth_token"]
            )
        )
        account_id = (
            data.get("account_id")
            or data.get("ilink_bot_id")
            or data.get("wxid")
            or data.get("uid")
            or data.get("user_id")
            or self._find_first_value(
                data,
                [
                    "account_id",
                    "ilink_bot_id",
                    "wxid",
                    "uid",
                    "user_id",
                    "from_user",
                    "from_uid",
                ],
            )
        )
        base_url = (
            data.get("baseurl")
            or data.get("base_url")
            or payload.get("baseurl")
            or payload.get("base_url")
        )
        if token:
            self.bot_token = token
        if account_id:
            self.account_id = str(account_id)
        state = (
            data.get("status")
            or data.get("state")
            or payload.get("status")
            or payload.get("state")
            or self._find_first_value(data, ["status", "state", "scan_status"])
            or "waiting"
        )
        return {
            "success": self._ok(payload),
            "status": str(state).lower(),
            "token": token,
            "account_id": account_id,
            "base_url": base_url,
            "qrcode_url": self._normalize_qrcode_url(
                data.get("qrcode_url")
                or data.get("url")
                or data.get("qrcodeUrl")
                or data.get("qr_url")
                or data.get("qrcode_img_content")
                or data.get("qrcode_img_url")
                or data.get("qr_img")
            ),
            "raw": payload,
            "message": payload.get("errmsg") or payload.get("message"),
        }

    def send_text(self, to_user: str, text: str, context_token: Optional[str] = None) -> bool:
        """
        按标准与兼容报文格式发送纯文本。

        :param to_user: iLink 接收用户标识，兼容候选格式由发送流程处理
        :param text: 必需的非空正文
        :param context_token: 最近一次入站消息携带的可选会话上下文令牌
        :return: 首个候选发送成功返回 True；凭据或内容缺失及全部尝试失败返回 False
        """
        if not self.bot_token:
            logger.warning("发送消息失败：bot token 未配置")
            return False
        if not to_user or not text:
            logger.warning("发送消息失败：to_user 或 text 为空")
            return False
        payload_candidates = [
            self._build_protocol_text_payload(
                user_id=str(to_user), text=text, context_token=context_token
            ),
            *self._build_text_payloads(str(to_user), text),
        ]
        return self._send_payload_candidates(to_user=to_user, payload_candidates=payload_candidates)

    def send_image_text_png(
        self,
        to_user: str,
        image_bytes: bytes,
        text: str,
        context_token: Optional[str] = None,
    ) -> bool:
        """
        加密上传图片，再分别发送文字和图片两条消息。

        失败时不撤回已发送的文字，继续尝试其他用户候选可能重复发送前一部分。

        :param to_user: iLink 接收用户标识，兼容候选格式由发送流程处理
        :param image_bytes: 必需的图片原始字节，不在此方法转换格式
        :param text: 必需的非空正文
        :param context_token: 最近一次入站消息携带的可选会话上下文令牌
        :return: 某个用户候选的文字和图片均发送成功返回 True，否则返回 False
        """
        if not self.bot_token:
            logger.warning("发送图文失败：bot token 未配置")
            return False
        if not to_user or not image_bytes or not text:
            logger.warning("发送图文失败：to_user 或 image_bytes 或 text 为空")
            return False
        for user_id in self._build_user_candidates(to_user):
            upload_param, upload_full_url, aeskey, _, filekey = self._request_upload_param(
                user_id, image_bytes, media_types=[1]
            )
            if (not upload_param and not upload_full_url) or not aeskey or not filekey:
                continue
            download_param, cipher_size = self._upload_encrypted_to_cdn(
                upload_param=upload_param,
                upload_full_url=upload_full_url,
                filekey=filekey,
                plaintext=image_bytes,
                aeskey=aeskey,
            )
            if not download_param or not cipher_size:
                continue
            aeskey_b64 = self._encode_media_aes_key(aeskey)
            message_items = [
                {"type": 1, "text_item": {"text": text}},
                {
                    "type": 2,
                    "image_item": {
                        "media": {
                            "encrypt_query_param": download_param,
                            "aes_key": aeskey_b64,
                            "encrypt_type": 1,
                        },
                        "mid_size": int(cipher_size),
                    },
                },
            ]
            sent_all = True
            for item in message_items:
                msg = {
                    "from_user_id": str(self.account_id or ""),
                    "to_user_id": user_id,
                    "client_id": f"mp-{uuid.uuid4()}",
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [item],
                }
                if context_token:
                    msg["context_token"] = context_token
                if not self._send_payload_candidates(
                    to_user=user_id,
                    payload_candidates=[{"msg": msg}],
                ):
                    sent_all = False
                    break
            if sent_all:
                return True
        return False

    def send_image_png(
        self, to_user: str, image_bytes: bytes, context_token: Optional[str] = None
    ) -> bool:
        """
        加密上传并发送图片引用。

        :param to_user: iLink 接收用户标识，兼容候选格式由发送流程处理
        :param image_bytes: 图片原始字节；方法名称不代表会转换为 PNG
        :param context_token: 最近一次入站消息携带的可选会话上下文令牌
        :return: 首个用户候选发送成功返回 True；参数缺失、上传或发送失败返回 False
        """
        if not self.bot_token:
            logger.warning("发送图片失败：bot token 未配置")
            return False
        if not to_user or not image_bytes:
            logger.warning("发送图片失败：to_user 或 image_bytes 为空")
            return False
        for user_id in self._build_user_candidates(to_user):
            upload_param, upload_full_url, aeskey, _, filekey = self._request_upload_param(
                user_id, image_bytes, media_types=[1]
            )
            if (not upload_param and not upload_full_url) or not aeskey or not filekey:
                continue
            download_param, cipher_size = self._upload_encrypted_to_cdn(
                upload_param=upload_param,
                upload_full_url=upload_full_url,
                filekey=filekey,
                plaintext=image_bytes,
                aeskey=aeskey,
            )
            if not download_param or not cipher_size:
                continue
            aeskey_b64 = self._encode_media_aes_key(aeskey)
            body = self._build_protocol_image_payload(
                user_id=user_id,
                context_token=context_token,
                download_param=download_param,
                aeskey_b64=aeskey_b64,
                cipher_size=cipher_size,
            )
            if self._send_payload_candidates(
                to_user=user_id,
                payload_candidates=[body],
            ):
                return True
        return False

    def send_file_bytes(
        self,
        to_user: str,
        file_bytes: bytes,
        file_name: str,
        mime_type: str,
        context_token: Optional[str] = None,
    ) -> bool:
        """
        申请上传、加密文件并尝试兼容附件报文。

        :param to_user: iLink 接收用户标识，兼容候选格式由发送流程处理
        :param file_bytes: 非空文件原始字节
        :param file_name: 显示文件名；空值时使用 attachment
        :param mime_type: 文件 MIME 类型；空值时使用 application/octet-stream
        :param context_token: 最近一次入站消息携带的可选会话上下文令牌
        :return: 首个用户候选发送成功返回 True；参数缺失、上传或发送失败返回 False
        """
        if not self.bot_token:
            logger.warning("发送文件失败：bot token 未配置")
            return False
        if not to_user or not file_bytes:
            logger.warning("发送文件失败：to_user 或 file_bytes 为空")
            return False
        file_name = file_name or "attachment"
        mime_type = mime_type or "application/octet-stream"
        file_md5 = hashlib.md5(file_bytes).hexdigest()
        for user_id in self._build_user_candidates(to_user):
            upload_param, upload_full_url, aeskey, _, filekey = self._request_upload_param(
                user_id, file_bytes, media_types=[2, 6, 1]
            )
            if (not upload_param and not upload_full_url) or not aeskey or not filekey:
                continue
            download_param, cipher_size = self._upload_encrypted_to_cdn(
                upload_param=upload_param,
                upload_full_url=upload_full_url,
                filekey=filekey,
                plaintext=file_bytes,
                aeskey=aeskey,
            )
            if not download_param or not cipher_size:
                continue
            aeskey_b64 = self._encode_media_aes_key(aeskey)
            payload_candidates = self._build_protocol_file_payloads(
                user_id=user_id,
                context_token=context_token,
                download_param=download_param,
                aeskey_b64=aeskey_b64,
                cipher_size=cipher_size,
                raw_size=len(file_bytes),
                file_name=file_name,
                mime_type=mime_type,
                file_md5=file_md5,
            )
            if self._send_payload_candidates(
                to_user=user_id,
                payload_candidates=payload_candidates,
            ):
                return True
        return False

    @classmethod
    def _encode_ref_payload(cls, kind: str, payload: Dict[str, Any]) -> str:
        """
        将附件元信息编码为 wxclaw:// 协议链接。
        """
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"wxclaw://{kind}/{encoded}"

    def _build_attachment_ref(
        self,
        kind: str,
        attachment: Dict[str, Any],
        default_name: Optional[str] = None,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        从附件信息构建 wxclaw 协议引用。

        下行报文可能把下载地址与密钥放在附件的 `media` 子对象中
        （如 `image_item.media.full_url` / `media.aes_key`），也可能直接放在附件顶层，
        这里统一按“附件顶层优先、media 子对象兜底”的顺序提取字段。
        """
        if not isinstance(attachment, dict):
            return None
        media = attachment.get("media")
        media = media if isinstance(media, dict) else {}
        url_keys = ["full_url", "download_url", "url", "cdnurl", "cdn_url", "file_url"]
        download_url = self._pick_value(attachment, url_keys) or self._pick_value(
            media, url_keys
        )
        if not download_url:
            return None
        aeskey_keys = ["aeskey", "aes_key", "encoding_aes_key", "encrypt_key"]
        payload = {
            "url": download_url,
            "aeskey": self._pick_value(attachment, aeskey_keys)
            or self._pick_value(media, aeskey_keys),
            "encrypt_type": self._pick_value(attachment, ["encrypt_type"])
            or self._pick_value(media, ["encrypt_type"]),
            "mime_type": self._pick_value(attachment, ["mime_type", "content_type"])
            or self._pick_value(media, ["mime_type", "content_type"]),
            "name": self._pick_value(attachment, ["name", "filename", "file_name"])
            or default_name,
            "size": self._pick_value(attachment, ["size", "file_size"])
            or self._pick_value(media, ["size", "file_size"]),
        }
        ref = self._encode_ref_payload(kind=kind, payload=payload)
        return ref, payload

    @staticmethod
    def _as_scalar(value: Any) -> Optional[Any]:
        """
        将值转换为标量，过滤掉字典和列表。
        """
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list, tuple, set)):
            return None
        return value

    def _append_item_list_entry(
        self,
        one: Dict[str, Any],
        append_text: Callable[[Any], None],
        images: List[Dict[str, Any]],
        audio_refs: List[str],
        files: List[Dict[str, Any]],
    ) -> None:
        """
        把 item_list 的单个子项分派为文本或媒体附件。

        iLink 下行报文的 item_list 存在两套类型约定：数字 `type` 与子项键名。
        这里按实际出现的子项键名分派，兼容公开资料的 `2 图片/3 语音/4 文件/5 视频`
        以及历史实现里 `file_item` 使用 `type=5/6` 的报文，避免媒体消息被静默丢弃。
        """
        candidates = (
            ("text_item", "text", None),
            ("image_item", "image", None),
            ("voice_item", "voice", "voice.amr"),
            ("file_item", "file", None),
            ("video_item", "file", "video.mp4"),
        )
        for key, kind, default_name in candidates:
            child = one.get(key)
            if not isinstance(child, dict):
                continue
            if kind == "text":
                append_text(child.get("text"))
                return
            if kind == "voice":
                append_text(child.get("content"))
            attachment_ref = self._build_attachment_ref(
                kind, child, default_name=default_name
            )
            if not attachment_ref:
                return
            ref, payload = attachment_ref
            if kind == "voice":
                audio_refs.append(ref)
                return
            entry = {
                "ref": ref,
                "name": payload.get("name"),
                "mime_type": payload.get("mime_type"),
                "size": payload.get("size"),
            }
            if kind == "image":
                images.append(entry)
            else:
                files.append(entry)
            return

    def _build_poll_result(
        self,
        success: bool,
        payload: Optional[Dict[str, Any]] = None,
        message: Optional[str] = None,
        item_count: int = 0,
        parsed_count: int = 0,
    ) -> Dict[str, Any]:
        """
        构建轮询结果摘要。
        """
        payload = payload or {}
        resolved_message = message or self._find_first_value(
            payload, ["errmsg", "message", "error", "error_msg", "detail"]
        )
        return {
            "success": success,
            "raw": payload,
            "message": self._short_text(resolved_message) if resolved_message else None,
            "item_count": item_count,
            "parsed_count": parsed_count,
        }

    def _parse_incoming(self, item: Dict[str, Any]) -> Optional[ILinkIncomingMessage]:
        """
        将 getupdates 返回的原始事件归一化为 MoviePilot 可消费的入站消息。

        iLink 返回结构存在多种嵌套和字段别名，这里集中做兼容处理，统一提取
        用户、消息ID、文本、图片、语音和文件附件。
        """
        if not isinstance(item, dict):
            return None
        message = item
        for key in ["message", "msg", "event", "payload", "data", "body"]:
            child = item.get(key)
            if isinstance(child, dict):
                message = child
                break
        sender_value = (
            message.get("from")
            if isinstance(message.get("from"), dict)
            else message.get("sender")
            if isinstance(message.get("sender"), dict)
            else message.get("user")
            if isinstance(message.get("user"), dict)
            else message.get("from_user")
            if isinstance(message.get("from_user"), dict)
            else {}
        )
        sender: Dict[str, Any] = sender_value if isinstance(sender_value, dict) else {}
        user_id = (
            self._pick_value(sender, ["user_id", "id", "wxid", "uid"])
            or self._pick_value(
                message,
                [
                    "from_user",
                    "from_user_id",
                    "user_id",
                    "uid",
                    "wxid",
                    "from_uid",
                    "fromUser",
                    "fromUserId",
                    "openid",
                ],
            )
            or self._pick_value(
                item,
                [
                    "from_user",
                    "from_user_id",
                    "user_id",
                    "uid",
                    "wxid",
                    "from_uid",
                    "fromUser",
                    "fromUserId",
                    "openid",
                ],
            )
            or self._find_first_value(
                message,
                [
                    "from_user",
                    "from_user_id",
                    "user_id",
                    "sender_id",
                    "uid",
                    "wxid",
                    "from_uid",
                    "fromUserId",
                    "openid",
                ],
            )
        )
        user_id = self._as_scalar(user_id)
        if not user_id:
            return None
        username = (
            self._pick_value(sender, ["name", "nickname", "username", "remark"])
            or self._pick_value(
                message, ["username", "nickname", "from_name", "fromNick", "sender_name"]
            )
            or str(user_id)
        )
        message_id = self._pick_value(
            message, ["message_id", "msg_id", "id", "client_msg_id", "msgId", "seq"]
        ) or self._pick_value(
            item, ["message_id", "msg_id", "id", "client_msg_id", "msgId", "seq"]
        )
        chat_id = self._pick_value(
            message,
            ["chat_id", "conversation_id", "room_id", "chatId", "conversationId", "roomId"],
        ) or self._pick_value(
            item,
            ["chat_id", "conversation_id", "room_id", "chatId", "conversationId", "roomId"],
        )
        context_token = self._pick_value(message, ["context_token", "contextToken"]) or self._pick_value(
            item, ["context_token", "contextToken"]
        )
        text_parts: List[str] = []
        images: List[Dict[str, Any]] = []
        audio_refs: List[str] = []
        files: List[Dict[str, Any]] = []

        def append_text(value: Any) -> None:
            """
            收集非空消息文本并处理嵌套载荷，避免把空字段加入正文。
            """
            if value in (None, ""):
                return
            if isinstance(value, dict):
                value = self._pick_value(value, ["content", "text", "value", "message"])
            if value in (None, ""):
                return
            text_value = str(value).strip()
            if text_value and text_value not in text_parts:
                text_parts.append(text_value)

        msgtype = str(
            message.get("msgtype") or message.get("msg_type") or message.get("type") or ""
        ).lower()
        if msgtype == "text":
            append_text((message.get("text") or {}).get("content") if isinstance(message.get("text"), dict) else None)
            append_text(message.get("content"))
        elif msgtype == "image":
            image_data = message.get("image") or {}
            image_ref = self._build_attachment_ref("image", image_data)
            if image_ref:
                ref, payload = image_ref
                images.append(
                    {
                        "ref": ref,
                        "name": payload.get("name"),
                        "mime_type": payload.get("mime_type"),
                        "size": payload.get("size"),
                    }
                )
        elif msgtype == "file":
            file_data = message.get("file") or {}
            file_ref = self._build_attachment_ref("file", file_data)
            if file_ref:
                ref, payload = file_ref
                files.append(
                    {
                        "ref": ref,
                        "name": payload.get("name"),
                        "mime_type": payload.get("mime_type"),
                        "size": payload.get("size"),
                    }
                )
        elif msgtype == "voice":
            voice_data = message.get("voice") or {}
            append_text((voice_data or {}).get("content"))
            voice_ref = self._build_attachment_ref("voice", voice_data, default_name="voice.amr")
            if voice_ref:
                ref, _ = voice_ref
                audio_refs.append(ref)
        elif msgtype == "mixed":
            for msg_item in ((message.get("mixed") or {}).get("msg_item") or []):
                item_type = str(msg_item.get("msgtype") or msg_item.get("type") or "").lower()
                if item_type == "text":
                    append_text((msg_item.get("text") or {}).get("content"))
                elif item_type == "image":
                    image_ref = self._build_attachment_ref("image", msg_item.get("image") or {})
                    if image_ref:
                        ref, payload = image_ref
                        images.append(
                            {
                                "ref": ref,
                                "name": payload.get("name"),
                                "mime_type": payload.get("mime_type"),
                                "size": payload.get("size"),
                            }
                        )
                elif item_type == "file":
                    file_ref = self._build_attachment_ref("file", msg_item.get("file") or {})
                    if file_ref:
                        ref, payload = file_ref
                        files.append(
                            {
                                "ref": ref,
                                "name": payload.get("name"),
                                "mime_type": payload.get("mime_type"),
                                "size": payload.get("size"),
                            }
                        )
                elif item_type == "voice":
                    append_text(((msg_item.get("voice") or {}).get("content") or "").strip())
                    voice_ref = self._build_attachment_ref(
                        "voice",
                        msg_item.get("voice") or {},
                        default_name="voice.amr",
                    )
                    if voice_ref:
                        ref, _ = voice_ref
                        audio_refs.append(ref)

        raw_item_list = message.get("item_list")
        item_list: List[Any] = raw_item_list if isinstance(raw_item_list, list) else []
        for one in item_list:
            if not isinstance(one, dict):
                continue
            self._append_item_list_entry(one, append_text, images, audio_refs, files)

        if not text_parts:
            append_text(
                self._pick_value(message, ["content", "message", "msg", "text", "body", "msg_content", "msgContent"])
            )
            append_text(
                self._pick_value(item, ["content", "message", "msg", "text", "body", "msg_content", "msgContent"])
            )
            append_text(self._find_first_value(message, ["content", "text", "message", "msg", "body", "cmd"]))

        text = "\n".join(part for part in text_parts if part).strip() or None
        return ILinkIncomingMessage(
            user_id=str(user_id),
            text=text,
            username=str(username) if username else None,
            message_id=str(message_id) if message_id else None,
            chat_id=str(chat_id) if chat_id else None,
            context_token=str(context_token) if context_token else None,
            images=images,
            audio_refs=audio_refs,
            files=files,
            raw=item,
        )

    def _extract_updates(
        self, payload: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        提取轮询结果中的消息列表与游标。

        线上存在两种等价字段命名：较新的实现返回 `get_updates_buf`，
        部分实例仍然返回 `sync_buf`。两者都表示下一轮轮询应携带的游标。
        """
        sync_buf = self._pick_present_value(
            payload, ["get_updates_buf", "sync_buf", "syncBuf"]
        )
        items = payload.get("msgs")
        if isinstance(items, list):
            return items, sync_buf
        return [], sync_buf

    @staticmethod
    def _has_canonical_poll_shape(payload: Dict[str, Any]) -> bool:
        """
        官方响应至少应包含顶层 msgs 列表。
        """
        return isinstance(payload.get("msgs"), list)

    def _resolve_poll_success(self, payload: Dict[str, Any]) -> Optional[bool]:
        """
        判断 getupdates 是否给出了明确的成功/失败信号。

        轮询接口不能沿用“只要没有明显报错就算成功”的宽松策略，否则服务端返回旧消息列表、
        但状态码其实失败时，会被误判为可消费响应，导致旧消息再次进入业务链路。
        返回 `None` 表示响应里没有显式状态，需要交给协议结构继续判断。
        """
        if not payload:
            return False
        code = self._find_first_value(
            payload, ["errcode", "code", "ret", "result_code", "status_code"]
        )
        if code is not None:
            try:
                return int(str(code)) == 0
            except Exception:
                return str(code).strip().lower() in {"0", "ok", "success", "succeed"}
        success_flag = self._find_first_value(
            payload, ["success", "ok", "is_success"]
        )
        if isinstance(success_flag, bool):
            return success_flag
        if success_flag is not None:
            return str(success_flag).strip().lower() in {
                "1",
                "true",
                "ok",
                "success",
                "succeed",
            }
        state = self._find_first_value(payload, ["status", "state"])
        if state is not None:
            lowered = str(state).strip().lower()
            if lowered in {"ok", "success", "succeed", "done"}:
                return True
            if lowered in {"failed", "fail", "error", "denied", "blocked"}:
                return False
        return None

    def poll_updates(
        self, timeout_seconds: int = 25
    ) -> Tuple[List[ILinkIncomingMessage], Optional[str], Dict[str, Any]]:
        """
        执行一次长轮询，仅消费包含顶层 msgs 列表的成功响应。

        有效响应中的新游标会写入当前客户端；缺少显式状态时可依据官方结构接受响应。

        :param timeout_seconds: 长轮询等待预算，单位为秒；HTTP 超时在此基础上增加 10 秒
        :return: 规范化消息列表、当前游标和轮询诊断结果的三元组；失败时消息列表为空且保留旧游标
        """
        if not self.bot_token:
            return [], self.sync_buf, {"success": False, "message": "bot token 未配置"}
        url = f"{self.base_url}/ilink/bot/getupdates"
        request_body = self._with_base_info({"get_updates_buf": self.sync_buf or ""})
        resp = RequestUtils(
            headers=self._headers(auth_required=True),
            timeout=timeout_seconds + 10,
        ).post(url, json=request_body)
        payload = self._json(resp)
        explicit_success = self._resolve_poll_success(payload)
        has_canonical_shape = self._has_canonical_poll_shape(payload)
        # 某些 iLink 部署不会返回 ret/success，但顶层 msgs + sync_buf 已经足够表明
        # 这是一次有效的轮询响应；只有出现显式失败信号时才应拒绝消费。
        success = bool(payload) and (
            explicit_success is True
            or (explicit_success is None and has_canonical_shape)
        )
        last_message = None
        if payload and explicit_success is False:
            last_message = self._find_first_value(
                payload, ["errmsg", "message", "error", "error_msg", "detail"]
            ) or self._short_text(payload)
        if not payload:
            return [], self.sync_buf, self._build_poll_result(
                success=False,
                message="轮询返回空响应",
            )
        if not success:
            return [], self.sync_buf, self._build_poll_result(
                success=False,
                payload=payload,
                message=last_message or "轮询响应未明确成功",
            )
        if not has_canonical_shape:
            logger.warning(
                "getupdates 返回非官方结构，已拒绝消费: %s",
                self._short_text(payload),
            )
            return [], self.sync_buf, self._build_poll_result(
                success=False,
                payload=payload,
                message="轮询响应结构非官方，缺少顶层 msgs 字段",
            )
        items, sync_buf = self._extract_updates(payload)
        parsed: List[ILinkIncomingMessage] = []
        for item in items:
            message = self._parse_incoming(item)
            if message:
                parsed.append(message)
        if sync_buf is not None:
            self.sync_buf = str(sync_buf)
        return parsed, self.sync_buf, self._build_poll_result(
            success=True,
            payload=payload,
            item_count=len(items),
            parsed_count=len(parsed),
        )

    def test_connection(self) -> Tuple[bool, str]:
        """
        请求账号配置接口以探测当前凭据的连通性。

        :return: 成功标记与说明的二元组；缺少令牌返回失败，缺少 ilink_user_id 的特定响应按连通处理
        """
        if not self.bot_token:
            return False, "未登录，缺少 bot token"
        url = f"{self.base_url}/ilink/bot/getconfig"
        resp = RequestUtils(
            headers=self._headers(auth_required=True), timeout=self.timeout
        ).post(url, json={})
        payload = self._json(resp)
        if self._ok(payload):
            return True, "连接正常"
        err_message = payload.get("errmsg") or payload.get("message") or "连接失败"
        if "ilink_user_id required" in str(err_message).strip().lower():
            return True, "连接正常"
        return False, err_message


class WechatClawBot:
    """
    管理微信 ClawBot 登录缓存、轮询线程、互动目标和通知发送。
    """

    _default_base_url = "https://ilinkai.weixin.qq.com"
    # 本地二维码有效期，按最后更新的 Unix 秒级时间判断
    _qrcode_ttl_seconds = 240
    # 无显式目标时优先选择的近期互动窗口，单位为秒
    _active_target_ttl_seconds = 24 * 60 * 60
    # 停止轮询时等待线程退出的上限，单位为秒
    _poll_join_timeout_seconds = 5

    @classmethod
    def _build_cache_key(cls, identity: str) -> str:
        """
        根据稳定渠道身份构建缓存键。
        """
        safe_name = hashlib.md5(str(identity or "wechatclawbot").encode("utf-8")).hexdigest()[:12]
        return f"__wechatclawbot_state_{safe_name}__"

    def __init__(
        self,
        WECHATCLAWBOT_BASE_URL: Optional[str] = None,
        WECHATCLAWBOT_DEFAULT_TARGET: Optional[str] = None,
        WECHATCLAWBOT_ADMINS: Optional[str] = None,
        WECHATCLAWBOT_POLL_TIMEOUT: Optional[int] = None,
        name: Optional[str] = None,
        identity: Optional[str] = None,
        auto_start_polling: bool = True,
        **kwargs: object,
    ) -> None:
        """
        恢复持久化登录状态，并按配置启动消息轮询。

        :param WECHATCLAWBOT_BASE_URL: iLink 服务基址，默认使用官方地址
        :param WECHATCLAWBOT_DEFAULT_TARGET: 可选默认通知目标
        :param WECHATCLAWBOT_ADMINS: 逗号分隔的管理员标识，保存在实例中
        :param WECHATCLAWBOT_POLL_TIMEOUT: 轮询等待秒数；默认 25，最小 10，无效输入回退到 25
        :param name: 显示实例名；为空时使用 wechatclawbot
        :param identity: 稳定缓存身份；为空时回退到实例名
        :param auto_start_polling: 已有令牌或扫码登录成功时是否自动启动轮询；预览实例可禁用
        :param kwargs: 通知实例附加配置，当前不使用
        """
        self._config_name = name or "wechatclawbot"
        self._identity = str(identity or self._config_name)
        self._base_url = (WECHATCLAWBOT_BASE_URL or self._default_base_url).rstrip("/")
        self._default_target = (WECHATCLAWBOT_DEFAULT_TARGET or "").strip() or None
        self._auto_start_polling = bool(auto_start_polling)
        self._admins = [
            admin.strip()
            for admin in str(WECHATCLAWBOT_ADMINS or "").split(",")
            if admin.strip()
        ]
        try:
            self._poll_timeout = max(10, int(WECHATCLAWBOT_POLL_TIMEOUT or 25))
        except Exception:
            self._poll_timeout = 25
        self._cache_key = self._build_cache_key(self._identity)
        self._legacy_cache_key = self._build_cache_key(self._config_name)
        self._filecache = FileCache()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._state = self._load_state()
        if self._state.get("bot_token") and self._auto_start_polling:
            self._start_polling()

    def _load_state(self) -> Dict[str, Any]:
        """
        恢复稳定身份下的登录状态，必要时迁移旧名称缓存。

        稳定身份下无缓存时复制旧名称缓存并删除旧键；损坏内容只在内存中重置，后续保存时覆盖。

        :return: 包含凭据、二维码、游标和互动目标的状态字典；缓存缺失或损坏时使用初始状态
        """
        content = self._filecache.get(self._cache_key)
        if not content and self._legacy_cache_key != self._cache_key:
            content = self._filecache.get(self._legacy_cache_key)
            if content:
                self._filecache.set(self._cache_key, content)
                self._filecache.delete(self._legacy_cache_key)
        if not content:
            return {
                "bot_token": None,
                "account_id": None,
                "sync_buf": None,
                "qrcode": {},
                "known_targets": {},
                "user_context_tokens": {},
                "base_url": self._base_url,
            }
        try:
            data = json.loads(content.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid state")
            data.setdefault("qrcode", {})
            data.setdefault("known_targets", {})
            data.setdefault("user_context_tokens", {})
            data.setdefault("base_url", self._base_url)
            return data
        except Exception as err:
            logger.warning(f"加载登录状态失败，已重置缓存：{err}")
            return {
                "bot_token": None,
                "account_id": None,
                "sync_buf": None,
                "qrcode": {},
                "known_targets": {},
                "user_context_tokens": {},
                "base_url": self._base_url,
            }

    def _save_state(self) -> None:
        """
        将当前状态持久化到文件缓存。
        """
        self._state["base_url"] = self._base_url
        self._filecache.set(
            self._cache_key,
            json.dumps(self._state, ensure_ascii=False).encode("utf-8"),
        )

    def _build_client(self) -> ILinkClient:
        """
        根据当前持久化状态构建一次性的 iLink 客户端。
        """
        return ILinkClient(
            base_url=self._state.get("base_url") or self._base_url,
            bot_token=self._state.get("bot_token"),
            account_id=self._state.get("account_id"),
            sync_buf=self._state.get("sync_buf"),
            timeout=max(self._poll_timeout, 20),
        )

    def _update_state(self, **kwargs: object) -> None:
        """
        更新并保存持久化状态。
        """
        with self._lock:
            self._state.update(kwargs)
            self._save_state()

    def _clear_login_state(self) -> None:
        """
        清除令牌、账号、轮询游标和用户上下文令牌并持久化。

        保留二维码和已知互动目标；轮询循环会因令牌缺失而退出。
        """
        with self._lock:
            self._state["bot_token"] = None
            self._state["account_id"] = None
            self._state["sync_buf"] = None
            self._state["user_context_tokens"] = {}
            self._save_state()

    def _qrcode_expired(self, updated_at: Optional[int]) -> bool:
        """
        检查二维码是否过期。
        """
        if not updated_at:
            return True
        return int(time.time()) - int(updated_at) > self._qrcode_ttl_seconds

    def _remember_target(
        self, user_id: str, username: Optional[str], context_token: Optional[str]
    ) -> None:
        """
        保存发送者最近活动时间及可选回复上下文令牌。

        :param user_id: 发送者标识；为空时忽略
        :param username: 显示名称；为空时回退到用户标识
        :param context_token: 可选新上下文令牌；为空时保留此前令牌
        """
        if not user_id:
            return
        now_ts = int(time.time())
        with self._lock:
            known_targets = self._state.setdefault("known_targets", {})
            known_targets[str(user_id)] = {
                "username": username or str(user_id),
                "last_active": now_ts,
            }
            if context_token:
                tokens = self._state.setdefault("user_context_tokens", {})
                tokens[str(user_id)] = str(context_token)
            self._save_state()

    def _get_context_token(self, user_id: str) -> Optional[str]:
        """
        获取特定用户的上下文 Token。
        """
        tokens = self._state.get("user_context_tokens") or {}
        token = tokens.get(str(user_id))
        return str(token) if token else None

    def _get_targets(self, userid: Optional[str] = None) -> List[str]:
        """
        按显式用户、默认目标和互动记录选择通知接收者。

        :param userid: 显式接收用户；为空时使用默认目标，再回退到近期活跃或全部已知用户
        :return: 目标标识列表；优先选择 24 小时内活跃用户，无活跃用户时回退到全部已知用户并排序
        """
        if userid:
            return [str(userid)]
        if self._default_target:
            return [self._default_target]
        now_ts = int(time.time())
        known_targets = self._state.get("known_targets") or {}
        active_targets = [
            target
            for target, data in known_targets.items()
            if isinstance(data, dict)
            and now_ts - int(data.get("last_active") or 0) <= self._active_target_ttl_seconds
        ]
        if active_targets:
            return sorted(active_targets)
        return sorted(known_targets.keys())

    @staticmethod
    def _short_text(value: Any, max_len: int = 240) -> str:
        """
        将内容缩短用于日志记录。
        """
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            try:
                text = json.dumps(value, ensure_ascii=False)
            except Exception:
                text = str(value)
        else:
            text = str(value)
        text = text.replace("\n", " ").replace("\r", " ").strip()
        if len(text) > max_len:
            return f"{text[:max_len]}..."
        return text

    @staticmethod
    def _split_content(content: str, max_bytes: int = 3000) -> List[str]:
        """
        按 UTF-8 字节数拆分文本并保留完整字符。

        :param content: 完整正文；每块首尾空白会移除
        :param max_bytes: 每块目标字节上限，默认 3000；必须足以容纳单个字符
        :return: 按原顺序排列的非空文本块
        """
        if not content:
            return []
        chunks: List[str] = []
        current = bytearray()
        for line in content.splitlines():
            encoded = (line + "\n").encode("utf-8")
            if len(encoded) > max_bytes:
                if current:
                    chunks.append(current.decode("utf-8", errors="replace").strip())
                    current = bytearray()
                start = 0
                while start < len(encoded):
                    end = min(start + max_bytes, len(encoded))
                    while start < end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
                        end -= 1
                    chunks.append(encoded[start:end].decode("utf-8", errors="replace").strip())
                    start = end
                continue
            if len(current) + len(encoded) > max_bytes:
                chunks.append(current.decode("utf-8", errors="replace").strip())
                current = bytearray()
            current += encoded
        if current:
            chunks.append(current.decode("utf-8", errors="replace").strip())
        return [chunk for chunk in chunks if chunk]

    @staticmethod
    def _compose_text(
        title: Optional[str] = None,
        text: Optional[str] = None,
        link: Optional[str] = None,
    ) -> str:
        """
        组合标题、文本和链接，生成消息内容。
        """
        parts = []
        if title:
            parts.append(str(title).strip())
        if text:
            parts.append(str(text).replace("\n\n", "\n"))
        if link:
            parts.append(f"查看详情：{link}")
        return "\n\n".join(part for part in parts if part).strip()

    @staticmethod
    def _guess_mime_type(file_path: Path, file_bytes: bytes) -> str:
        """
        猜测文件 MIME 类型。
        """
        guessed = mimetypes.guess_type(file_path.name)[0]
        if guessed:
            return guessed
        if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if file_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if file_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        return "application/octet-stream"

    @staticmethod
    def _load_remote_image(image: str) -> Optional[bytes]:
        """
        加载远程图片并返回二进制数据。
        """
        image_url = str(image or "").strip()
        if not image_url:
            return None
        if image_url.startswith("data:image/"):
            try:
                _, raw = image_url.split(",", 1)
                return base64.b64decode(raw)
            except Exception:
                return None
        if image_url.startswith("/"):
            image_url = get_runtime_setting('MP_DOMAIN')(image_url)
        if not image_url.lower().startswith("http"):
            return None
        try:
            resp = RequestUtils(
                timeout=20,
                proxies=get_runtime_setting('PROXY'),
                ua=get_runtime_setting('USER_AGENT'),
            ).get_res(image_url)
            if resp and resp.status_code == 200 and resp.content:
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if not content_type or "image" in content_type:
                    content = resp.content
                    return content if isinstance(content, bytes) else None
        except Exception as err:
            logger.warning(f"加载图片失败：{err}")
        return None

    def get_state(self) -> bool:
        """
        检查本地缓存是否保存了登录令牌。

        :return: 有令牌时返回 True；不代表服务端已验证令牌或轮询线程正在运行
        """
        return bool(self._state.get("bot_token"))

    def stop(self) -> bool:
        """
        请求停止轮询并最多等待 5 秒，保留未退出线程以便重试。

        :return: 轮询线程不存在或已退出返回 True，仍存活返回 False
        """
        self._stop_event.set()
        poll_thread = self._poll_thread
        if (
            poll_thread
            and poll_thread.is_alive()
            and poll_thread is not threading.current_thread()
        ):
            poll_thread.join(timeout=self._poll_join_timeout_seconds)
        if poll_thread and poll_thread.is_alive():
            logger.error("微信 ClawBot 消息轮询线程未在关闭预算内退出")
            return False
        self._poll_thread = None
        return True

    def _start_polling(self) -> None:
        """
        启动消息轮询线程。
        """
        if not self._state.get("bot_token"):
            return
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        logger.info("消息轮询线程已启动")

    def _poll_loop(self) -> None:
        """
        轮询并先保存游标，再同步转发每条入站消息。

        入口转发失败只记录日志，不回滚游标，因此失败消息不会由本循环保证重投。
        轮询异常依次等待 1、2、5、10、30 秒，连续失败 10 次会清除登录状态并结束轮询。
        """
        consecutive_failures = 0
        backoff = [1, 2, 5, 10, 30]
        while not self._stop_event.is_set() and self._state.get("bot_token"):
            try:
                client = self._build_client()
                messages, sync_buf, result = client.poll_updates(
                    timeout_seconds=self._poll_timeout
                )
                if sync_buf is not None and sync_buf != self._state.get("sync_buf"):
                    self._update_state(sync_buf=sync_buf)
                if not result.get("success"):
                    raise RuntimeError(result.get("message") or "poll failed")
                for message in messages:
                    self._remember_target(
                        user_id=message.user_id,
                        username=message.username,
                        context_token=message.context_token,
                    )
                    try:
                        if not self._forward_to_message_chain(
                            message.to_message_payload()
                        ):
                            logger.error(
                                "转发微信 ClawBot 消息失败："
                                f"message_id={message.message_id}"
                            )
                    except Exception as err:
                        logger.error(
                            f"转发微信 ClawBot 消息失败：message_id={message.message_id}, error={err}"
                        )
                consecutive_failures = 0
            except Exception as err:
                consecutive_failures += 1
                delay = backoff[min(consecutive_failures - 1, len(backoff) - 1)]
                logger.warning(f"轮询异常，{delay}s 后重试：{err}")
                if consecutive_failures >= 10:
                    logger.error("轮询连续失败，已清理登录状态")
                    self._clear_login_state()
                    break
                self._stop_event.wait(delay)

    def _forward_to_message_chain(self, payload: Dict[str, Any]) -> bool:
        """
        把 WeChatClawBot 轮询消息同步转交统一消息入口。
        """
        return forward_message_to_host(payload, self._config_name)

    def _build_known_targets(self) -> List[Dict[str, Any]]:
        """
        把已记录的微信接收方状态转换为可查询列表。
        """
        known_targets = self._state.get("known_targets") or {}
        items = []
        for userid, data in known_targets.items():
            if not isinstance(data, dict):
                continue
            items.append(
                {
                    "userid": userid,
                    "username": data.get("username") or userid,
                    "last_active": data.get("last_active"),
                }
            )
        return sorted(items, key=lambda item: item.get("last_active") or 0, reverse=True)

    def refresh_qrcode(self) -> Dict[str, Any]:
        """
        未登录时请求并持久化新二维码；已登录时只返回本地状态。

        :return: 状态字典，二维码更新时含 Unix 秒级时间戳；获取失败时返回接口的失败结果
        """
        if self._state.get("bot_token"):
            return self.get_status(refresh_remote=False)
        client = ILinkClient(
            base_url=self._base_url,
            timeout=max(self._poll_timeout, 20),
        )
        result = client.get_qrcode()
        if not result.get("success"):
            return result
        qrcode = {
            "qrcode": result.get("qrcode"),
            "qrcode_url": result.get("qrcode_url"),
            "status": "waiting",
            "updated_at": int(time.time()),
        }
        self._update_state(qrcode=qrcode)
        return self.get_status(refresh_remote=False)

    def get_status(
        self,
        refresh_remote: bool = True,
        auto_generate_qrcode: bool = False,
    ) -> Dict[str, Any]:
        """
        查询登录与二维码状态，必要时更新持久化凭据并启动轮询。

        状态结果的 success 固定为 True，不代表远端登录校验成功；轮询自动启动受实例配置控制。

        :param refresh_remote: 未登录且已有二维码时是否请求远端扫码状态
        :param auto_generate_qrcode: 二维码缺失或超过本地 240 秒有效期时是否自动生成
        :return: 包含 success、connected、二维码、账号及已知目标的状态字典；connected 只依据本地令牌
        """
        qrcode = self._state.get("qrcode") or {}
        if (
            auto_generate_qrcode
            and not self._state.get("bot_token")
            and (not qrcode.get("qrcode") or self._qrcode_expired(qrcode.get("updated_at")))
        ):
            self.refresh_qrcode()
            qrcode = self._state.get("qrcode") or {}
        if refresh_remote and not self._state.get("bot_token") and qrcode.get("qrcode"):
            client = ILinkClient(
                base_url=self._base_url,
                timeout=max(self._poll_timeout, 20),
            )
            result = client.get_qrcode_status(str(qrcode.get("qrcode")))
            updated_qrcode = dict(qrcode)
            updated_qrcode["status"] = result.get("status") or updated_qrcode.get("status") or "waiting"
            updated_qrcode["updated_at"] = int(time.time())
            if result.get("qrcode_url"):
                updated_qrcode["qrcode_url"] = result.get("qrcode_url")
            update_payload: Dict[str, Any] = {"qrcode": updated_qrcode}
            if result.get("token"):
                update_payload.update(
                    {
                        "bot_token": result.get("token"),
                        "account_id": str(result.get("account_id") or "") or None,
                        "sync_buf": None,
                        "base_url": (result.get("base_url") or self._base_url).rstrip("/"),
                    }
                )
                self._base_url = (result.get("base_url") or self._base_url).rstrip("/")
                self._update_state(**update_payload)
                if self._auto_start_polling:
                    self._start_polling()
            else:
                self._update_state(**update_payload)
            qrcode = self._state.get("qrcode") or {}
        return {
            "success": True,
            "connected": bool(self._state.get("bot_token")),
            "account_id": self._state.get("account_id"),
            "qrcode": qrcode.get("qrcode"),
            "qrcode_url": qrcode.get("qrcode_url"),
            "qrcode_status": qrcode.get("status"),
            "qrcode_updated_at": qrcode.get("updated_at"),
            "known_targets": self._build_known_targets(),
            "default_target": self._default_target,
            "base_url": self._base_url,
        }

    def logout(self) -> Dict[str, Any]:
        """
        请求停止轮询并清空本地登录、二维码及上下文令牌。

        保留已知互动目标；不向远端发送账号注销请求。

        :return: 本地状态清理成功的结果字典；不保证停止请求返回时轮询线程已退出
        """
        self.stop()
        with self._lock:
            self._state["bot_token"] = None
            self._state["account_id"] = None
            self._state["sync_buf"] = None
            self._state["qrcode"] = {}
            self._state["user_context_tokens"] = {}
            self._save_state()
        return {"success": True, "message": "已退出微信 ClawBot 登录"}

    def test_connection(self) -> Tuple[bool, str]:
        """
        使用当前缓存令牌探测 iLink 服务连接。

        :return: 状态与说明的二元组；未登录直接返回失败
        """
        if not self._state.get("bot_token"):
            return False, "未登录，请先扫码完成绑定"
        return self._build_client().test_connection()

    @classmethod
    def migrate_cached_state(
        cls,
        old_name: Optional[str],
        new_name: Optional[str],
        cleanup_old: bool = False,
        overwrite: bool = False,
    ) -> Tuple[bool, str]:
        """
        按配置名称迁移登录缓存。

        :param old_name: 来源配置名，不能为空
        :param new_name: 目标配置名，不能为空
        :param cleanup_old: 复制成功后是否删除旧缓存，默认 False
        :param overwrite: 目标已有缓存时是否覆盖，默认 False
        :return: 是否完成或可安全跳过与说明的二元组；无来源缓存或不覆盖已有目标也视为成功
        """
        source_name = str(old_name or "").strip()
        target_name = str(new_name or "").strip()
        if not source_name or not target_name:
            return False, "旧名称或新名称不能为空"
        if source_name == target_name:
            return True, "通知名称未变化，无需迁移登录缓存"

        cache = FileCache()
        source_key = cls._build_cache_key(source_name)
        target_key = cls._build_cache_key(target_name)
        source_state = cache.get(source_key)
        if not source_state:
            return True, "未找到可迁移的微信 ClawBot 登录缓存"
        if cache.exists(target_key) and not overwrite:
            return True, "新名称下已存在登录缓存，跳过迁移"

        cache.set(target_key, source_state)
        if cleanup_old:
            cache.delete(source_key)
        return True, f"已将微信 ClawBot 登录缓存从 {source_name} 迁移到 {target_name}"

    @classmethod
    def reconcile_cached_states(
        cls, previous: List[Dict[str, Any]], current: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        按稳定身份迁移改名缓存，并删除已移除渠道的缓存。

        :param previous: 修改前完整通知配置列表，使用 id、name 和 type 字段
        :param current: 修改后完整通知配置列表，用稳定 id 判断保留与删除
        :return: 含 success 与说明的同步结果；缓存操作异常向上传播
        """
        cache = FileCache()
        old_by_id = {str(item.get("id")): item for item in previous if item.get("id")}
        current_ids = {str(item.get("id")) for item in current if item.get("id")}
        for item in current:
            if item.get("type") != "wechatclawbot":
                continue
            identity = str(item.get("id") or item.get("name") or "").strip()
            old = old_by_id.get(identity)
            if old and old.get("name") and old.get("name") != item.get("name"):
                legacy_key = cls._build_cache_key(str(old["name"]))
                target_key = cls._build_cache_key(identity)
                if not cache.exists(target_key) and cache.exists(legacy_key):
                    content = cache.get(legacy_key)
                    if content:
                        cache.set(target_key, content)
                cache.delete(legacy_key)
        for item in previous:
            if item.get("type") != "wechatclawbot" or str(item.get("id")) in current_ids:
                continue
            for legacy_identity in (item.get("id"), item.get("name")):
                if legacy_identity:
                    cache.delete(cls._build_cache_key(str(legacy_identity)))
        return {"success": True, "message": "通知渠道缓存已同步"}

    @staticmethod
    def _decode_ref_payload(ref: str, kind: str) -> Optional[Dict[str, Any]]:
        """
        校验本地资源引用类别并解码载荷，非法引用不进入资源读取流程。
        """
        prefix = f"wxclaw://{kind}/"
        if not ref or not ref.startswith(prefix):
            return None
        encoded = ref.replace(prefix, "", 1)
        padding = "=" * (-len(encoded) % 4)
        try:
            payload = json.loads(
                base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode(
                    "utf-8"
                )
            )
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    @staticmethod
    def _decode_aes_key(aeskey: Optional[str]) -> Optional[bytes]:
        """
        解析微信资源使用的 AES 密钥，无法识别时返回 None。
        """
        if not aeskey:
            return None
        value = str(aeskey).strip()
        if not value:
            return None
        if re.fullmatch(r"[0-9a-fA-F]{32}", value):
            try:
                return bytes.fromhex(value)
            except Exception:
                return None
        for candidate in (value, value + "=" * (-len(value) % 4)):
            try:
                decoded = base64.b64decode(candidate)
                if len(decoded) == 16:
                    return decoded
                if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
                    return bytes.fromhex(decoded.decode("ascii"))
            except Exception:
                continue
        return None

    @staticmethod
    def _unpad_bytes(content: bytes) -> Optional[bytes]:
        """
        校验并移除资源明文的块填充，非法填充返回 None。
        """
        if not content:
            return None
        padding_len = content[-1]
        if padding_len <= 0 or padding_len > 16:
            return None
        if content[-padding_len:] != bytes([padding_len]) * padding_len:
            return None
        return content[:-padding_len]

    @classmethod
    def _decrypt_if_needed(
        cls,
        content: bytes,
        aeskey: Optional[str],
        mime_type: Optional[str] = None,
    ) -> bytes:
        """
        按资源协议使用AES-ECB解密并校验PKCS7填充，不根据文件内容猜测加密方式。

        :param content: 下载到的原始字节
        :param aeskey: 可选编码后的 AES 密钥
        :param mime_type: 保留调用兼容的文件MIME类型，不参与密码协议判断
        :return: 完整明文；未声明密钥时原样返回下载内容
        :raises ValueError: 已声明密钥但密钥、密文或填充无效
        """
        del mime_type
        if not aeskey:
            return content
        aes_bytes = cls._decode_aes_key(aeskey)
        if not aes_bytes or not content:
            raise ValueError("附件密钥或密文无效")
        plaintext = AES.new(aes_bytes, AES.MODE_ECB).decrypt(content)
        unpadded = cls._unpad_bytes(plaintext)
        if unpadded is None:
            raise ValueError("附件密文填充校验失败")
        return unpadded

    @staticmethod
    def _guess_binary_mime(content: bytes, default: str = "application/octet-stream") -> str:
        """
        根据资源内容的文件签名识别 MIME，未知内容沿用调用方默认值。
        """
        if not content:
            return default
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"#!AMR"):
            return "audio/amr"
        if content.startswith(b"OggS"):
            return "audio/ogg"
        if content.startswith(b"ID3"):
            return "audio/mpeg"
        if content.startswith(b"RIFF") and b"WAVE" in content[:16]:
            return "audio/wav"
        if content.startswith(b"PK\x03\x04"):
            return "application/zip"
        return default

    def _download_ref_bytes(self, ref: str, kind: str) -> Optional[Tuple[bytes, str, Optional[str]]]:
        """
        下载内部资源引用并尝试兼容解密。

        :param ref: wxclaw:// 引用，包含下载地址及可选密钥、名称和格式
        :param kind: 引用类型，如 image、file 或 voice
        :return: 内容字节、MIME 类型和可选名称的三元组；引用无效或下载失败返回 None
        """
        payload = self._decode_ref_payload(ref=ref, kind=kind)
        if not payload:
            return None
        encryption_type = payload.get("encrypt_type")
        if encryption_type not in (None, 1, "1") or (encryption_type is not None and not payload.get("aeskey")):
            logger.warning("附件使用不支持的加密方式或缺少密钥，已拒绝下载内容")
            return None
        download_url = payload.get("url")
        if not download_url:
            return None
        try:
            resp = RequestUtils(timeout=30).get_res(download_url)
        except Exception as err:
            logger.error(f"下载 {kind} 失败：{err}")
            return None
        if not resp or not resp.content:
            return None
        try:
            content = self._decrypt_if_needed(
                content=resp.content,
                aeskey=payload.get("aeskey"),
                mime_type=payload.get("mime_type"),
            )
        except ValueError:
            logger.warning("附件密文或密钥无效，已拒绝返回未解密内容")
            return None
        mime_type = payload.get("mime_type") or self._guess_binary_mime(
            content,
            (resp.headers.get("Content-Type") or "application/octet-stream").split(
                ";", 1
            )[0],
        )
        return content, mime_type, payload.get("name")

    def download_image_to_data_url(self, image_ref: str) -> Optional[str]:
        """
        下载内部图片引用并编码为数据地址。

        :param image_ref: wxclaw://image/ 内部图片引用
        :return: 包含 MIME 类型的 Base64 数据地址；引用无效或下载失败返回 None
        """
        result = self._download_ref_bytes(ref=image_ref, kind="image")
        if not result:
            return None
        content, mime_type, _ = result
        return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"

    def download_media_bytes(self, media_ref: str) -> Optional[bytes]:
        """
        下载内部文件或语音引用的内容。

        :param media_ref: wxclaw://file/ 或 wxclaw://voice/ 引用
        :return: 下载及兼容解密后的字节；类型不支持或下载失败返回 None
        """
        kind = None
        if media_ref.startswith("wxclaw://file/"):
            kind = "file"
        elif media_ref.startswith("wxclaw://voice/"):
            kind = "voice"
        if not kind:
            return None
        result = self._download_ref_bytes(ref=media_ref, kind=kind)
        return result[0] if result else None

    def send_msg(
        self,
        title: str,
        text: Optional[str] = None,
        image: Optional[str] = None,
        userid: Optional[str] = None,
        link: Optional[str] = None,
        **kwargs: object,
    ) -> Optional[bool]:
        """
        优先发送组合文字，只有无文字时才尝试发送图片。

        各目标独立发送，已发出的分块不会撤回。当前无文字且无可用图片时，
        只要存在目标也会返回 True，此结果不表示实际发出消息。

        :param title: 可选标题
        :param text: 可选正文
        :param image: 可选远程图片或图片数据地址；标题、正文或链接产生文字时忽略
        :param userid: 显式接收用户；为空时使用默认目标，再回退到近期活跃或全部已知用户
        :param link: 追加为详情文字的链接
        :param kwargs: 兼容发送接口的附加参数，当前忽略
        :return: 至少一个目标的全部发送步骤成功时返回 True；无目标或全部发送失败返回 False
        """
        targets = self._get_targets(userid=userid)
        if not targets:
            logger.warning("未找到可发送的微信 ClawBot 目标")
            return False
        content = self._compose_text(title=title, text=text, link=link)
        # 当前 iLink 发送实现会把图文拆成两条消息发送。
        # 为避免用户看到“同一条通知被拆成文本 + 图片”两次触达，这里约定：
        # 只要通知里已经有文本内容，就优先只发文本；只有纯图片通知才发送图片。
        image_bytes = self._load_remote_image(image) if image and not content else None
        ok = False
        for target in targets:
            context_token = self._get_context_token(target)
            if content:
                client = self._build_client()
                sent = True
                for chunk in self._split_content(content):
                    if not client.send_text(
                        to_user=target,
                        text=chunk,
                        context_token=context_token,
                    ):
                        sent = False
                        break
            elif image_bytes:
                sent = self._build_client().send_image_png(
                    to_user=target,
                    image_bytes=image_bytes,
                    context_token=context_token,
                )
            else:
                client = self._build_client()
                sent = True
                for chunk in self._split_content(content):
                    if not client.send_text(
                        to_user=target,
                        text=chunk,
                        context_token=context_token,
                    ):
                        sent = False
                        break
            ok = ok or bool(sent)
        return ok

    def send_file(
        self,
        file_path: str,
        file_name: Optional[str] = None,
        title: Optional[str] = None,
        text: Optional[str] = None,
        userid: Optional[str] = None,
        **kwargs: object,
    ) -> Optional[bool]:
        """
        发送本地附件；图片类型使用图片协议，其他类型使用文件协议。

        :param file_path: 本地普通文件路径；完整读入内存后发送，保留源文件
        :param file_name: 普通附件显示名称；为空时使用路径文件名
        :param title: 兼容发送接口的标题，当前不发送
        :param text: 兼容发送接口的说明，当前不发送
        :param userid: 显式接收用户；为空时使用默认目标，再回退到近期活跃或全部已知用户
        :param kwargs: 兼容发送接口的附加参数，当前忽略
        :return: 至少一个目标的全部发送步骤成功时返回 True；无目标或全部发送失败返回 False
        """
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            logger.warning(f"待发送文件不存在：{file_path}")
            return False
        file_bytes = path.read_bytes()
        effective_name = file_name or path.name
        mime_type = self._guess_mime_type(path, file_bytes)
        targets = self._get_targets(userid=userid)
        if not targets:
            return False
        ok = False
        for target in targets:
            context_token = self._get_context_token(target)
            # send_file 的主语义是发送附件本体，附加文案可以丢弃，避免出现
            # “先发一段说明，再发一个文件”的重复触达体验。
            if mime_type.startswith("image/"):
                sent = self._build_client().send_image_png(
                    to_user=target,
                    image_bytes=file_bytes,
                    context_token=context_token,
                )
            else:
                sent = self._build_client().send_file_bytes(
                    to_user=target,
                    file_bytes=file_bytes,
                    file_name=effective_name,
                    mime_type=mime_type,
                    context_token=context_token,
                )
            ok = ok or bool(sent)
        return ok

    def send_medias_msg(
        self, medias: List[MediaInfo], userid: Optional[str] = None
    ) -> Optional[bool]:
        """
        发送包含评分和详情地址的媒体候选列表。

        :param medias: 按原顺序展示的候选项；空列表不发送
        :param userid: 显式接收用户；为空时使用默认目标，再回退到近期活跃或全部已知用户
        :return: 至少一个目标的全部发送步骤成功时返回 True；无目标或全部发送失败返回 False
        """
        if not medias:
            return False
        lines = []
        for index, media in enumerate(medias, start=1):
            line = f"{index}. {media.title_year}"
            if media.vote_average:
                line += f" 评分：{media.vote_average}"
            if media.detail_link:
                line += f"\n{media.detail_link}"
            lines.append(line)
        return self.send_msg(title="媒体列表", text="\n\n".join(lines), userid=userid)

    def send_torrents_msg(
        self,
        torrents: List[Context],
        userid: Optional[str] = None,
        title: Optional[str] = None,
        link: Optional[str] = None,
    ) -> Optional[bool]:
        """
        发送带站点、资源信息和详情地址的种子候选列表。

        :param torrents: 按原顺序展示的种子上下文；空列表不发送
        :param userid: 显式接收用户；为空时使用默认目标，再回退到近期活跃或全部已知用户
        :param title: 列表标题；为空时使用“种子列表”
        :param link: 列表末尾的可选详情地址
        :return: 至少一个目标的全部发送步骤成功时返回 True；无目标或全部发送失败返回 False
        """
        if not torrents:
            return False
        lines = []
        for index, context in enumerate(torrents, start=1):
            torrent = context.torrent_info
            if torrent is None:
                continue
            meta = MetaInfo(title=torrent.title or "", subtitle=torrent.description)
            text = (
                f"{index}.【{torrent.site_name}】{meta.season_episode} {meta.resource_term} "
                f"{meta.video_term} {meta.release_group} {size_tools.format_compact_size(torrent.size)} "
                f"{torrent.volume_factor} {torrent.seeders}↑"
            )
            text = re.sub(r"\s+", " ", text).strip()
            if torrent.page_url:
                text += f"\n{torrent.page_url}"
            lines.append(text)
        return self.send_msg(
            title=title or "种子列表",
            text="\n\n".join(lines),
            userid=userid,
            link=link,
        )
