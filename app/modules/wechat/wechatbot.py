"""
企业微信智能机器人长连接、消息确认、互动用户缓存和通知渲染。
"""

import base64
import hashlib
import json
import pickle
import re
import threading
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import websocket
from Crypto.Cipher import AES

from app.adapters.network.http import RequestUtils
from app.adapters.system.attachment import load_image
from app.application.messaging.channel.admin import matches_channel_admin
from app.application.messaging.image import build_wechat_image_url
from app.application.messaging.ingress import submit_message_to_host, submit_verified_message
from app.domain.admission import AdmissionProof
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.runtime.cache import FileCache
from app.runtime.log import logger
from app.runtime.thread import ThreadHelper
from app.schemas.channel import InteractionPolicy
from app.schemas.delivery import DeliveryPartOutcome, DeliveryPartPlanner, DeliveryStatus
from app.schemas.delivery import DeliveryPartSender as _PartSender
from app.schemas.delivery import DeliveryReceipt as _Receipt
from app.schemas.message import IncomingMessage
from app.schemas.types import NotificationChannel


class WeChatBot:
    """
    通过 WebSocket 长连接收发企业微信智能机器人消息。

    接收端只处理私聊，管理员配置约束命令执行；已互动用户持久化后可作为通知接收对象。
    """

    _default_ws_url = "wss://openws.work.weixin.qq.com"
    # 应用心跳间隔，单位为秒
    _heartbeat_interval = 30
    # 认证及单条消息确认各自的等待上限，单位为秒
    _ack_timeout = 10
    # 关闭时等待网关和心跳线程退出的独立预算，单位为秒
    _gateway_join_timeout_seconds = 5
    _heartbeat_join_timeout_seconds = 2

    def __init__(self,
                 WECHAT_BOT_ID: Optional[str] = None,
                 WECHAT_BOT_SECRET: Optional[str] = None,
                 WECHAT_BOT_CHAT_ID: Optional[str] = None,
                 WECHAT_BOT_WS_URL: Optional[str] = None,
                 WECHAT_ADMINS: Optional[str] = None,
                 name: Optional[str] = None,
                 interaction_policy: Optional[InteractionPolicy] = None,
                 **kwargs: Any) -> None:
        """
        加载互动用户并启动网关和心跳线程。

        :param WECHAT_BOT_ID: 机器人标识；与密钥缺失时保持未就绪状态
        :param WECHAT_BOT_SECRET: 机器人长连接密钥
        :param WECHAT_BOT_CHAT_ID: 默认通知目标，支持 user: 或 group: 前缀
        :param WECHAT_BOT_WS_URL: WebSocket 地址；为空时使用官方长连接地址
        :param WECHAT_ADMINS: 以逗号分隔的管理员用户标识
        :param name: 实例名称，用于日志和互动用户缓存隔离；为空时使用 wechat
        :param interaction_policy: 该已装配连接本代的交互策略
        :param kwargs: 通知实例附加配置，当前不使用
        """
        self._config_name = name or "wechat"
        self._interaction_policy = interaction_policy
        self._bot_id = WECHAT_BOT_ID
        self._bot_secret = WECHAT_BOT_SECRET
        self._default_chat_id = WECHAT_BOT_CHAT_ID.strip() if WECHAT_BOT_CHAT_ID else None
        self._ws_url = WECHAT_BOT_WS_URL or self._default_ws_url
        self._admins = [item.strip() for item in (WECHAT_ADMINS or "").split(",") if item.strip()]
        safe_name = hashlib.md5(self._config_name.encode()).hexdigest()[:12]
        self._cache_key = f"__wechatbot_known_targets_{safe_name}__"
        self._filecache = FileCache()
        self._known_targets: Set[str] = set()

        self._ready = False
        self._ws_app: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._authenticated = threading.Event()
        self._subscribe_req_id: str | None = None
        self._send_lock = threading.Lock()
        self._acks_lock = threading.Lock()
        self._pending_acks: Dict[str, Dict[str, Any]] = {}

        if not self._bot_id or not self._bot_secret:
            logger.error("企业微信智能机器人配置不完整！")
            return

        self._load_known_targets()
        self._ready = True
        self._start_gateway()

    @staticmethod
    def _build_req_id(prefix: str) -> str:
        """
        为微信协议请求生成带操作前缀的独立请求标识。
        """
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _split_content(content: str, max_bytes: int = 4000) -> List[str]:
        """
        按 UTF-8 字节数拆分 Markdown 并保持字符完整。

        :param content: 原始正文；分块首尾空白会被移除
        :param max_bytes: 每块字节上限，默认 4000；必须足以容纳文本中的单个字符
        :return: 非空文本分块；原正文为空时返回空列表
        """
        if not content:
            return []

        chunks = []
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
                    while end > start and end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
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

    def _start_gateway(self) -> None:
        """
        启动网关及心跳守护线程；网关仍运行时不重复启动。
        """
        if self._ws_thread and self._ws_thread.is_alive():
            return

        self._stop_event.clear()
        self._ws_thread = threading.Thread(target=self._run_gateway, daemon=True)
        self._ws_thread.start()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        logger.info(f"企业微信智能机器人长连接已启动：{self._config_name}")

    def stop(self) -> bool:
        """
        请求停止长连接和心跳，并在有限时间内等待线程退出。

        网关最多等待 5 秒，心跳最多等待 2 秒；不会等待当前调用线程自身。

        :return: 两个线程均已退出时返回 True，否则返回 False
        """
        self._stop_event.set()
        self._authenticated.clear()
        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception as err:
                logger.debug(f"关闭企业微信智能机器人连接失败：{err}")
        ws_thread = self._ws_thread
        heartbeat_thread = self._heartbeat_thread
        if (
            ws_thread
            and ws_thread.is_alive()
            and ws_thread is not threading.current_thread()
        ):
            ws_thread.join(timeout=self._gateway_join_timeout_seconds)
        if (
            heartbeat_thread
            and heartbeat_thread.is_alive()
            and heartbeat_thread is not threading.current_thread()
        ):
            heartbeat_thread.join(timeout=self._heartbeat_join_timeout_seconds)
        converged = not any(
            thread and thread.is_alive()
            for thread in (ws_thread, heartbeat_thread)
        )
        if not converged:
            logger.error("企业微信智能机器人线程未在关闭预算内退出")
        return converged

    def get_state(self) -> bool:
        """
        检查配置和长连接认证是否均已就绪。

        :return: 客户端已初始化且收到成功订阅确认时返回 True
        """
        return self._ready and self._authenticated.is_set()

    def _load_known_targets(self) -> None:
        """
        从实例文件缓存恢复已互动用户；损坏缓存只记录日志。
        """
        try:
            content = self._filecache.get(self._cache_key)
            if not content:
                return
            data = pickle.loads(content)
            if isinstance(data, (list, set, tuple)):
                self._known_targets = {str(item).strip() for item in data if str(item).strip()}
        except Exception as err:
            logger.debug(f"加载企业微信智能机器人已互动用户失败：{err}")

    def _save_known_targets(self) -> None:
        """
        按稳定顺序持久化已互动用户；缓存写入失败只记录日志。
        """
        try:
            self._filecache.set(self._cache_key, pickle.dumps(sorted(self._known_targets)))
        except Exception as err:
            logger.debug(f"保存企业微信智能机器人已互动用户失败：{err}")

    def _remember_target(self, userid: Optional[str]) -> None:
        """
        记录新的互动用户并立即写入实例缓存。

        :param userid: 发送者标识；空值或已记录用户不会触发写入
        """
        target = str(userid).strip() if userid else None
        if not target:
            return
        if target not in self._known_targets:
            self._known_targets.add(target)
            self._save_known_targets()

    def _run_gateway(self) -> None:
        """
        维护 WebSocket 连接，断开后逐步延长重连间隔。

        重连间隔依次为 1、2、5、10、30、60 秒，此后维持 60 秒，直到收到停止信号。
        """
        reconnect_delays = [1, 2, 5, 10, 30, 60]
        attempt = 0

        while not self._stop_event.is_set():
            self._authenticated.clear()
            self._subscribe_req_id = None
            try:
                self._ws_app = websocket.WebSocketApp(
                    self._ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws_app.run_forever(
                    ping_interval=None,
                    ping_timeout=None,
                    skip_utf8_validation=True,
                )
            except Exception as err:
                logger.error(f"企业微信智能机器人连接异常：{err}")

            if self._stop_event.is_set():
                break

            delay = reconnect_delays[min(attempt, len(reconnect_delays) - 1)]
            attempt += 1
            logger.info(f"企业微信智能机器人将在 {delay}s 后重连：{self._config_name}")
            for _ in range(delay * 10):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)

    def _heartbeat_loop(self) -> None:
        """
        认证后每 30 秒发送应用心跳，停止信号到达时退出。
        """
        while not self._stop_event.is_set():
            if self._authenticated.is_set():
                try:
                    self._send_raw({
                        "cmd": "ping",
                        "headers": {"req_id": self._build_req_id("ping")},
                    })
                except Exception as err:
                    logger.debug(f"发送企业微信智能机器人心跳失败：{err}")
            for _ in range(self._heartbeat_interval * 10):
                if self._stop_event.is_set():
                    return
                time.sleep(0.1)

    def _on_open(self, ws: Any) -> None:
        """
        连接建立后提交机器人订阅凭据。

        :param ws: 当前 WebSocket 连接
        """
        if ws is not self._ws_app or self._stop_event.is_set():
            return
        req_id = self._build_req_id("aibot_subscribe")
        self._subscribe_req_id = req_id
        logger.info(f"企业微信智能机器人连接成功，开始订阅：{self._config_name}")
        self._send_raw({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {
                "bot_id": self._bot_id,
                "secret": self._bot_secret,
            },
        })

    def _on_message(self, ws: Any, message: str) -> None:
        """
        解析确认、订阅结果和回调事件，并更新认证状态。

        :param ws: 当前 WebSocket 连接
        :param message: 服务器发送的 JSON 文本
        """
        if ws is not self._ws_app or self._stop_event.is_set():
            return
        try:
            payload = json.loads(message)
        except Exception as err:
            logger.error(f"解析企业微信智能机器人消息失败：{err}")
            return

        req_id = (payload.get("headers") or {}).get("req_id")
        if req_id:
            self._resolve_ack(req_id, payload)

        cmd = payload.get("cmd")
        if not cmd:
            if req_id and req_id == self._subscribe_req_id:
                self._subscribe_req_id = None
                if payload.get("errcode") == 0:
                    self._authenticated.set()
                    logger.info(f"企业微信智能机器人订阅成功：{self._config_name}")
                else:
                    logger.error(
                        f"企业微信智能机器人订阅失败：{payload.get('errmsg')} ({payload.get('errcode')})"
                    )
                    self._authenticated.clear()
            return

        if cmd == "aibot_msg_callback":
            self._handle_callback_message(payload)
        elif cmd == "aibot_event_callback":
            self._handle_callback_event(payload)

    def _on_error(self, ws: Any, error: Any) -> None:
        """
        清除认证状态并记录连接错误。

        :param ws: 产生错误的 WebSocket 连接
        :param error: 底层连接错误
        """
        if ws is self._ws_app:
            self._authenticated.clear()
            self._subscribe_req_id = None
        logger.error(f"企业微信智能机器人 WebSocket 错误：{error}")

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any) -> None:
        """
        连接关闭时清除认证状态并记录关闭原因。

        :param ws: 已关闭的 WebSocket 连接
        :param close_status_code: 服务器关闭状态码，可能为空
        :param close_msg: 服务器关闭说明，可能为空
        """
        if ws is self._ws_app:
            self._authenticated.clear()
            self._subscribe_req_id = None
        logger.info(f"企业微信智能机器人连接关闭：{close_status_code} {close_msg}")

    def _resolve_ack(self, req_id: str, payload: Dict[str, Any]) -> None:
        """
        将服务器确认交给等待同一请求标识的发送线程。

        :param req_id: 请求关联标识
        :param payload: 完整确认报文；无对应等待者时忽略
        """
        with self._acks_lock:
            pending = self._pending_acks.get(req_id)
        if not pending:
            return
        pending["payload"] = payload
        pending["event"].set()

    def _send_raw(self, payload: Dict[str, Any]) -> None:
        """
        向当前连接发送未经确认等待的 JSON 报文。

        连接不可用时抛出 RuntimeError；此方法本身不获取发送锁。

        :param payload: 原始协议报文
        """
        if not self._ws_app or not self._ws_app.sock or not self._ws_app.sock.connected:
            raise RuntimeError("企业微信智能机器人未连接")
        self._ws_app.send(json.dumps(payload, ensure_ascii=False))

    def _send_with_ack(self, payload: Dict[str, Any], *,
                       response: Optional[Dict[str, Any]] = None) -> tuple[DeliveryStatus, str | None]:
        """
        等待认证后串行写入报文，并等待对应服务器确认。

        认证和确认各最多等待 10 秒。写入后未收到确认时，平台是否接收不可知。

        :param payload: 必须在 headers.req_id 中包含请求标识的协议报文
        :param response: 可选的确认结果容器，仅成功时填入，供临时素材上传读取服务端标识
        :return: 投递结果和无敏感信息的固定错误类别
        """
        req_id = (payload.get("headers") or {}).get("req_id")
        if not req_id:
            return "permanent_failure", "missing_request_id"

        if not self._authenticated.wait(timeout=self._ack_timeout):
            logger.error("企业微信智能机器人未完成认证，无法发送消息")
            return "retryable_failure", "client_unavailable"

        pending: Dict[str, Any] = {"event": threading.Event(), "payload": None}
        with self._acks_lock:
            self._pending_acks[req_id] = pending

        try:
            with self._send_lock:
                self._send_raw(payload)
            event = pending["event"]
            if not isinstance(event, threading.Event) or not event.wait(timeout=self._ack_timeout):
                logger.error(f"企业微信智能机器人消息发送超时：req_id={req_id}")
                return "unknown", "ack_timeout"
            ack_value = pending.get("payload")
            ack: Dict[str, Any] = ack_value if isinstance(ack_value, dict) else {}
            if ack.get("errcode") != 0:
                logger.error(
                    f"企业微信智能机器人消息发送失败：{ack.get('errmsg')} ({ack.get('errcode')})"
                )
                return "retryable_failure", "platform_rejected"
            if response is not None:
                response.update(ack)
            return "confirmed", None
        finally:
            with self._acks_lock:
                self._pending_acks.pop(req_id, None)

    def _handle_callback_event(self, payload: Dict[str, Any]) -> None:
        """
        记录旧连接被服务器替换的事件；其他事件当前忽略。

        :param payload: 机器人事件回调报文
        """
        event = ((payload.get("body") or {}).get("event") or {}).get("eventtype")
        if event == "disconnected_event":
            self._authenticated.clear()
            self._subscribe_req_id = None
            logger.info(f"企业微信智能机器人旧连接被踢下线：{self._config_name}")

    @staticmethod
    def _extract_text_from_body(body: Dict[str, Any]) -> Optional[str]:
        """
        提取文本、语音转写或混合消息文字，无正文时回退到引用文字。

        :param body: 机器人回调 body 字典
        :return: 去除首尾空白并以换行拼接的正文；无有效文字返回 None
        """
        msgtype = body.get("msgtype")
        text_parts = []

        if msgtype == "text":
            text = ((body.get("text") or {}).get("content") or "").strip()
            if text:
                text_parts.append(text)
        elif msgtype == "voice":
            text = ((body.get("voice") or {}).get("content") or "").strip()
            if text:
                text_parts.append(text)
        elif msgtype == "mixed":
            for item in (body.get("mixed") or {}).get("msg_item") or []:
                if item.get("msgtype") == "text":
                    content = ((item.get("text") or {}).get("content") or "").strip()
                    if content:
                        text_parts.append(content)

        quote = body.get("quote") or {}
        if not text_parts and quote.get("msgtype") == "text":
            quote_text = ((quote.get("text") or {}).get("content") or "").strip()
            if quote_text:
                text_parts.append(quote_text)

        text = "\n".join(part for part in text_parts if part).strip()
        return text or None

    @staticmethod
    def _build_image_ref(image_payload: Dict[str, Any]) -> Optional[str]:
        """
        将图片下载地址、可选密钥和格式封装为内部引用。

        :param image_payload: 图片元数据，兼容多种下载地址及密钥字段名
        :return: wxbot://image/ 引用；输入无效或缺少下载地址返回 None
        """
        if not image_payload or not isinstance(image_payload, dict):
            return None
        download_url = (
            image_payload.get("download_url")
            or image_payload.get("url")
            or image_payload.get("cdnurl")
        )
        if not download_url:
            return None
        payload = {
            "url": download_url,
            "aeskey": image_payload.get("aeskey")
            or image_payload.get("encoding_aes_key")
            or image_payload.get("encrypt_key"),
            "mime_type": image_payload.get("mime_type")
            or image_payload.get("content_type"),
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"wxbot://image/{encoded}"

    @classmethod
    def _extract_images_from_body(
        cls, body: Dict[str, Any]
    ) -> Optional[List["IncomingMessage.MessageImage"]]:
        """
        提取图片或混合消息中的图片，无正文图片时回退到引用图片。

        :param body: 机器人回调 body 字典
        :return: 包含内部图片引用的列表；没有可下载图片时返回 None
        """
        images: List["IncomingMessage.MessageImage"] = []
        msgtype = body.get("msgtype")

        if msgtype == "image":
            image_payload = body.get("image") or {}
            image_ref = cls._build_image_ref(image_payload)
            if image_ref:
                images.append(
                    IncomingMessage.MessageImage(
                        ref=image_ref,
                        mime_type=image_payload.get("mime_type")
                        or image_payload.get("content_type"),
                    )
                )
        elif msgtype == "mixed":
            for item in (body.get("mixed") or {}).get("msg_item") or []:
                if item.get("msgtype") != "image":
                    continue
                image_payload = item.get("image") or {}
                image_ref = cls._build_image_ref(image_payload)
                if image_ref:
                    images.append(
                        IncomingMessage.MessageImage(
                            ref=image_ref,
                            mime_type=image_payload.get("mime_type")
                            or image_payload.get("content_type"),
                        )
                    )

        quote = body.get("quote") or {}
        if not images and quote.get("msgtype") == "image":
            image_payload = quote.get("image") or {}
            image_ref = cls._build_image_ref(image_payload)
            if image_ref:
                images.append(
                    IncomingMessage.MessageImage(
                        ref=image_ref,
                        mime_type=image_payload.get("mime_type")
                        or image_payload.get("content_type"),
                    )
                )

        return images or None

    @staticmethod
    def _guess_mime_type(content: bytes, default: str = "image/jpeg") -> str:
        """
        根据图片文件头识别常见图片格式。

        :param content: 图片字节
        :param default: 空内容或未知格式使用的 MIME 类型
        :return: 识别出的 MIME 类型或默认值
        """
        if not content:
            return default
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"BM"):
            return "image/bmp"
        if content.startswith(b"RIFF") and b"WEBP" in content[:16]:
            return "image/webp"
        return default

    def download_image_to_data_url(self, image_ref: str) -> Optional[str]:
        """
        下载内部图片引用，并按元数据中的可选密钥解密。

        :param image_ref: 包含下载地址和可选 AES 密钥的 wxbot://image/ 引用
        :return: Base64 图片数据地址；引用无效、下载或解密失败返回 None
        """
        if not image_ref or not image_ref.startswith("wxbot://image/"):
            return None
        encoded = image_ref.replace("wxbot://image/", "", 1)
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode(
                    "utf-8"
                )
            )
        except Exception as err:
            logger.error(f"解析企业微信智能机器人图片引用失败：{err}")
            return None

        download_url = payload.get("url")
        if not download_url:
            return None

        try:
            resp = RequestUtils(timeout=30).get_res(download_url)
        except Exception as err:
            logger.error(f"下载企业微信智能机器人图片失败：{err}")
            return None
        if not resp or not resp.content:
            return None

        content = resp.content
        aes_key = payload.get("aeskey")
        if aes_key:
            try:
                aes_bytes = base64.b64decode(aes_key + "=" * (-len(aes_key) % 4))
                cipher = AES.new(aes_bytes, AES.MODE_CBC, aes_bytes[:16])
                decrypted = cipher.decrypt(content)
                padding_len = decrypted[-1]
                if 0 < padding_len <= 32:
                    decrypted = decrypted[:-padding_len]
                content = decrypted
            except Exception as err:
                logger.error(f"解密企业微信智能机器人图片失败：{err}")
                return None

        mime_type = self._guess_mime_type(content, payload.get("mime_type") or "image/jpeg")
        return f"data:{mime_type};base64,{base64.b64encode(content).decode()}"

    def _receive_verified_text(self, body: Dict[str, Any], sender: str) -> bool:
        """
        将当前官方长连接收到的群聊或单聊文本交给宿主判定。
        """
        policy = getattr(self, "_interaction_policy", None)
        chat_type = body.get("chattype")
        chat_id = str(body.get("chatid") or "").strip() if chat_type == "group" else None
        if (
            not policy or not policy.enabled or not self._is_verified_text_callback(body)
            or chat_type not in {"group", "single"} or (chat_type == "group" and not chat_id)
        ):
            return False
        text = re.sub(r"@\S+", "", self._extract_text_from_body(body) or "").strip()
        event_id = body.get("msgid")
        if not text or not isinstance(event_id, str) or not event_id.strip():
            return False
        if chat_id is None and not (policy.admission_rules or policy.auto_download_rules):
            return False
        if chat_id is None:
            self._remember_target(sender)
        info = IncomingMessage(
            channel=NotificationChannel.Wechat, source=self._config_name, userid=sender,
            text=text, chat_id=f"group:{chat_id}" if chat_id else None,
            conversation_type="group" if chat_id else "private", message_id=event_id,
        )
        info._admission_proof = AdmissionProof(
            policy.connection_id, policy.revision, sender, chat_id, time.time() + 60, private=chat_id is None,
        )
        submit_verified_message(info, ThreadHelper().submit)
        return True

    def _is_verified_text_callback(self, body: Dict[str, Any]) -> bool:
        """
        确认事件来自当前官方已认证连接并发给本机器人。
        """
        return (
            self._authenticated.is_set() and not self._stop_event.is_set()
            and self._ws_url == self._default_ws_url and body.get("aibotid") == self._bot_id
            and body.get("msgtype") == "text" and "compat_info" not in body
        )

    def _handle_callback_message(self, payload: Dict[str, Any]) -> None:
        """
        接收私聊文本、图片、语音或文件，记录互动用户并检查管理员命令权限。

        可信群文本经过平台证明路径；私聊的兼容报文交给统一消息入口。

        :param payload: 完整机器人消息回调报文
        """
        body = payload.get("body") or {}
        sender = ((body.get("from") or {}).get("userid") or "").strip()
        if not sender:
            return

        if self._receive_verified_text(body, sender) or body.get("chattype") == "group":
            return

        text = self._extract_text_from_body(body)
        images = self._extract_images_from_body(body)

        if text:
            text = re.sub(r"@\S+", "", text).strip()

        has_media = body.get("msgtype") == "voice" or (
            body.get("msgtype") == "file" and bool((body.get("file") or {}).get("download_url"))
        )
        if not text and not images and not has_media:
            return

        self._remember_target(sender)

        is_channel_admin = matches_channel_admin(
            NotificationChannel.Wechat,
            {
                "WECHAT_ADMINS": ",".join(self._admins),
                "WECHAT_BOT_CHAT_ID": getattr(self, "_default_chat_id", None),
                "WECHAT_MODE": "bot",
            },
            sender,
            source=self._config_name,
        )
        if text and text.startswith("/") and self._admins and not is_channel_admin:
            self.send_msg(title="只有管理员才有权限执行此命令", userid=sender)
            return

        logger.info(
            f"收到来自 {self._config_name} 的企业微信智能机器人消息："
            f"userid={sender}, text={text}, images={len(images) if images else 0}"
        )
        self._forward_to_message_chain(payload)

    def _forward_to_message_chain(self, payload: Dict[str, Any]) -> bool:
        """
        通过受管线程池将原始回调提交给统一消息入口。

        :param payload: 保留来源字段的机器人回调报文
        :return: 入口任务成功提交时返回 True，否则返回 False
        """
        return submit_message_to_host(
            payload,
            self._config_name,
            submit=ThreadHelper().submit,
        )

    @staticmethod
    def _normalize_target(userid: Optional[str], default_chat_id: Optional[str]) -> Tuple[Optional[str], int]:
        """
        解析通知目标前缀，并回退到默认目标。

        :param userid: 显式目标，非空时优先使用
        :param default_chat_id: 未显式指定时使用的默认目标
        :return: 目标标识与会话类型的二元组；类型 1 为私聊、2 为群聊，无目标时标识为 None
        """
        target = str(userid).strip() if userid else (default_chat_id.strip() if default_chat_id else None)
        if not target:
            return None, 1

        lowered = target.lower()
        if lowered.startswith("group:"):
            return target[6:].strip(), 2
        if lowered.startswith("user:"):
            return target[5:].strip(), 1
        return target, 1

    @staticmethod
    def _build_markdown(title: Optional[str] = None,
                        text: Optional[str] = None,
                        image: Optional[str] = None,
                        link: Optional[str] = None) -> str:
        """
        将标题、正文、封面和跳转链接组合成 Markdown。

        :param title: 可选加粗标题
        :param text: 正文，连续两个换行会合并为一个
        :param image: 经企业微信图片地址适配后使用的封面地址
        :param link: 可选消息跳转地址
        :return: 合并后的正文；全部为空时返回空字符串
        """
        parts = []
        if title:
            parts.append(f"**{title}**")
        if text:
            parts.append(text.replace("\n\n", "\n"))
        if image:
            parts.append(f"![]({build_wechat_image_url(image)})")
        if link:
            parts.append(f"[点击查看]({link})")
        return "\n\n".join(part for part in parts if part).strip()

    def _resolve_targets(self, userid: Optional[str] = None, *, include_known_users: bool = True) -> List[Tuple[str, int]]:
        """
        按显式目标、默认目标、已互动用户的顺序确定接收对象。

        :param userid: 接收用户或 group: 群标识；为空时优先使用默认目标，否则发送给所有已互动用户
        :param include_known_users: 旧客户端直发时是否保留已互动用户回退
        :return: 目标标识和会话类型的列表；显式无效目标为空列表，不广播给已互动用户
        """
        target, chat_type = self._normalize_target(userid=userid, default_chat_id=self._default_chat_id)
        if target:
            return [(target, chat_type)]
        if userid or not include_known_users:
            return []
        return [(known_userid, 1) for known_userid in sorted(self._known_targets)]

    def _send_markdown(
        self, content: str, userid: Optional[str] = None, *, receipt: _Receipt | None = None,
        part_sender: _PartSender | None = None, plan: DeliveryPartPlanner | None = None,
    ) -> Optional[bool]:
        """
        向每个目标分块发送 Markdown，并等待服务器确认。

        一个目标的分块失败后停止该目标的后续分块，继续处理其他目标；不会撤回已发送内容。

        :param content: 待发送正文
        :param userid: 接收用户或 group: 群标识；为空时优先使用默认目标，否则发送给所有已互动用户
        :param receipt: 宿主投递入口的逐目标、逐分段回执
        :param part_sender: 宿主在提交前核对持久进度的执行边界
        :param plan: 在首次发送前登记当前连接的投递清单
        :return: 至少一个目标的全部分块发送成功时返回 True；内容为空、无目标或全部目标失败返回 False
        """
        if not content:
            if receipt:
                receipt(userid, 0, "skipped", "empty_content")
            return False

        targets = self._resolve_targets(userid=userid, include_known_users=receipt is None)
        if not targets:
            if receipt:
                receipt(userid, 0, "skipped", "no_target")
            logger.warning(f"{self._config_name} 未配置默认发送目标，且暂无已互动用户")
            return False

        send_success = False
        chunks = self._split_content(content)
        if plan is not None:
            plan(tuple(
                (self._part_address(target, chat_type), part, chunk)
                for target, chat_type in targets for part, chunk in enumerate(chunks)
            ))
        for target, chat_type in targets:
            target_success = self._send_markdown_target(target, chat_type, chunks, receipt=receipt, part_sender=part_sender)
            send_success = send_success or target_success
        return send_success

    @staticmethod
    def _part_address(target: str, chat_type: int) -> str:
        """
        为群和个人分别标记持久化的投递地址。
        """
        return f"{'group' if chat_type == 2 else 'user'}:{target}"

    def _send_markdown_target(
        self, target: str, chat_type: int, chunks: list[str], *, receipt: _Receipt | None, part_sender: _PartSender | None,
    ) -> bool:
        """
        按序提交同一会话的各段，失败后报告尚未提交的分段。
        """
        address = self._part_address(target, chat_type)
        previous_confirmed = True
        for part, chunk in enumerate(chunks):
            if part_sender:
                outcome, code = part_sender(
                    address, part, chunk, previous_confirmed,
                    partial(self._send_markdown_part, target, chat_type, chunk),
                )
            elif previous_confirmed:
                outcome, code = self._send_markdown_part(target, chat_type, chunk)
            else:
                outcome, code = "retryable_failure", "previous_part_unconfirmed"
            if receipt:
                receipt(address, part, outcome, code)
            previous_confirmed = previous_confirmed and outcome == "confirmed"
        return previous_confirmed

    def _send_markdown_part(self, target: str, chat_type: int, chunk: str) -> tuple[DeliveryStatus, str | None]:
        """
        向明确目标提交单段 Markdown，读写异常保留为未确认。
        """
        payload = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._build_req_id("aibot_send_msg")},
            "body": {
                "chatid": target, "chat_type": chat_type, "msgtype": "markdown",
                "markdown": {"content": chunk},
            },
        }
        try:
            return self._send_with_ack(payload)
        except (OSError, RuntimeError, websocket.WebSocketException):
            logger.warning("企业微信智能机器人发送未确认：%s", self._config_name)
            return "unknown", "transport_unconfirmed"

    def send_msg(self, title: Optional[str], text: Optional[str] = None, image: Optional[str] = None,
                 userid: Optional[str] = None, link: Optional[str] = None, *,
                 native_image: bool = False,
                 receipt: _Receipt | None = None, part_sender: _PartSender | None = None,
                 plan: DeliveryPartPlanner | None = None) -> Optional[bool]:
        """
        将通知组合成 Markdown 并发送给目标会话。

        :param title: 通知标题
        :param text: 可选正文
        :param image: 可选封面地址
        :param userid: 接收用户或 group: 群标识；为空时优先使用默认目标，否则发送给所有已互动用户
        :param link: 可选跳转链接
        :param native_image: 上传 image 并发送独立原图，支持 URL、Base64 和本地 JPG/PNG，忽略详情链接
        :param receipt: 宿主逐目标、逐分段收集回执的回调
        :param part_sender: 宿主在网络请求前后处理持久进度的回调
        :param plan: 在首次发送前登记当前连接的投递清单
        :return: 至少一个目标的全部分块发送成功时返回 True；内容为空、无目标或全部目标失败返回 False
        """
        if native_image and image:
            loaded = load_image(image)
            if loaded is None:
                logger.warning("企业微信机器人原图不可用")
                return False
            image_bytes, suffix = loaded
            return self._send_media(
                image_bytes, "image", f"image{suffix}", title, text, userid,
                receipt=receipt, part_sender=part_sender, plan=plan,
            )
        content = self._build_markdown(title=title, text=text, image=image, link=link)
        if receipt is None and part_sender is None and plan is None:
            return self._send_markdown(content=content, userid=userid)
        return self._send_markdown(content=content, userid=userid, receipt=receipt, part_sender=part_sender, plan=plan)

    def _media_command(self, command: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        发送素材协议命令并读取成功回执的业务字段，不新增线程或绕过认证。
        """
        response: Dict[str, Any] = {}
        status, _ = self._send_with_ack({
            "cmd": command, "headers": {"req_id": self._build_req_id(command)}, "body": body,
        }, response=response)
        if status != "confirmed":
            return None
        value = response.get("body")
        return value if isinstance(value, dict) else {}

    def _upload_media(self, content: bytes, media_type: str, filename: str) -> Optional[str]:
        """
        按官方长连接协议初始化、逐片确认并完成素材上传。

        每片最多 512 KiB，从 1 编号；失败片最多重试两次，未确认前不发送下一片。

        :param content: 已校验大小的完整素材
        :param media_type: image 或 file
        :param filename: 接收端文件名
        :return: 完成上传的 media_id；任一步失败或缺少必要回执字段返回 None
        """
        chunk_bytes = 512 * 1024
        total_chunks = (len(content) + chunk_bytes - 1) // chunk_bytes
        initialized = self._media_command("aibot_upload_media_init", {
            "type": media_type, "filename": filename, "total_size": len(content),
            "total_chunks": total_chunks, "md5": hashlib.md5(content).hexdigest(),
        })
        upload_id = (initialized or {}).get("upload_id")
        if not isinstance(upload_id, str) or not upload_id:
            return None
        for index in range(total_chunks):
            chunk = content[index * chunk_bytes:(index + 1) * chunk_bytes]
            body = {"upload_id": upload_id, "chunk_index": index + 1,
                    "base64_data": base64.b64encode(chunk).decode()}
            if not any(self._media_command("aibot_upload_media_chunk", body) is not None for _ in range(3)):
                return None
        completed = self._media_command("aibot_upload_media_finish", {"upload_id": upload_id})
        media_id = (completed or {}).get("media_id")
        return media_id if isinstance(media_id, str) and media_id else None

    def _send_media(
        self, content: bytes, media_type: str, filename: str, title: Optional[str],
        text: Optional[str], userid: Optional[str], *, receipt: _Receipt | None = None,
        part_sender: _PartSender | None = None, plan: DeliveryPartPlanner | None = None,
    ) -> bool:
        """
        说明及素材分别记账。新上传只发生在素材分段获准提交之后。
        """
        targets = self._resolve_targets(userid, include_known_users=receipt is None)
        if not targets:
            if receipt:
                receipt(userid, 0, "skipped", "no_target")
            return False
        caption = self._build_markdown(title=title, text=text)
        if part_sender is None:
            media_id = self._upload_media(content, media_type, filename)
            if not media_id:
                return False
            return self._send_unmanaged_media(targets, media_id, media_type, caption)
        chunks = self._split_content(caption) if caption else []
        signature = json.dumps((media_type, filename, hashlib.sha256(content).hexdigest()))
        if plan is not None:
            plan(tuple(
                (self._part_address(target, kind), index, value)
                for target, kind in targets for index, value in enumerate([*chunks, signature])
            ))
        confirmed = False
        for target, kind in targets:
            status = self._send_tracked_media_target(
                target, kind, chunks, signature, partial(self._upload_and_send_media, target, kind, content, media_type, filename),
                part_sender, receipt,
            )
            confirmed = confirmed or status
        return confirmed

    def _send_unmanaged_media(
        self, targets: list[tuple[str, int]], media_id: str, media_type: str, caption: str,
    ) -> bool:
        """
        旧客户端直发共用已确认上传的素材地址。
        """
        confirmed = False
        for target, chat_type in targets:
            if caption and not self._send_markdown(caption, self._part_address(target, chat_type)):
                continue
            status = self._send_media_part(target, chat_type, media_type, media_id)[0]
            confirmed = confirmed or status == "confirmed"
        return confirmed

    def _send_tracked_media_target(
        self, target: str, chat_type: int, chunks: list[str], signature: str,
        submit_media: Callable[[], DeliveryPartOutcome], part_sender: _PartSender, receipt: _Receipt | None,
    ) -> bool:
        """
        各分段共用原目标，已确认说明不会再发送。
        """
        address = self._part_address(target, chat_type)
        confirmed = True
        submits: list[Callable[[], DeliveryPartOutcome]] = [
            partial(self._send_markdown_part, target, chat_type, chunk) for chunk in chunks
        ]
        for index, (value, submit) in enumerate(zip([*chunks, signature], [*submits, submit_media])):
            status, code = part_sender(address, index, value, confirmed, submit)
            if receipt:
                receipt(address, index, status, code)
            confirmed = confirmed and status == "confirmed"
        return confirmed

    def _upload_and_send_media(
        self, target: str, chat_type: int, content: bytes, media_type: str, filename: str,
    ) -> DeliveryPartOutcome:
        """
        上传未完成时未产生对用户的投递，可报告重试。
        """
        media_id = self._upload_media(content, media_type, filename)
        if not media_id:
            return "retryable_failure", "attachment_upload_failed"
        return self._send_media_part(target, chat_type, media_type, media_id)

    def _send_media_part(self, target: str, chat_type: int, media_type: str, media_id: str) -> DeliveryPartOutcome:
        """
        写入后确认超时仍为未知态。
        """
        try:
            return self._send_with_ack({
                "cmd": "aibot_send_msg", "headers": {"req_id": self._build_req_id("aibot_send_msg")},
                "body": {"chatid": target, "chat_type": chat_type, "msgtype": media_type,
                         media_type: {"media_id": media_id}},
            })
        except (OSError, RuntimeError, websocket.WebSocketException):
            return "unknown", "transport_unconfirmed"

    def send_file(self, file_path: str, file_name: Optional[str] = None, title: Optional[str] = None,
                  text: Optional[str] = None, userid: Optional[str] = None, *,
                  receipt: _Receipt | None = None, part_sender: _PartSender | None = None,
                  plan: DeliveryPartPlanner | None = None) -> bool:
        """
        通过统一附件参数发送本地文件，限制为大于 5 字节且不超过 20 MiB，保留源文件。

        :param file_path: 插件提供的本地路径
        :param file_name: 可选展示名，默认使用源文件名
        :param title: 附件说明标题
        :param text: 附件说明正文
        :param userid: 沿用既有 user:、group: 或默认目标
        :return: 至少一个目标发送成功时返回 True
        """
        try:
            path = Path(file_path).expanduser()
            limit = 20 * 1024 * 1024
            with path.open("rb") as file:
                content = file.read(limit + 1)
            if not 5 < len(content) <= limit:
                logger.warning("企业微信机器人文件须大于 5 字节且不超过 20 MiB")
                return False
            return self._send_media(
                content, "file", Path(file_name or path.name).name, title, text, userid,
                receipt=receipt, part_sender=part_sender, plan=plan,
            )
        except Exception as error:
            logger.error(f"企业微信机器人文件发送失败：{type(error).__name__}")
            if receipt:
                receipt(userid, 0, "permanent_failure", "attachment_unavailable")
            return False

    def send_medias_msg(self, medias: List[MediaInfo], userid: Optional[str] = None) -> Optional[bool]:
        """
        将媒体候选顺序、评分和详情地址组合成 Markdown 列表。

        :param medias: 按原顺序展示的媒体候选项
        :param userid: 接收用户或 group: 群标识；为空时优先使用默认目标，否则发送给所有已互动用户
        :return: 至少一个目标的全部分块发送成功时返回 True；内容为空、无目标或全部目标失败返回 False
        """
        if not medias:
            return False

        lines = ["**媒体列表**"]
        for index, media in enumerate(medias, start=1):
            line = f"{index}. {media.title_year}"
            if media.vote_average:
                line += f" 评分：{media.vote_average}"
            if media.detail_link:
                line += f"\n{media.detail_link}"
            lines.append(line)
        return self._send_markdown(content="\n\n".join(lines), userid=userid)

    def send_torrents_msg(self, torrents: List[Context],
                          userid: Optional[str] = None, title: Optional[str] = None,
                          link: Optional[str] = None) -> Optional[bool]:
        """
        发送带站点、资源信息和详情地址的种子候选列表。

        :param torrents: 含种子信息的上下文列表
        :param userid: 接收用户或 group: 群标识；为空时优先使用默认目标，否则发送给所有已互动用户
        :param title: 列表标题；为空时使用“种子列表”
        :param link: 追加在标题后的可选链接
        :return: 至少一个目标的全部分块发送成功时返回 True；内容为空、无目标或全部目标失败返回 False
        """
        if not torrents:
            return False

        lines = [f"**{title or '种子列表'}**"]
        if link:
            lines.append(link)

        for index, context in enumerate(torrents, start=1):
            torrent = context.torrent_info
            if torrent is None:
                continue
            meta = MetaInfo(title=torrent.title or "", subtitle=torrent.description)
            torrent_title = (
                f"{index}.【{torrent.site_name}】"
                f"{meta.season_episode} "
                f"{meta.resource_term} "
                f"{meta.video_term} "
                f"{meta.release_group} "
                f"{size_tools.format_compact_size(torrent.size)} "
                f"{torrent.volume_factor} "
                f"{torrent.seeders}↑"
            )
            torrent_title = re.sub(r"\s+", " ", torrent_title).strip()
            if torrent.page_url:
                torrent_title += f"\n{torrent.page_url}"
            lines.append(torrent_title)

        return self._send_markdown(content="\n\n".join(lines), userid=userid)

    def create_menus(self, commands: Dict[str, Dict[str, Any]]) -> None:
        """
        保留客户端菜单接口；智能机器人模式不支持应用菜单。

        :param commands: 兼容应用客户端的命令映射，当前忽略
        """
        return

    def delete_menus(self) -> None:
        """
        保留客户端菜单清理接口；智能机器人模式不执行菜单操作。
        """
        return
