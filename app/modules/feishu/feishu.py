import asyncio
import json
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

import lark_oapi as lark
import lark_oapi.ws.client as lark_ws_client_module
from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    DeleteMessageReactionRequest,
    GetFileRequest,
    GetImageRequest,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    P2ImChatAccessEventBotP2pChatEnteredV1,
    P2ImMessageMessageReadV1,
    P2ImMessageReactionCreatedV1,
    P2ImMessageReactionDeletedV1,
    P2ImMessageRecalledV1,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    Emoji,
)
from lark_oapi.core.const import FEISHU_DOMAIN
from lark_oapi.core.enum import LogLevel
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from app.core.config import settings
from app.core.context import Context, MediaInfo
from app.db.user_oper import UserOper
from app.log import logger
from app.schemas import CommingMessage, Notification
from app.schemas.types import MessageChannel, NotificationType
from app.utils.http import RequestUtils


class Feishu:
    """飞书通知客户端，负责长连接收消息与主动发送通知。"""

    PROCESSING_REACTION_EMOJI = "GLANCE"
    STREAM_CARD_TITLE_ELEMENT_ID = "mp_stream_title"
    STREAM_CARD_BODY_ELEMENT_ID = "mp_stream_body"
    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".heic"}
    MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]\n]*)]\((?P<target>[^)\n]*)\)")

    def __init__(
            self,
            FEISHU_APP_ID: Optional[str] = None,
            FEISHU_APP_SECRET: Optional[str] = None,
            FEISHU_OPEN_ID: Optional[str] = None,
            FEISHU_CHAT_ID: Optional[str] = None,
            FEISHU_ADMINS: Optional[str] = None,
            FEISHU_VERIFICATION_TOKEN: Optional[str] = None,
            FEISHU_ENCRYPT_KEY: Optional[str] = None,
            name: Optional[str] = None,
            **kwargs,
    ):
        """初始化飞书客户端与长连接所需配置。"""
        self._name = name or "feishu"
        self._app_id = (FEISHU_APP_ID or "").strip()
        self._app_secret = (FEISHU_APP_SECRET or "").strip()
        self._default_open_id = (FEISHU_OPEN_ID or "").strip() or None
        self._default_chat_id = (FEISHU_CHAT_ID or "").strip() or None
        self._admins = [item.strip() for item in (FEISHU_ADMINS or "").split(",") if item.strip()]
        self._verification_token = (FEISHU_VERIFICATION_TOKEN or "").strip()
        self._encrypt_key = (FEISHU_ENCRYPT_KEY or "").strip()

        self._api_client: Optional[lark.Client] = None
        self._ws_client: Optional[lark.ws.Client] = None
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_tasks: Set[asyncio.Task] = set()
        self._user_chat_mapping: Dict[str, str] = {}
        self._user_receive_id_type_mapping: Dict[str, str] = {}
        self._chat_open_mapping: Dict[str, str] = {}

        if not self._app_id or not self._app_secret:
            logger.error("飞书配置不完整：缺少 App ID 或 App Secret")
            return

        self._api_client = self._build_api_client()
        self._start_ws_client()

    def _should_reject_admin_command(
            self, *user_ids: Optional[Union[str, int]]
    ) -> bool:
        """判断飞书命令或命令型按钮回调是否应因非管理员身份被拒绝。"""
        if not self._admins:
            return False
        candidates = [
            str(user_id).strip()
            for user_id in user_ids
            if user_id is not None and str(user_id).strip()
        ]
        return not any(candidate in self._admins for candidate in candidates)

    def _build_api_client(self) -> lark.Client:
        """构建飞书 OpenAPI client，用于发送和编辑消息。"""
        return (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(FEISHU_DOMAIN)
            .log_level(LogLevel.INFO)
            .build()
        )

    def _build_event_handler(self) -> lark.EventDispatcherHandler:
        """构建飞书事件分发器，将消息与卡片回调接到本地消息链。"""
        builder = lark.EventDispatcherHandler.builder(
            self._encrypt_key,
            self._verification_token,
            level=LogLevel.INFO,
        )
        builder.register_p2_im_message_receive_v1(self._on_message)
        builder.register_p2_im_message_message_read_v1(self._on_message_read)
        builder.register_p2_im_message_reaction_created_v1(self._on_message_reaction_created)
        builder.register_p2_im_message_reaction_deleted_v1(self._on_message_reaction_deleted)
        builder.register_p2_im_message_recalled_v1(self._on_message_recalled)
        builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._on_bot_p2p_chat_entered)
        builder.register_p2_card_action_trigger(self._on_card_action)
        return builder.build()

    def _start_ws_client(self) -> None:
        """启动飞书长连接客户端线程。"""
        if self._ws_thread and self._ws_thread.is_alive():
            return

        self._stop_event.clear()
        self._ws_thread = threading.Thread(target=self._run_ws_client, daemon=True)
        self._ws_thread.start()

    def _run_ws_client(self) -> None:
        """在后台线程中运行飞书长连接客户端。"""
        original_select = lark_ws_client_module._select
        original_loop = lark_ws_client_module.loop
        loop = asyncio.new_event_loop()
        original_create_task = loop.create_task
        self._ws_loop = loop
        asyncio.set_event_loop(loop)
        lark_ws_client_module.loop = loop

        async def _wait_for_stop() -> None:
            """等待停止信号，让 SDK 的阻塞 select 可被本地生命周期控制。"""
            while not self._stop_event.is_set():
                await asyncio.sleep(1)

        def _create_tracked_task(coro, *args, **kwargs) -> asyncio.Task:
            """跟踪 SDK 后台任务，避免关闭时产生未取回的任务异常。"""
            task = original_create_task(coro, *args, **kwargs)
            coro_name = getattr(coro, "__qualname__", "")
            if coro_name in {
                "Client._ping_loop",
                "Client._receive_message_loop",
                "Client._handle_message",
            }:
                self._ws_tasks.add(task)
                task.add_done_callback(self._consume_ws_task_result)
            return task

        lark_ws_client_module._select = _wait_for_stop
        loop.create_task = _create_tracked_task
        try:
            self._ws_client = lark.ws.Client(
                self._app_id,
                self._app_secret,
                log_level=LogLevel.INFO,
                event_handler=self._build_event_handler(),
                domain=FEISHU_DOMAIN,
                auto_reconnect=True,
            )
            self._ready.set()
            logger.info("飞书长连接服务启动：%s", self._name)
            self._ws_client.start()
        except Exception as err:
            self._ready.clear()
            if not self._stop_event.is_set():
                logger.error(f"飞书长连接服务启动失败：{err}")
        finally:
            if not loop.is_closed():
                loop.run_until_complete(self._shutdown_ws_client())
            lark_ws_client_module._select = original_select
            lark_ws_client_module.loop = original_loop
            loop.create_task = original_create_task
            pending_tasks = [
                task
                for task in asyncio.all_tasks(loop)
                if not task.done()
            ]
            for task in pending_tasks:
                task.cancel()
            if pending_tasks:
                loop.run_until_complete(
                    asyncio.gather(*pending_tasks, return_exceptions=True)
                )
            loop.close()
            asyncio.set_event_loop(None)
            self._ws_loop = None

    def _consume_ws_task_result(self, task: asyncio.Task) -> None:
        """取回飞书 SDK 后台任务结果，防止 asyncio 在关机时输出未消费异常。"""
        self._ws_tasks.discard(task)
        if task.cancelled():
            return
        try:
            err = task.exception()
        except asyncio.CancelledError:
            return
        if not err:
            return
        if self._stop_event.is_set():
            logger.debug(f"飞书长连接后台任务已随停止退出：{err}")
            return
        logger.error(f"飞书长连接后台任务异常：{err}")

    async def _shutdown_ws_client(self) -> None:
        """在飞书长连接线程内有序取消后台任务并关闭 WebSocket。"""
        ws_client = self._ws_client
        if ws_client:
            ws_client._auto_reconnect = False
        current_task = asyncio.current_task()
        running_tasks = [
            task
            for task in list(self._ws_tasks)
            if task is not current_task and not task.done()
        ]
        for task in running_tasks:
            task.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)
        if ws_client:
            try:
                await self._disconnect_ws_client_quietly(ws_client)
            except Exception as err:
                logger.debug(f"关闭飞书长连接失败：{err}")

    @staticmethod
    async def _disconnect_ws_client_quietly(ws_client: lark.ws.Client) -> None:
        """静默关闭飞书 WebSocket，避免 SDK 在关机时打印带敏感参数的连接地址。"""
        if ws_client._conn is None:
            ws_client._conn_url = ""
            ws_client._conn_id = ""
            ws_client._service_id = ""
            return
        await ws_client._lock.acquire()
        try:
            if ws_client._conn is not None:
                await ws_client._conn.close()
        finally:
            ws_client._conn = None
            ws_client._conn_url = ""
            ws_client._conn_id = ""
            ws_client._service_id = ""
            ws_client._lock.release()

    def _forward_to_message_chain(self, payload: dict) -> None:
        """将飞书入站消息转发到统一消息入口，复用现有交互主链。"""

        def _run() -> None:
            try:
                RequestUtils(timeout=15).post_res(
                    f"http://127.0.0.1:{settings.PORT}/api/v1/message?token={settings.API_TOKEN}&source={self._name}",
                    json=payload,
                )
            except Exception as err:
                logger.error(f"飞书转发消息失败：{err}")

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _parse_message_content(message) -> Tuple[
        str, Optional[List[CommingMessage.MessageImage]], Optional[List[str]], Optional[
            List[CommingMessage.MessageAttachment]]]:
        """从飞书事件消息体中提取文本、图片、音频和文件引用。"""
        raw_content = getattr(message, "content", None)
        if not raw_content:
            return "", None, None, None
        try:
            content = json.loads(raw_content)
        except Exception:
            return "", None, None, None
        if not isinstance(content, dict):
            return "", None, None, None

        message_type = getattr(message, "message_type", None)
        message_id = str(getattr(message, "message_id", None) or "").strip()
        text = content.get("text", "").strip() if isinstance(content.get("text"), str) else ""
        images = None
        audio_refs = None
        files = None

        if message_type == "image":
            image_key = str(content.get("image_key") or "").strip()
            if image_key:
                if message_id:
                    images = [CommingMessage.MessageImage(ref=f"feishu://image/{message_id}/{image_key}")]
                else:
                    images = [CommingMessage.MessageImage(ref=f"feishu://image/{image_key}")]
        elif message_type in {"audio", "media", "file"}:
            file_key = str(content.get("file_key") or "").strip()
            file_name = str(content.get("file_name") or "").strip() or None
            if file_key:
                if message_type == "audio":
                    resource_path = f"{message_id}/{file_key}" if message_id else file_key
                    audio_refs = [f"feishu://file/{resource_path}/{file_name or 'audio.opus'}"]
                else:
                    resource_path = f"{message_id}/{file_key}" if message_id else file_key
                    files = [
                        CommingMessage.MessageAttachment(
                            ref=f"feishu://file/{resource_path}/{file_name or 'attachment'}",
                            name=file_name,
                        )
                    ]
        elif message_type == "post" and not text:
            text, images = Feishu._parse_post_message_content(
                content=content,
                message_id=message_id,
            )

        return text, images, audio_refs, files

    @staticmethod
    def _resolve_post_message_body(content: dict) -> Optional[dict]:
        """解析飞书富文本消息在事件和 webhook 结构中的正文节点。"""
        if isinstance(content.get("content"), list):
            return content

        post = content.get("post")
        if isinstance(post, dict):
            preferred_locales = ("zh_cn", "en_us", "ja_jp")
            for locale in preferred_locales:
                locale_body = post.get(locale)
                if isinstance(locale_body, dict):
                    return locale_body
            for locale_body in post.values():
                if isinstance(locale_body, dict):
                    return locale_body

        return None

    @staticmethod
    def _parse_post_element_text(element: dict) -> str:
        """将飞书富文本元素转换为消息链可消费的纯文本片段。"""
        tag = str(element.get("tag") or "").strip()
        if tag in {"text", "plain_text"}:
            return str(element.get("text") or element.get("content") or "")
        if tag == "a":
            link_text = str(element.get("text") or "").strip()
            href = str(element.get("href") or element.get("url") or "").strip()
            if link_text and href and link_text != href:
                return f"{link_text} {href}"
            return link_text or href
        if tag == "at":
            user_name = str(element.get("user_name") or element.get("name") or "").strip()
            user_id = str(element.get("user_id") or "").strip()
            target = user_name or user_id
            return f" @{target}" if target else ""
        if tag in {"code_block", "pre"}:
            code = str(element.get("text") or element.get("content") or "").strip()
            language = str(element.get("language") or "").strip()
            if not code:
                return ""
            return f"```{language}\n{code}\n```" if language else f"```\n{code}\n```"
        return str(element.get("text") or element.get("content") or "")

    @staticmethod
    def _parse_post_message_content(
            content: dict,
            message_id: Optional[str] = None,
    ) -> Tuple[str, Optional[List[CommingMessage.MessageImage]]]:
        """从飞书富文本消息中提取可转发的文本和图片引用。"""
        post_body = Feishu._resolve_post_message_body(content)
        if not post_body:
            return "", None

        lines = []
        title = str(post_body.get("title") or "").strip()
        if title:
            lines.append(title)

        images = []
        post_content = post_body.get("content")
        if isinstance(post_content, list):
            for row in post_content:
                if not isinstance(row, list):
                    continue
                row_parts = []
                for element in row:
                    if not isinstance(element, dict):
                        continue
                    image_key = str(element.get("image_key") or "").strip()
                    if element.get("tag") == "img" and image_key:
                        if message_id:
                            images.append(CommingMessage.MessageImage(ref=f"feishu://image/{message_id}/{image_key}"))
                        else:
                            images.append(CommingMessage.MessageImage(ref=f"feishu://image/{image_key}"))
                    element_text = Feishu._parse_post_element_text(element)
                    if element_text:
                        row_parts.append(element_text)
                row_text = "".join(row_parts).strip()
                if row_text:
                    lines.append(row_text)

        text = "\n".join(lines).strip()
        return text, images or None

    def _remember_target(self, userid: Optional[str], chat_id: Optional[str]) -> None:
        """记录最近互动的用户与会话映射，便于后续主动回复。"""
        normalized_userid = (userid or "").strip()
        normalized_chat_id = (chat_id or "").strip()
        if not normalized_userid or not normalized_chat_id:
            return
        self._user_chat_mapping[normalized_userid] = normalized_chat_id
        self._chat_open_mapping[normalized_chat_id] = normalized_userid

    def _remember_user_id_type(
            self,
            open_id: Optional[str] = None,
            user_id: Optional[str] = None,
    ) -> None:
        """记住用户对应的飞书 ID 类型，避免回消息时误用 open_id/user_id。"""
        normalized_open_id = (open_id or "").strip()
        normalized_user_id = (user_id or "").strip()
        if normalized_open_id:
            self._user_receive_id_type_mapping[normalized_open_id] = "open_id"
        if normalized_user_id:
            self._user_receive_id_type_mapping[normalized_user_id] = "user_id"

    @staticmethod
    def _resolve_username(
            open_id: Optional[str],
            user_id: Optional[str],
            fallback: Optional[str],
    ) -> Optional[str]:
        """根据飞书绑定 ID 映射 MoviePilot 用户名，未绑定时保留渠道名称。"""
        binding_ids = {}
        if open_id:
            binding_ids["feishu_openid"] = open_id
        if user_id:
            binding_ids["feishu_userid"] = user_id
        if binding_ids:
            try:
                mapped_username = UserOper().get_name(**binding_ids)
                if mapped_username:
                    return mapped_username
            except Exception as err:
                logger.debug(f"解析飞书用户绑定失败：{err}")
        return fallback

    def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """处理飞书长连接收到的普通消息事件。"""
        event = getattr(data, "event", None)
        sender = getattr(event, "sender", None)
        message = getattr(event, "message", None)
        sender_id = getattr(sender, "sender_id", None)
        open_id = getattr(sender_id, "open_id", None)
        user_id = getattr(sender_id, "user_id", None)
        chat_id = getattr(message, "chat_id", None)
        text, images, audio_refs, files = self._parse_message_content(message)
        message_type = getattr(message, "message_type", None)

        payload = {
            "type": "message",
            "source": self._name,
            "message_id": getattr(message, "message_id", None),
            "chat_id": chat_id,
            "chat_type": getattr(message, "chat_type", None),
            "message_type": message_type,
            "text": text,
            "images": [image.model_dump() for image in images] if images else None,
            "audio_refs": audio_refs,
            "files": [file.model_dump() for file in files] if files else None,
            "sender": {
                "open_id": open_id,
                "user_id": user_id,
                "name": open_id or user_id,
            },
        }
        userid = open_id or user_id
        self._remember_user_id_type(open_id=open_id, user_id=user_id)
        self._remember_target(userid=userid, chat_id=chat_id)
        logger.info(
            "收到来自 %s 的飞书消息：userid=%s, chat_id=%s, type=%s, text=%s",
            self._name,
            userid,
            chat_id,
            message_type,
            text,
        )
        self._forward_to_message_chain(payload)

    def _on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """处理飞书卡片按钮回调，并同步回统一消息链。"""
        event = getattr(data, "event", None)
        operator = getattr(event, "operator", None)
        action = getattr(event, "action", None)
        context = getattr(event, "context", None)
        callback_data = self._extract_card_callback_data(
            value=getattr(action, "value", None),
            name=getattr(action, "name", None),
        )

        payload = {
            "type": "cardAction",
            "source": self._name,
            "message_id": getattr(context, "open_message_id", None),
            "chat_id": getattr(context, "open_chat_id", None),
            "callback_data": callback_data,
            "sender": {
                "open_id": getattr(operator, "open_id", None),
                "user_id": getattr(operator, "user_id", None),
                "name": getattr(operator, "open_id", None) or getattr(operator, "user_id", None),
            },
        }
        userid = payload["sender"].get("open_id") or payload["sender"].get("user_id")
        self._remember_user_id_type(
            open_id=payload["sender"].get("open_id"),
            user_id=payload["sender"].get("user_id"),
        )
        self._remember_target(userid=userid, chat_id=payload.get("chat_id"))
        logger.info(
            "收到来自 %s 的飞书按钮回调：userid=%s, callback_data=%s",
            self._name,
            userid,
            callback_data,
        )
        self._forward_to_message_chain(payload)

        return P2CardActionTriggerResponse(
            {
                "toast": {
                    "type": "info",
                    "content": "操作已提交",
                }
            }
        )

    @staticmethod
    def _on_message_read(data: P2ImMessageMessageReadV1) -> None:
        """忽略消息已读事件，避免长连接打印未注册处理器错误。"""
        event = getattr(data, "event", None)
        reader = getattr(event, "reader", None)
        logger.debug(
            "收到飞书消息已读事件：reader=%s, message_count=%s",
            getattr(reader, "open_id", None) or getattr(reader, "user_id", None),
            len(getattr(event, "message_id_list", None) or []),
        )

    @staticmethod
    def _on_message_reaction_created(data: P2ImMessageReactionCreatedV1) -> None:
        """忽略消息表情新增事件，避免长连接打印未注册处理器错误。"""
        event = getattr(data, "event", None)
        operator = getattr(event, "operator", None)
        reaction = getattr(event, "reaction", None)
        logger.debug(
            "收到飞书消息表情新增事件：message_id=%s, user=%s, emoji=%s",
            getattr(event, "message_id", None),
            getattr(operator, "open_id", None) or getattr(operator, "user_id", None),
            getattr(reaction, "emoji_type", None),
        )

    @staticmethod
    def _on_message_reaction_deleted(data: P2ImMessageReactionDeletedV1) -> None:
        """忽略消息表情删除事件，避免长连接打印未注册处理器错误。"""
        event = getattr(data, "event", None)
        operator = getattr(event, "operator", None)
        reaction = getattr(event, "reaction", None)
        logger.debug(
            "收到飞书消息表情删除事件：message_id=%s, user=%s, emoji=%s",
            getattr(event, "message_id", None),
            getattr(operator, "open_id", None) or getattr(operator, "user_id", None),
            getattr(reaction, "emoji_type", None),
        )

    @staticmethod
    def _on_message_recalled(data: P2ImMessageRecalledV1) -> None:
        """忽略消息撤回事件，避免长连接打印未注册处理器错误。"""
        event = getattr(data, "event", None)
        operator = getattr(event, "operator", None)
        logger.debug(
            "收到飞书消息撤回事件：message_id=%s, user=%s",
            getattr(event, "message_id", None),
            getattr(operator, "open_id", None) or getattr(operator, "user_id", None),
        )

    @staticmethod
    def _on_bot_p2p_chat_entered(data: P2ImChatAccessEventBotP2pChatEnteredV1) -> None:
        """忽略机器人进入单聊事件，避免长连接打印未注册处理器错误。"""
        event = getattr(data, "event", None)
        operator = getattr(event, "operator_id", None)
        logger.debug(
            "收到飞书机器人进入单聊事件：chat_id=%s, user=%s",
            getattr(event, "chat_id", None),
            getattr(operator, "open_id", None) or getattr(operator, "user_id", None),
        )

    def get_state(self) -> bool:
        """返回飞书客户端是否已就绪。"""
        return self._ready.is_set() and self._api_client is not None

    def stop(self) -> None:
        """停止飞书客户端并结束长连接线程。"""
        self._stop_event.set()
        self._ready.clear()
        ws_client = self._ws_client
        ws_loop = self._ws_loop
        if ws_client:
            try:
                ws_client._auto_reconnect = False
                if ws_loop and ws_loop.is_running():
                    shutdown_future = asyncio.run_coroutine_threadsafe(
                        self._shutdown_ws_client(),
                        ws_loop,
                    )
                    shutdown_future.result(timeout=5)
            except Exception as err:
                logger.debug(f"停止飞书客户端失败：{err}")
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)

    def parse_message(self, body: Any) -> Optional[CommingMessage]:
        """解析飞书转发到消息入口的 JSON 报文。"""
        try:
            message = json.loads(body) if isinstance(body, (str, bytes, bytearray)) else body
        except Exception as err:
            logger.debug(f"解析飞书消息失败：{err}")
            return None

        if not isinstance(message, dict):
            return None

        sender = message.get("sender") or {}
        open_id = sender.get("open_id")
        user_id = sender.get("user_id")
        username = self._resolve_username(
            open_id=open_id,
            user_id=user_id,
            fallback=sender.get("name") or open_id or user_id,
        )
        userid = open_id or user_id
        if not userid:
            return None

        if message.get("type") == "cardAction":
            callback_data = message.get("callback_data")
            if not callback_data:
                return None
            if str(callback_data).strip().startswith("/") and self._should_reject_admin_command(
                    open_id, user_id
            ):
                self.send_text(
                    "只有管理员才有权限执行此命令",
                    userid=str(userid),
                    chat_id=message.get("chat_id"),
                    receive_id_type="open_id" if open_id else "user_id",
                )
                return None
            return CommingMessage(
                channel=MessageChannel.Feishu,
                source=self._name,
                userid=userid,
                username=username,
                text=f"CALLBACK:{callback_data}",
                is_callback=True,
                callback_data=callback_data,
                message_id=message.get("message_id"),
                chat_id=message.get("chat_id"),
            )

        text = (message.get("text") or "").strip()
        images = CommingMessage.MessageImage.normalize_list(message.get("images"))
        audio_refs = None
        if isinstance(message.get("audio_refs"), list):
            audio_refs = [str(item).strip() for item in message.get("audio_refs") if str(item).strip()] or None
        files = None
        if isinstance(message.get("files"), list):
            normalized_files = []
            for item in message.get("files"):
                if isinstance(item, dict) and item.get("ref"):
                    normalized_files.append(CommingMessage.MessageAttachment(**item))
            files = normalized_files or None

        if not text and not images and not audio_refs and not files:
            return None

        if text.startswith("/") and self._should_reject_admin_command(open_id, user_id):
            self.send_text(
                "只有管理员才有权限执行此命令",
                userid=str(userid),
                chat_id=message.get("chat_id"),
                receive_id_type="open_id" if open_id else "user_id",
            )
            return None

        return CommingMessage(
            channel=MessageChannel.Feishu,
            source=self._name,
            userid=userid,
            username=username,
            text=text,
            message_id=message.get("message_id"),
            chat_id=message.get("chat_id"),
            images=images,
            audio_refs=audio_refs,
            files=files,
        )

    def _resolve_target(
            self,
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            receive_id_type: Optional[str] = None,
    ) -> Tuple[str, str]:
        """解析飞书发送目标，优先走显式用户，其次回退默认配置。"""
        resolved_userid = (userid or "").strip() or None
        resolved_chat_id = (chat_id or "").strip() or None
        normalized_receive_id_type = (receive_id_type or "").strip() or None
        if not resolved_userid and not resolved_chat_id:
            resolved_userid = self._default_open_id
            resolved_chat_id = self._default_chat_id
            if resolved_userid and not normalized_receive_id_type:
                normalized_receive_id_type = "open_id"
        if normalized_receive_id_type == "chat_id" and resolved_chat_id:
            return resolved_chat_id, "chat_id"
        if resolved_userid:
            if normalized_receive_id_type in {"open_id", "user_id"}:
                return resolved_userid, normalized_receive_id_type
            remembered_type = self._user_receive_id_type_mapping.get(resolved_userid)
            return resolved_userid, remembered_type or "open_id"
        if resolved_chat_id:
            return resolved_chat_id, "chat_id"
        raise ValueError("未找到可发送的飞书目标")

    @staticmethod
    def _escape_card_text(text: Optional[str]) -> str:
        """转义飞书卡片 markdown 中易误触的字符。"""
        if not text:
            return ""
        escaped = str(text)
        for source, target in {
            "\\": "&#92;",
            "<": "&#60;",
            ">": "&#62;",
        }.items():
            escaped = escaped.replace(source, target)
        return escaped

    @classmethod
    def _strip_streaming_markdown_images(cls, text: Optional[str]) -> str:
        """从流式卡片文本中剥离 Markdown 图片语法，图片由独立消息发送。"""
        if not text:
            return ""

        normalized_text = cls._strip_trailing_incomplete_markdown_image(str(text))
        parts = []
        last_end = 0
        for match in cls.MARKDOWN_IMAGE_PATTERN.finditer(normalized_text):
            parts.append(normalized_text[last_end:match.start()])
            alt_text = (match.group("alt") or "").strip()
            if alt_text:
                parts.append(alt_text)
            last_end = match.end()
        parts.append(normalized_text[last_end:])
        return "".join(parts)

    @classmethod
    def _strip_trailing_incomplete_markdown_image(cls, text: str) -> str:
        """隐藏末尾尚未闭合的 Markdown 图片片段，等流式累计完整后再处理。"""
        if not text:
            return ""

        start = text.rfind("![")
        if start < 0:
            return text
        fragment = text[start:]
        if "\n" in fragment or "\r" in fragment or cls.MARKDOWN_IMAGE_PATTERN.fullmatch(fragment):
            return text

        if ")" not in fragment:
            return text[:start].rstrip()
        return text

    @classmethod
    def _extract_markdown_image_urls(cls, text: Optional[str]) -> List[str]:
        """提取 Markdown 图片中的外部 URL，供 Agent 流式回复单独发送图片。"""
        if not text:
            return []
        urls = []
        for match in cls.MARKDOWN_IMAGE_PATTERN.finditer(str(text)):
            image_url = (match.group("target") or "").strip()
            if image_url and cls._is_external_image_url(image_url):
                urls.append(image_url)
        return urls

    @staticmethod
    def _is_external_image_url(image_url: str) -> bool:
        """判断图片地址是否可以按远程图片下载上传。"""
        normalized_url = (image_url or "").strip().lower()
        return normalized_url.startswith(("http://", "https://", "feishu://image/"))

    @classmethod
    def _is_supported_remote_image_response(
            cls,
            image_url: str,
            content_type: Optional[str] = None,
            content: Optional[bytes] = None,
    ) -> bool:
        """校验远程响应是否像图片，避免把普通网页链接上传到飞书图片接口。"""
        normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
        if normalized_type:
            return normalized_type.startswith("image/")
        path_suffix = Path(urlparse(image_url).path).suffix.lower()
        return path_suffix in cls.IMAGE_SUFFIXES and cls._looks_like_image_content(content)

    @staticmethod
    def _looks_like_image_content(content: Optional[bytes]) -> bool:
        """在响应缺少 Content-Type 时用文件头兜底判断是否为常见图片。"""
        if not content:
            return False
        head = bytes(content[:32])
        if head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")):
            return True
        if head.startswith((b"II*\x00", b"MM\x00*", b"\x00\x00\x01\x00")):
            return True
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return True
        if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in {
            b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif",
        }:
            return True
        return False

    @staticmethod
    def _dedupe_image_urls(image_urls: List[str]) -> List[str]:
        """按出现顺序去重图片 URL，避免 Agent 同一张图重复发送。"""
        deduped = []
        seen = set()
        for image_url in image_urls:
            normalized_url = (image_url or "").strip()
            if not normalized_url or normalized_url in seen:
                continue
            seen.add(normalized_url)
            deduped.append(normalized_url)
        return deduped

    @classmethod
    def _build_markdown_section(
            cls,
            text: Optional[str],
            text_size: str = "normal",
            margin: Optional[str] = None,
    ) -> Optional[dict]:
        content = cls._escape_card_text(text).strip()
        if not content:
            return None
        section = {
            "tag": "markdown",
            "text_size": text_size,
            "content": content,
        }
        if margin:
            # 图片卡片需要 body 零边距，文字留白转移到组件外边距上。
            section["margin"] = margin
        return section

    @staticmethod
    def _build_message_text(title: Optional[str], text: Optional[str], link: Optional[str] = None) -> str:
        """拼接飞书 Markdown 文本内容。"""
        parts = []
        if title:
            parts.append(f"**{Feishu._escape_card_text(title).strip()}**")
        if text:
            parts.append(Feishu._escape_card_text(text).strip())
        if link:
            parts.append(f"[查看详情]({link.strip()})")
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def _guess_image_suffix(image_url: str, content_type: Optional[str] = None) -> str:
        """根据 URL 或响应 Content-Type 推断临时图片后缀。"""
        content_type = (content_type or "").split(";", 1)[0].strip().lower()
        suffix_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/tiff": ".tiff",
            "image/heic": ".heic",
        }
        if content_type in suffix_map:
            return suffix_map[content_type]
        path_suffix = Path(urlparse(image_url).path).suffix.lower()
        if path_suffix in Feishu.IMAGE_SUFFIXES:
            return path_suffix
        return ".jpg"

    def _upload_remote_image(self, image_url: Optional[str]) -> Optional[str]:
        """下载远程图片并上传到飞书，返回可用于卡片的 image_key。"""
        image_url = (image_url or "").strip()
        if not image_url:
            return None
        if image_url.startswith("feishu://image/"):
            resource_path = image_url.replace("feishu://image/", "", 1)
            return resource_path.rsplit("/", 1)[-1].strip() or None

        response = None
        temp_path = None
        try:
            response = RequestUtils(timeout=30, ua=settings.USER_AGENT).get_res(image_url)
            if not response or not getattr(response, "content", None):
                logger.warning(f"飞书图片下载失败：{image_url}")
                return None
            content_type = response.headers.get("Content-Type") if response.headers else None
            if not self._is_supported_remote_image_response(image_url, content_type, response.content):
                logger.warning(f"飞书图片地址不是有效图片：{image_url}, content_type={content_type}")
                return None
            suffix = self._guess_image_suffix(image_url=image_url, content_type=content_type)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fp:
                fp.write(response.content)
                temp_path = Path(fp.name)
            return self._upload_image(temp_path)
        except Exception as err:
            logger.error(f"飞书远程图片上传失败：{err}")
            return None
        finally:
            if response is not None:
                response.close()
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception as err:
                    logger.debug(f"删除飞书临时图片失败：{err}")

    @staticmethod
    def _extract_card_callback_data(value: Any, name: Optional[str] = None) -> Optional[str]:
        """从新版/旧版飞书卡片回调中提取统一的 callback_data。"""
        callback_data = None
        if isinstance(value, dict):
            callback_data = value.get("callback_data") or value.get("value")
        elif isinstance(value, str):
            callback_data = value
        if not callback_data:
            callback_data = name
        return str(callback_data).strip() if callback_data else None

    @staticmethod
    def _card_actions(buttons: Optional[List[List[dict]]], margin: Optional[str] = None) -> List[dict]:
        """将统一按钮结构转换为飞书卡片按钮配置。"""
        if not buttons:
            return []
        card_rows = []
        for row in buttons[:8]:
            columns = []
            for button in row[:3]:
                text = (button or {}).get("text")
                if not text:
                    continue
                url = (button or {}).get("url")
                callback_data = (button or {}).get("callback_data")
                behaviors = []
                # 长连接模式不支持旧版消息卡片回传，必须使用新版 behaviors callback。
                if callback_data:
                    behaviors.append(
                        {
                            "type": "callback",
                            "value": {"callback_data": str(callback_data)},
                        }
                    )
                if url:
                    behaviors.append(
                        {
                            "type": "open_url",
                            "default_url": str(url),
                            "pc_url": str(url),
                            "android_url": str(url),
                            "ios_url": str(url),
                        }
                    )
                if not behaviors:
                    behaviors.append(
                        {
                            "type": "callback",
                            "value": {"callback_data": str(text)},
                        }
                    )
                element = {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": text[:20]},
                    "type": "default",
                    "behaviors": behaviors,
                }
                columns.append(
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [element],
                    }
                )
            if columns:
                row = {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "columns": columns,
                }
                if margin:
                    row["margin"] = margin
                card_rows.append(row)
        return card_rows

    def _build_card(
            self,
            title: Optional[str],
            text: Optional[str],
            link: Optional[str],
            buttons: Optional[List[List[dict]]],
            image_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """构建飞书交互卡片结构。"""
        elements: List[dict] = []
        if image_key:
            # 图文混合消息需要让图片贴近卡片顶部，避免先展示文字再露出海报。
            elements.append(
                {
                    "tag": "img",
                    "img_key": image_key,
                    "alt": {
                        "tag": "plain_text",
                        "content": title or "图片",
                    },
                    "mode": "fit_horizontal",
                }
            )
        text_margin = "12px 12px 0px 12px" if image_key else None
        body_margin = "4px 12px 12px 12px" if image_key else None
        action_margin = "0px 12px 12px 12px" if image_key else None
        title_section = self._build_markdown_section(title, text_size="heading", margin=text_margin)
        body_section = self._build_markdown_section(
            self._build_message_text(title=None, text=text, link=link),
            text_size="normal",
            margin=body_margin,
        )
        if title_section:
            elements.append(title_section)
        if body_section:
            elements.append(body_section)
        elements.extend(self._card_actions(buttons, margin=action_margin))
        return {
            # 飞书卡片消息要支持后续 PATCH 更新，发送和更新时都必须显式声明 update_multi。
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
                "update_multi": True,
                "summary": {
                    "content": title or "MoviePilot",
                },
            },
            "body": {
                "direction": "vertical",
                "padding": "0px 0px 0px 0px" if image_key else "12px 12px 12px 12px",
                "elements": elements,
            },
        }

    def _build_streaming_card_payload(
            self,
            title: Optional[str],
            text: Optional[str],
    ) -> Dict[str, Any]:
        """构建支持 CardKit 流式更新的飞书卡片 JSON 2.0。"""
        elements: List[dict] = []
        title_content = self._escape_card_text(title).strip() if title else ""
        body_content = self._escape_card_text(
            self._strip_streaming_markdown_images(text)
        ).strip()
        if title_content:
            elements.append(
                {
                    "tag": "markdown",
                    "element_id": self.STREAM_CARD_TITLE_ELEMENT_ID,
                    "content": f"**{title_content}**",
                }
            )
        elements.append(
            {
                "tag": "markdown",
                "element_id": self.STREAM_CARD_BODY_ELEMENT_ID,
                "content": body_content or " ",
            }
        )
        return {
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
                "update_multi": True,
                "streaming_mode": True,
                "summary": {
                    "content": title or "MoviePilot助手",
                },
                "streaming_config": {
                    "print_frequency_ms": {"default": 70},
                    "print_step": {"default": 1},
                    "print_strategy": "fast",
                },
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": elements,
            },
        }

    def _create_streaming_card(self, title: Optional[str], text: Optional[str]) -> Optional[str]:
        if not self._api_client:
            return None
        response = self._api_client.cardkit.v1.card.create(
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(self._build_streaming_card_payload(title=title, text=text), ensure_ascii=False))
                .build()
            )
            .build()
        )
        if response.success():
            data = getattr(response, "data", None)
            return getattr(data, "card_id", None)
        logger.warn(
            "飞书流式卡片创建失败：code=%s, msg=%s, log_id=%s",
            response.code,
            response.msg,
            response.get_log_id(),
        )
        return None

    def _send_streaming_card_message(
            self,
            title: Optional[str],
            text: Optional[str],
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            receive_id_type: Optional[str] = None,
            original_message_id: Optional[str] = None,
    ) -> Optional[dict]:
        card_id = self._create_streaming_card(title=title, text=text)
        if not card_id:
            return None
        if original_message_id:
            result = self._reply_message(
                message_id=original_message_id,
                msg_type="interactive",
                content={"type": "card", "data": {"card_id": card_id}},
            )
        else:
            receive_id, resolved_receive_id_type = self._resolve_target(
                userid=userid,
                chat_id=chat_id,
                receive_id_type=receive_id_type,
            )
            result = self._send_message(
                receive_id,
                resolved_receive_id_type,
                "interactive",
                {"type": "card", "data": {"card_id": card_id}},
            )
        if not result:
            return None
        result["metadata"] = {
            "feishu_streaming": {
                "card_id": card_id,
                "element_id": self.STREAM_CARD_BODY_ELEMENT_ID,
                # CardKit 的后续 PATCH/设置调用都依赖单调递增 sequence，
                # 首次建卡后尚未发生内容更新，因此从 0 开始记录。
                "sequence": 0,
            }
        }
        return result

    def _send_agent_streaming_images(
            self,
            image_urls: List[str],
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            receive_id_type: Optional[str] = None,
            sent_image_urls: Optional[List[str]] = None,
    ) -> List[str]:
        """将 Agent 流式回复中的图片作为独立图片卡片发送，避免污染流式文本组件。"""
        sent_images = list(sent_image_urls or [])
        pending_image_urls = [
            image_url
            for image_url in self._dedupe_image_urls(image_urls)
            if image_url not in sent_images
        ]
        for image_url in pending_image_urls:
            image_key = self._upload_remote_image(image_url)
            if not image_key:
                continue
            payload = self._build_card(
                title=None,
                text=None,
                link=None,
                buttons=None,
                image_key=image_key,
            )
            try:
                receive_id, resolved_receive_id_type = self._resolve_target(
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                )
                self._send_message(
                    receive_id,
                    resolved_receive_id_type,
                    "interactive",
                    payload,
                )
                sent_images.append(image_url)
            except Exception as err:
                logger.error(f"飞书 Agent 图片消息发送失败：{err}")
        return sent_images

    def _update_streaming_card_content(
            self,
            card_id: str,
            element_id: str,
            content: str,
            sequence: int,
    ) -> bool:
        if not self._api_client:
            return False
        response = self._api_client.cardkit.v1.card_element.content(
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(element_id)
            .request_body(
                ContentCardElementRequestBody.builder()
                .uuid(str(uuid.uuid4()))
                .content(content or " ")
                .sequence(sequence)
                .build()
            )
            .build()
        )
        if response.success():
            logger.debug("飞书流式卡片更新成功：card_id=%s, sequence=%s, content_len=%s", card_id, sequence, len(content))
            return True
        logger.warn(
            "飞书流式卡片内容更新失败：card_id=%s, element_id=%s, sequence=%s, code=%s, msg=%s, log_id=%s",
            card_id,
            element_id,
            sequence,
            response.code,
            response.msg,
            response.get_log_id(),
        )
        return False

    def close_streaming_card(self, card_id: str, sequence: int) -> bool:
        if not self._api_client or not card_id:
            return False
        response = self._api_client.cardkit.v1.card.settings(
            SettingsCardRequest.builder()
            .card_id(card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(json.dumps({"config": {"streaming_mode": False}}, ensure_ascii=False))
                .uuid(str(uuid.uuid4()))
                .sequence(sequence)
                .build()
            )
            .build()
        )
        if response.success():
            return True
        logger.warn(
            "飞书关闭流式卡片失败：card_id=%s, sequence=%s, code=%s, msg=%s, log_id=%s",
            card_id,
            sequence,
            response.code,
            response.msg,
            response.get_log_id(),
        )
        return False

    def _send_message(self, receive_id: str, receive_id_type: str, msg_type: str, content: dict) -> Optional[dict]:
        """调用飞书 IM API 发送消息，并返回统一结果结构。"""
        if not self._api_client:
            raise RuntimeError("飞书客户端未初始化")

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        response = self._api_client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                "飞书消息发送失败：code=%s, msg=%s, log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            return None

        data = getattr(response, "data", None)
        logger.info(
            "_send_message 飞书回复消息成功：message_id=%s",
            getattr(data, "message_id", None),
        )
        return {
            "success": True,
            "message_id": getattr(data, "message_id", None),
            "chat_id": getattr(data, "chat_id", None),
            "msg_type": getattr(data, "msg_type", None),
        }

    def _reply_message(
            self,
            message_id: str,
            msg_type: str,
            content: dict,
            reply_in_thread: bool = False,
    ) -> Optional[dict]:
        """按原消息回复，保持飞书会话中的引用关系。"""
        if not self._api_client:
            raise RuntimeError("飞书客户端未初始化")

        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps(content, ensure_ascii=False))
                .msg_type(msg_type)
                .reply_in_thread(reply_in_thread)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        response = self._api_client.im.v1.message.reply(request)
        if not response.success():
            logger.error(
                "飞书回复消息失败：code=%s, msg=%s, log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            return None

        data = getattr(response, "data", None)
        logger.info(
            "_reply_message 飞书回复消息成功：message_id=%s",
            getattr(data, "message_id", None),
        )
        return {
            "success": True,
            "message_id": getattr(data, "message_id", None),
            "chat_id": getattr(data, "chat_id", None),
            "msg_type": getattr(data, "msg_type", None),
            "root_id": getattr(data, "root_id", None),
            "parent_id": getattr(data, "parent_id", None),
            "thread_id": getattr(data, "thread_id", None),
        }

    @staticmethod
    def _guess_file_type(file_path: Path) -> str:
        suffix = file_path.suffix.lower().lstrip(".")
        if suffix == "opus":
            return "opus"
        if suffix == "mp4":
            return "mp4"
        if suffix in {"pdf", "doc", "xls", "ppt"}:
            return suffix
        return "stream"

    def _upload_image(self, file_path: Path) -> Optional[str]:
        if not self._api_client:
            return None
        with file_path.open("rb") as fp:
            response = self._api_client.im.v1.image.create(
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(fp)
                    .build()
                )
                .build()
            )
        if not response.success():
            logger.error(
                "飞书图片上传失败：code=%s, msg=%s, log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            return None
        data = getattr(response, "data", None)
        return getattr(data, "image_key", None)

    def _upload_file(self, file_path: Path, file_name: Optional[str] = None, duration: Optional[int] = None) -> \
    Optional[str]:
        if not self._api_client:
            return None
        with file_path.open("rb") as fp:
            builder = (
                CreateFileRequestBody.builder()
                .file_type(self._guess_file_type(file_path))
                .file_name(file_name or file_path.name)
                .file(fp)
            )
            if duration is not None:
                builder.duration(duration)
            response = self._api_client.im.v1.file.create(
                CreateFileRequest.builder().request_body(builder.build()).build()
            )
        if not response.success():
            logger.error(
                "飞书文件上传失败：code=%s, msg=%s, log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
            return None
        data = getattr(response, "data", None)
        return getattr(data, "file_key", None)

    def download_image_bytes(self, image_key: str) -> Optional[Tuple[bytes, Optional[str], Optional[str]]]:
        if not self._api_client or not image_key:
            return None
        response = self._api_client.im.v1.image.get(
            GetImageRequest.builder().image_key(image_key).build()
        )
        if getattr(response, "code", -1) != 0 or not getattr(response, "file", None):
            return None
        content_type = None
        if getattr(response, "raw", None) and getattr(response.raw, "headers", None):
            content_type = response.raw.headers.get("Content-Type")
        return response.file.read(), response.file_name, content_type

    def download_file_bytes(self, file_key: str) -> Optional[Tuple[bytes, Optional[str], Optional[str]]]:
        if not self._api_client or not file_key:
            return None
        response = self._api_client.im.v1.file.get(
            GetFileRequest.builder().file_key(file_key).build()
        )
        if getattr(response, "code", -1) != 0 or not getattr(response, "file", None):
            return None
        content_type = None
        if getattr(response, "raw", None) and getattr(response.raw, "headers", None):
            content_type = response.raw.headers.get("Content-Type")
        return response.file.read(), response.file_name, content_type

    def download_message_resource_bytes(self, message_id: str, file_key: str, resource_type: str) -> Optional[
        Tuple[bytes, Optional[str], Optional[str]]]:
        if not self._api_client or not message_id or not file_key:
            return None
        response = self._api_client.im.v1.message_resource.get(
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        if getattr(response, "code", -1) != 0 or not getattr(response, "file", None):
            return None
        content_type = None
        if getattr(response, "raw", None) and getattr(response.raw, "headers", None):
            content_type = response.raw.headers.get("Content-Type")
        return response.file.read(), response.file_name, content_type

    def send_text(
            self,
            text: str,
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            receive_id_type: Optional[str] = None,
            original_message_id: Optional[str] = None,
    ) -> Optional[dict]:
        """发送纯文本消息。"""
        try:
            if original_message_id:
                result = self._reply_message(
                    message_id=original_message_id,
                    msg_type="text",
                    content={"text": text},
                )
            else:
                receive_id, resolved_receive_id_type = self._resolve_target(
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                )
                result = self._send_message(
                    receive_id,
                    resolved_receive_id_type,
                    "text",
                    {"text": text},
                )
        except Exception as err:
            logger.error(f"飞书文本消息发送失败：{err}")
            return {"success": False}

        if not result:
            return {"success": False}
        result["chat_id"] = result.get("chat_id") or chat_id or self._user_chat_mapping.get(
            userid or "") or self._default_chat_id
        return result

    def send_file(
            self,
            file_path: str,
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            title: Optional[str] = None,
            text: Optional[str] = None,
            file_name: Optional[str] = None,
            receive_id_type: Optional[str] = None,
            original_message_id: Optional[str] = None,
    ) -> Optional[dict]:
        """发送本地图片或文件。"""
        local_file = Path(file_path)
        if not local_file.exists() or not local_file.is_file():
            logger.error(f"飞书附件不存在：{local_file}")
            return {"success": False}

        suffix = local_file.suffix.lower()
        is_image = suffix in self.IMAGE_SUFFIXES
        try:
            if is_image:
                image_key = self._upload_image(local_file)
                if not image_key:
                    return {"success": False}
                payload = self._build_card(
                    title=title,
                    text=text,
                    link=None,
                    buttons=None,
                    image_key=image_key,
                )
                if original_message_id:
                    result = self._reply_message(
                        message_id=original_message_id,
                        msg_type="interactive",
                        content=payload,
                    )
                else:
                    receive_id, resolved_receive_id_type = self._resolve_target(
                        userid=userid,
                        chat_id=chat_id,
                        receive_id_type=receive_id_type,
                    )
                    result = self._send_message(
                        receive_id,
                        resolved_receive_id_type,
                        "interactive",
                        payload,
                    )
            else:
                file_key = self._upload_file(local_file, file_name=file_name)
                if not file_key:
                    return {"success": False}
                if original_message_id:
                    result = self._reply_message(
                        message_id=original_message_id,
                        msg_type="file",
                        content={"file_key": file_key},
                    )
                else:
                    receive_id, resolved_receive_id_type = self._resolve_target(
                        userid=userid,
                        chat_id=chat_id,
                        receive_id_type=receive_id_type,
                    )
                    result = self._send_message(
                        receive_id,
                        resolved_receive_id_type,
                        "file",
                        {"file_key": file_key},
                    )
            if result and (title or text) and not is_image:
                self.send_text(
                    self._build_message_text(title=title, text=text),
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                    original_message_id=original_message_id,
                )
        except Exception as err:
            logger.error(f"飞书附件发送失败：{err}")
            return {"success": False}

        if not result:
            return {"success": False}
        result["chat_id"] = result.get("chat_id") or chat_id or self._user_chat_mapping.get(
            userid or "") or self._default_chat_id
        return result

    def send_voice(
            self,
            voice_path: str,
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            caption: Optional[str] = None,
            receive_id_type: Optional[str] = None,
            original_message_id: Optional[str] = None,
    ) -> Optional[dict]:
        """发送飞书语音消息。"""
        local_file = Path(voice_path)
        if not local_file.exists() or not local_file.is_file():
            logger.error(f"飞书语音文件不存在：{local_file}")
            return {"success": False}

        try:
            file_key = self._upload_file(local_file, file_name=local_file.name)
            if not file_key:
                return {"success": False}
            if original_message_id:
                result = self._reply_message(
                    message_id=original_message_id,
                    msg_type="audio",
                    content={"file_key": file_key},
                )
            else:
                receive_id, resolved_receive_id_type = self._resolve_target(
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                )
                result = self._send_message(
                    receive_id,
                    resolved_receive_id_type,
                    "audio",
                    {"file_key": file_key},
                )
            if result and caption:
                self.send_text(
                    caption,
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                    original_message_id=original_message_id,
                )
        except Exception as err:
            logger.error(f"飞书语音消息发送失败：{err}")
            return {"success": False}

        if not result:
            return {"success": False}
        result["chat_id"] = result.get("chat_id") or chat_id or self._user_chat_mapping.get(
            userid or "") or self._default_chat_id
        return result

    def send_notification(
            self,
            message: Notification,
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            receive_id_type: Optional[str] = None,
            original_message_id: Optional[str] = None,
    ) -> Optional[dict]:
        """发送通知消息，优先使用交互卡片承载按钮。"""
        is_streaming_agent_text = (
                message.mtype == NotificationType.Agent
                and not message.buttons
                and not message.link
        )
        if is_streaming_agent_text:
            try:
                stream_image_urls = []
                if self._is_external_image_url(message.image):
                    stream_image_urls.append(message.image)
                stream_image_urls.extend(self._extract_markdown_image_urls(message.text))
                result = self._send_streaming_card_message(
                    title=message.title,
                    text=message.text,
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                    original_message_id=original_message_id,
                )
            except Exception as err:
                logger.warn(f"飞书流式卡片发送失败：{err}")
                return {"success": False}
            if not result:
                return {"success": False}
            result["chat_id"] = result.get("chat_id") or chat_id or self._user_chat_mapping.get(
                userid or "") or self._default_chat_id
            sent_image_urls = self._send_agent_streaming_images(
                stream_image_urls,
                userid=userid,
                chat_id=result.get("chat_id") or chat_id,
                receive_id_type=receive_id_type,
            )
            stream_meta = result.get("metadata", {}).get("feishu_streaming")
            if isinstance(stream_meta, dict):
                stream_meta["sent_image_urls"] = sent_image_urls
            return result

        image_key = self._upload_remote_image(message.image)
        payload = self._build_card(
            title=message.title,
            text=message.text,
            link=message.link,
            buttons=message.buttons,
            image_key=image_key,
        )
        try:
            if original_message_id:
                result = self._reply_message(
                    message_id=original_message_id,
                    msg_type="interactive",
                    content=payload,
                )
            else:
                receive_id, resolved_receive_id_type = self._resolve_target(
                    userid=userid,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                )
                result = self._send_message(
                    receive_id,
                    resolved_receive_id_type,
                    "interactive",
                    payload,
                )
        except Exception as err:
            logger.error(f"飞书通知发送失败：{err}")
            return {"success": False}

        if not result:
            return {"success": False}
        result["chat_id"] = result.get("chat_id") or chat_id or self._user_chat_mapping.get(
            userid or "") or self._default_chat_id
        return result

    def edit_message(self, message_id: str, title: Optional[str] = None, text: Optional[str] = None,
                     buttons: Optional[List[List[dict]]] = None, metadata: Optional[dict] = None,
                     chat_id: Optional[str] = None) -> bool:
        """编辑已发送的飞书交互卡片消息。"""
        if not self._api_client:
            return False

        stream_meta = (metadata or {}).get("feishu_streaming") if isinstance(metadata, dict) else None
        if isinstance(stream_meta, dict) and not buttons:
            card_id = str(stream_meta.get("card_id") or "").strip()
            element_id = str(stream_meta.get("element_id") or self.STREAM_CARD_BODY_ELEMENT_ID).strip()
            sequence = int(stream_meta.get("sequence") or 0) + 1
            logger.debug("准备更新飞书流式卡片：card_id=%s, sequence=%s (before incr: %s)", card_id, sequence, stream_meta.get("sequence"))
            # 无论远端是否响应成功都自增 sequence，防止某次超时导致后续 sequence 一直因为没有递增而被拒绝
            stream_meta["sequence"] = sequence
            
            if card_id and element_id:
                content = self._escape_card_text(
                    self._strip_streaming_markdown_images(text)
                ).strip()
                if self._update_streaming_card_content(
                        card_id=card_id,
                        element_id=element_id,
                        content=content or " ",
                        sequence=sequence,
                ):
                    stream_image_urls = self._extract_markdown_image_urls(text)
                    stream_meta["sent_image_urls"] = self._send_agent_streaming_images(
                        stream_image_urls,
                        chat_id=chat_id,
                        sent_image_urls=stream_meta.get("sent_image_urls") or [],
                    )
                    return True
                logger.error("飞书流式更新失败被拦截，直接返回 False 以防止降级为普通卡片")
                return False

        card = self._build_card(title=title, text=text, link=None, buttons=buttons)
        try:
            response = self._api_client.im.v1.message.patch(
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(json.dumps(card, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            if response.success():
                return True
            logger.error(
                "飞书消息更新失败：code=%s, msg=%s, log_id=%s",
                response.code,
                response.msg,
                response.get_log_id(),
            )
        except Exception as err:
            logger.error(f"飞书消息更新失败：{err}")
        return False

    def add_message_reaction(
            self,
            message_id: str,
            emoji_type: str,
    ) -> Optional[str]:
        """为指定消息添加表情回应，并返回 reaction_id。"""
        if not self._api_client or not message_id or not emoji_type:
            return None

        try:
            response = self._api_client.im.v1.message_reaction.create(
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(
                        Emoji.builder().emoji_type(emoji_type).build()
                    )
                    .build()
                )
                .build()
            )
            if not response.success():
                logger.error(
                    "飞书消息表情添加失败：message_id=%s, emoji_type=%s, code=%s, msg=%s, log_id=%s",
                    message_id,
                    emoji_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return None
            data = getattr(response, "data", None)
            return getattr(data, "reaction_id", None)
        except Exception as err:
            logger.error(f"飞书消息表情添加失败：{err}")
            return None

    def delete_message_reaction(self, message_id: str, reaction_id: str) -> bool:
        """删除指定消息上的表情回应。"""
        if not self._api_client or not message_id or not reaction_id:
            return False

        try:
            response = self._api_client.im.v1.message_reaction.delete(
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            if response.success():
                return True
            logger.error(
                "飞书消息表情删除失败：message_id=%s, reaction_id=%s, code=%s, msg=%s, log_id=%s",
                message_id,
                reaction_id,
                response.code,
                response.msg,
                response.get_log_id(),
            )
        except Exception as err:
            logger.error(f"飞书消息表情删除失败：{err}")
        return False

    def send_medias_message(
            self,
            message: Notification,
            medias: List[MediaInfo],
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            receive_id_type: Optional[str] = None,
    ) -> Optional[dict]:
        """发送媒体列表消息，复用通知发送链路。"""
        lines = []
        for index, media in enumerate(medias[:10], start=1):
            title = getattr(media, "title_year", None) or getattr(media, "title", None) or "未知媒体"
            lines.append(f"{index}. {title}")
        proxy_message = Notification(
            title=message.title,
            text="\n".join(lines),
            link=message.link,
            buttons=message.buttons,
            userid=message.userid,
            targets=message.targets,
        )
        return self.send_notification(
            proxy_message,
            userid=userid or message.userid,
            chat_id=chat_id,
            receive_id_type=receive_id_type,
        )

    def send_torrents_message(
            self,
            message: Notification,
            torrents: List[Context],
            userid: Optional[str] = None,
            chat_id: Optional[str] = None,
            receive_id_type: Optional[str] = None,
    ) -> Optional[dict]:
        """发送种子列表消息，复用通知发送链路。"""
        lines = []
        for index, torrent in enumerate(torrents[:10], start=1):
            torrent_info = getattr(torrent, "torrent_info", None)
            title = getattr(torrent_info, "title", None) or getattr(torrent_info, "site_name", None) or "未知种子"
            lines.append(f"{index}. {title}")
        proxy_message = Notification(
            title=message.title,
            text="\n".join(lines),
            link=message.link,
            buttons=message.buttons,
            userid=message.userid,
            targets=message.targets,
        )
        return self.send_notification(
            proxy_message,
            userid=userid or message.userid,
            chat_id=chat_id,
            receive_id_type=receive_id_type,
        )
