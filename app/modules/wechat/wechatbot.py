import hashlib
import json
import pickle
import re
import threading
import time
import uuid
import base64
from typing import Optional, List, Dict, Tuple, Set

import websocket
from Crypto.Cipher import AES

from app.core.cache import FileCache
from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class WeChatBot:
    """
    企业微信智能机器人（长连接模式）
    固定使用：
    - dmPolicy = open
    - groupPolicy = disabled
    """

    _default_ws_url = "wss://openws.work.weixin.qq.com"
    _ds_url = f"http://127.0.0.1:{settings.PORT}/api/v1/message?token={settings.API_TOKEN}"
    _heartbeat_interval = 30
    _ack_timeout = 10

    def __init__(self,
                 WECHAT_BOT_ID: Optional[str] = None,
                 WECHAT_BOT_SECRET: Optional[str] = None,
                 WECHAT_BOT_CHAT_ID: Optional[str] = None,
                 WECHAT_BOT_WS_URL: Optional[str] = None,
                 WECHAT_ADMINS: Optional[str] = None,
                 name: Optional[str] = None,
                 **kwargs):
        self._config_name = name or "wechat"
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
        self._send_lock = threading.Lock()
        self._acks_lock = threading.Lock()
        self._pending_acks: Dict[str, dict] = {}

        if not self._bot_id or not self._bot_secret:
            logger.error("企业微信智能机器人配置不完整！")
            return

        self._load_known_targets()
        self._ready = True
        self._start_gateway()

    @staticmethod
    def _build_req_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _split_content(content: str, max_bytes: int = 4000) -> List[str]:
        """
        将 markdown 内容拆分为较小分块，避免消息过长发送失败
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
        if self._ws_thread and self._ws_thread.is_alive():
            return

        self._stop_event.clear()
        self._ws_thread = threading.Thread(target=self._run_gateway, daemon=True)
        self._ws_thread.start()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        logger.info(f"企业微信智能机器人长连接已启动：{self._config_name}")

    def stop(self) -> None:
        self._stop_event.set()
        self._authenticated.clear()
        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception as err:
                logger.debug(f"关闭企业微信智能机器人连接失败：{err}")
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)

    def get_state(self) -> bool:
        return self._ready and self._authenticated.is_set()

    def _load_known_targets(self) -> None:
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
        try:
            self._filecache.set(self._cache_key, pickle.dumps(sorted(self._known_targets)))
        except Exception as err:
            logger.debug(f"保存企业微信智能机器人已互动用户失败：{err}")

    def _remember_target(self, userid: Optional[str]) -> None:
        target = str(userid).strip() if userid else None
        if not target:
            return
        if target not in self._known_targets:
            self._known_targets.add(target)
            self._save_known_targets()

    def _run_gateway(self) -> None:
        reconnect_delays = [1, 2, 5, 10, 30, 60]
        attempt = 0

        while not self._stop_event.is_set():
            self._authenticated.clear()
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

    def _on_open(self, ws) -> None:
        logger.info(f"企业微信智能机器人连接成功，开始订阅：{self._config_name}")
        self._send_raw({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": self._build_req_id("aibot_subscribe")},
            "body": {
                "bot_id": self._bot_id,
                "secret": self._bot_secret,
            },
        })

    def _on_message(self, ws, message: str) -> None:
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
            if str(req_id).startswith("aibot_subscribe"):
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

    def _on_error(self, ws, error) -> None:
        self._authenticated.clear()
        logger.error(f"企业微信智能机器人 WebSocket 错误：{error}")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._authenticated.clear()
        logger.info(f"企业微信智能机器人连接关闭：{close_status_code} {close_msg}")

    def _resolve_ack(self, req_id: str, payload: dict) -> None:
        with self._acks_lock:
            pending = self._pending_acks.get(req_id)
        if not pending:
            return
        pending["payload"] = payload
        pending["event"].set()

    def _send_raw(self, payload: dict) -> None:
        if not self._ws_app or not self._ws_app.sock or not self._ws_app.sock.connected:
            raise RuntimeError("企业微信智能机器人未连接")
        self._ws_app.send(json.dumps(payload, ensure_ascii=False))

    def _send_with_ack(self, payload: dict) -> bool:
        req_id = (payload.get("headers") or {}).get("req_id")
        if not req_id:
            return False

        if not self._authenticated.wait(timeout=self._ack_timeout):
            logger.error("企业微信智能机器人未完成认证，无法发送消息")
            return False

        pending = {"event": threading.Event(), "payload": None}
        with self._acks_lock:
            self._pending_acks[req_id] = pending

        try:
            with self._send_lock:
                self._send_raw(payload)
            if not pending["event"].wait(timeout=self._ack_timeout):
                logger.error(f"企业微信智能机器人消息发送超时：req_id={req_id}")
                return False
            ack = pending["payload"] or {}
            if ack.get("errcode") != 0:
                logger.error(
                    f"企业微信智能机器人消息发送失败：{ack.get('errmsg')} ({ack.get('errcode')})"
                )
                return False
            return True
        finally:
            with self._acks_lock:
                self._pending_acks.pop(req_id, None)

    def _handle_callback_event(self, payload: dict) -> None:
        event = ((payload.get("body") or {}).get("event") or {}).get("eventtype")
        if event == "disconnected_event":
            logger.info(f"企业微信智能机器人旧连接被踢下线：{self._config_name}")

    @staticmethod
    def _extract_text_from_body(body: dict) -> Optional[str]:
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
    def _build_image_ref(image_payload: dict) -> Optional[str]:
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
    def _extract_images_from_body(cls, body: dict) -> Optional[List[str]]:
        images: List[str] = []
        msgtype = body.get("msgtype")

        if msgtype == "image":
            image_ref = cls._build_image_ref(body.get("image") or {})
            if image_ref:
                images.append(image_ref)
        elif msgtype == "mixed":
            for item in (body.get("mixed") or {}).get("msg_item") or []:
                if item.get("msgtype") != "image":
                    continue
                image_ref = cls._build_image_ref(item.get("image") or {})
                if image_ref:
                    images.append(image_ref)

        quote = body.get("quote") or {}
        if not images and quote.get("msgtype") == "image":
            image_ref = cls._build_image_ref(quote.get("image") or {})
            if image_ref:
                images.append(image_ref)

        return images or None

    @staticmethod
    def _guess_mime_type(content: bytes, default: str = "image/jpeg") -> str:
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

    def _handle_callback_message(self, payload: dict) -> None:
        body = payload.get("body") or {}
        sender = ((body.get("from") or {}).get("userid") or "").strip()
        if not sender:
            return

        if body.get("chattype") == "group":
            logger.debug(f"企业微信智能机器人忽略群聊消息（groupPolicy=disabled）：{self._config_name}")
            return

        text = self._extract_text_from_body(body)
        images = self._extract_images_from_body(body)

        if text:
            text = re.sub(r"@\S+", "", text).strip()

        if not text and not images:
            return

        self._remember_target(sender)

        if text and text.startswith("/") and self._admins and sender not in self._admins:
            self.send_msg(title="只有管理员才有权限执行此命令", userid=sender)
            return

        logger.info(
            f"收到来自 {self._config_name} 的企业微信智能机器人消息："
            f"userid={sender}, text={text}, images={len(images) if images else 0}"
        )
        self._forward_to_message_chain(payload)

    def _forward_to_message_chain(self, payload: dict) -> None:
        def _run():
            try:
                # 回调
                RequestUtils(timeout=15).post_res(
                    f"http://127.0.0.1:{settings.PORT}/api/v1/message?token={settings.API_TOKEN}&source={self._config_name}",
                    json=payload
                )
            except Exception as err:
                logger.error(f"企业微信智能机器人转发消息失败：{err}")

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _normalize_target(userid: Optional[str], default_chat_id: Optional[str]) -> Tuple[Optional[str], int]:
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
        parts = []
        if title:
            parts.append(f"**{title}**")
        if text:
            parts.append(text.replace("\n\n", "\n"))
        if image:
            parts.append(f"![]({image})")
        if link:
            parts.append(f"[点击查看]({link})")
        return "\n\n".join(part for part in parts if part).strip()

    def _resolve_targets(self, userid: Optional[str] = None) -> List[Tuple[str, int]]:
        target, chat_type = self._normalize_target(userid=userid, default_chat_id=self._default_chat_id)
        if target:
            return [(target, chat_type)]
        return [(known_userid, 1) for known_userid in sorted(self._known_targets)]

    def _send_markdown(self, content: str, userid: Optional[str] = None) -> Optional[bool]:
        if not content:
            return False

        targets = self._resolve_targets(userid=userid)
        if not targets:
            logger.warning(f"{self._config_name} 未配置默认发送目标，且暂无已互动用户")
            return False

        send_success = False
        for target, chat_type in targets:
            target_success = True
            for chunk in self._split_content(content):
                req_id = self._build_req_id("aibot_send_msg")
                payload = {
                    "cmd": "aibot_send_msg",
                    "headers": {"req_id": req_id},
                    "body": {
                        "chatid": target,
                        "chat_type": chat_type,
                        "msgtype": "markdown",
                        "markdown": {
                            "content": chunk
                        }
                    }
                }
                if not self._send_with_ack(payload):
                    target_success = False
                    logger.warning(f"{self._config_name} 向目标 {target} 发送通知失败")
                    break
            send_success = send_success or target_success
        return send_success

    def send_msg(self, title: str, text: Optional[str] = None, image: Optional[str] = None,
                 userid: Optional[str] = None, link: Optional[str] = None) -> Optional[bool]:
        content = self._build_markdown(title=title, text=text, image=image, link=link)
        return self._send_markdown(content=content, userid=userid)

    def send_medias_msg(self, medias: List[MediaInfo], userid: Optional[str] = None) -> Optional[bool]:
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
        if not torrents:
            return False

        lines = [f"**{title or '种子列表'}**"]
        if link:
            lines.append(link)

        for index, context in enumerate(torrents, start=1):
            torrent = context.torrent_info
            meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
            torrent_title = (
                f"{index}.【{torrent.site_name}】"
                f"{meta.season_episode} "
                f"{meta.resource_term} "
                f"{meta.video_term} "
                f"{meta.release_group} "
                f"{StringUtils.str_filesize(torrent.size)} "
                f"{torrent.volume_factor} "
                f"{torrent.seeders}↑"
            )
            torrent_title = re.sub(r"\s+", " ", torrent_title).strip()
            if torrent.page_url:
                torrent_title += f"\n{torrent.page_url}"
            lines.append(torrent_title)

        return self._send_markdown(content="\n\n".join(lines), userid=userid)

    def create_menus(self, commands: Dict[str, dict]):
        """
        智能机器人模式不支持传统自建应用菜单
        """
        return

    def delete_menus(self):
        """
        智能机器人模式不支持传统自建应用菜单
        """
        return
