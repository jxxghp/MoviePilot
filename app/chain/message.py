import asyncio
import base64
import mimetypes
import re
import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union
from urllib.parse import unquote, urlparse

from app.application.agent import (
    get_running_agent_manager,
    is_audio_input_available,
    supports_image_input,
    transcribe_audio,
)
from app.application.messaging.agent import agent_interaction_manager, parse_agent_choice_callback
from app.application.messaging.interaction import InteractionContext, InteractionDispatch
from app.application.messaging.media import media_interaction_manager
from app.application.messaging.plugin import PluginInputInteractionHandler
from app.application.messaging.router import CallbackRoute, InteractionRouter, SessionRoute
from app.application.messaging.session import MessageSessionService
from app.application.messaging.site import site_interaction_manager
from app.application.messaging.skill import SkillInteractionHandler, skill_interaction_manager
from app.application.messaging.subscribe import subscribe_interaction_manager
from app.chain.base import ChainBase
from app.chain.interaction import MediaInteractionChain as _MediaInteractionChain
from app.chain.site import SiteChain
from app.chain.subscribe.facade import SubscribeChain
from app.chain.transfer.facade import TransferChain
from app.runtime.log import logger
from app.runtime.loop import main_loop_registry
from app.runtime.tasks import get_task_registry
from app.schemas.message import IncomingMessage, Message
from app.schemas.notification import ChannelCapabilityManager
from app.schemas.types import EventType, NotificationChannel


class MessageResponsePort(Protocol):
    """消息链读取附件所需的最小同步 HTTP 响应契约。"""

    content: bytes
    headers: Mapping[str, str]

    def close(self) -> None:
        """释放响应与连接资源。"""
        ...


class MessageHttpPort(Protocol):
    """消息链读取远程附件所需的同步 GET 端口。"""

    def get(self, url: str, *, timeout: int) -> Optional[MessageResponsePort]:
        """读取附件响应，并保留无响应与有响应两态。"""
        ...


_message_http_lock = threading.RLock()
_message_http_port: Optional[MessageHttpPort] = None


def configure_message_http_port(http: MessageHttpPort) -> Optional[MessageHttpPort]:
    """由启动组合根装配消息附件 HTTP 端口，并返回旧实现。"""
    global _message_http_port
    with _message_http_lock:
        previous = _message_http_port
        _message_http_port = http
        return previous


def reset_message_http_port(http: Optional[MessageHttpPort] = None) -> None:
    """恢复指定消息 HTTP 端口；省略参数时回到未装配状态。"""
    global _message_http_port
    with _message_http_lock:
        _message_http_port = http


def _message_http_snapshot() -> MessageHttpPort:
    """读取消息 HTTP 端口快照，未装配时稳定失败。"""
    with _message_http_lock:
        http = _message_http_port
    if http is None:
        raise RuntimeError("消息附件 HTTP 端口尚未由启动组合根装配")
    return http


def _read_message_http(
    url: str,
    *,
    timeout: int = 30,
) -> tuple[Optional[bytes], Mapping[str, str]]:
    """读取远程消息附件并在复制所需字段后立即释放响应。"""
    response = _message_http_snapshot().get(url, timeout=timeout)
    if response is None:
        return None, {}
    try:
        return response.content or None, dict(response.headers)
    finally:
        try:
            response.close()
        except Exception as err:
            logger.debug(f"释放消息附件响应失败：{str(err)}")


class MessageChain(ChainBase):
    """
    外来消息处理链
    """

    _ai_prefix = "/ai"
    _no_ai_prefix = "/noai"
    # 用户会话信息 {userid: (session_id, last_time)}
    _user_sessions: Dict[Union[str, int], tuple] = {}
    # 会话超时时间（分钟）
    _session_timeout_minutes: int = 24 * 60

    @staticmethod
    def _schedule_agent_session_clear(session_id: str, userid: Union[str, int]) -> None:
        """
        异步调度 Agent 会话清理，避免同步消息链阻塞在模型资源释放上。
        """
        if not session_id:
            return
        manager = get_running_agent_manager()
        if manager is None:
            return
        clear_task = None
        try:
            clear_task = manager.clear_session(
                session_id=session_id, user_id=str(userid)
            )
            get_task_registry().submit_threadsafe(
                clear_task,
                loop=main_loop_registry.require(),
                owner="chain.message.agent_session_clear",
            )
        except Exception as e:
            if clear_task:
                clear_task.close()
            logger.warning(f"调度清理智能体会话失败: {e}")

    def _cleanup_expired_user_sessions(self, current_time: datetime) -> None:
        """
        清理超过复用窗口的用户会话映射，并同步释放旧 Agent 实例。
        """
        self._message_session_service().cleanup(current_time)

    def _message_session_service(self) -> MessageSessionService:
        """用类级兼容映射构建可测试的用户会话服务。"""
        return MessageSessionService(
            sessions=self._user_sessions,
            timeout_minutes=self._session_timeout_minutes,
            expired_handler=self._schedule_agent_session_clear,
        )

    def _plugin_input_interaction_handler(self) -> PluginInputInteractionHandler:
        """构造使用当前 Chain 消息与事件端口的插件输入处理器。"""
        return PluginInputInteractionHandler(
            messenger=self,
            event_publisher=self.eventmanager,
        )

    @dataclass
    class _ProcessingStatus:
        channel: NotificationChannel
        source: str
        userid: Optional[Union[str, int]] = None
        message_id: Optional[Union[str, int]] = None
        chat_id: Optional[Union[str, int]] = None
        metadata: Optional[Dict[str, Any]] = None

        def to_dict(self) -> Dict[str, Any]:
            """转换为模块接口可安全传递的普通字典。"""
            return {
                "channel": self.channel.value,
                "source": self.source,
                "userid": self.userid,
                "message_id": self.message_id,
                "chat_id": self.chat_id,
                "metadata": self.metadata or {},
            }

    def process(self, body: Any, form: Any, args: Any) -> None:
        """
        调用模块识别消息内容
        """
        # 消息来源
        source = args.get("source")
        # 获取消息内容
        info = self.message_parser(source=source, body=body, form=form, args=args)
        if not info:
            logger.info("消息链路未识别到有效消息: source=%s", source)
            return
        # 更新消息来源
        source = info.source
        # 渠道
        channel = info.channel
        # 用户ID
        userid = info.userid
        # 用户名（当渠道未提供公开用户名时，回退为 userid 的字符串，避免后续类型校验异常）
        username = (
            str(info.username) if info.username not in (None, "") else str(userid)
        )
        if userid is None or userid == "":
            logger.debug(f"未识别到用户ID：{body}{form}{args}")
            return

        # 消息内容
        text = str(info.text).strip() if info.text else ""
        images = info.images
        audio_refs = info.audio_refs
        files = info.files
        # 结构化按钮回调数据，优先于 CALLBACK: 文本前缀
        callback_data = (
            str(info.callback_data).strip()
            if info.callback_data
            else None
        )
        if not text and not callback_data and not images and not audio_refs and not files:
            logger.debug(f"未识别到消息内容：：{body}{form}{args}")
            return

        original_message_id = info.message_id
        original_chat_id = info.chat_id
        reply_to_message_id = info.reply_to_message_id

        # 处理消息
        self.handle_message(
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            is_channel_admin=info.is_channel_admin,
            text=text,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            reply_to_message_id=reply_to_message_id,
            images=images,
            audio_refs=audio_refs,
            files=files,
            callback_data=callback_data,
        )

    def handle_message(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: Optional[str],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            images: Optional[List[IncomingMessage.MessageImage]] = None,
            audio_refs: Optional[List[str]] = None,
            files: Optional[List[IncomingMessage.MessageAttachment]] = None,
            reply_to_message_id: Optional[Union[str, int]] = None,
            is_channel_admin: Optional[bool] = None,
            callback_data: Optional[str] = None,
    ) -> None:
        """
        识别消息内容，执行操作
        """
        images = IncomingMessage.MessageImage.normalize_list(images)

        # 兼容归一化：结构化回调优先，CALLBACK: 文本前缀作为旧渠道和插件直接调用的兼容入口
        normalized_callback = str(callback_data or "").strip() or None
        if normalized_callback is None and str(text or "").startswith("CALLBACK:"):
            normalized_callback = str(text)[9:].strip() or None

        processing_status = None
        processing_finish_deferred = False
        try:
            # 语音输入只用于转写为文本，不默认改变回复形式。
            has_audio_input = bool(audio_refs)
            if audio_refs:
                transcript = self._transcribe_audio_refs(audio_refs, channel, source)
                merged_parts = []
                seen_parts = set()
                for item in [text.strip() if text else "", transcript or ""]:
                    normalized = item.strip()
                    if not normalized or normalized in seen_parts:
                        continue
                    seen_parts.add(normalized)
                    merged_parts.append(normalized)
                text = "\n".join(merged_parts).strip()
                if not text:
                    self.post_message(
                        Message(
                            channel=channel,
                            source=source,
                            userid=userid,
                            username=username,
                            title="语音识别失败，请稍后重试",
                            save_history=False,
                        )
                    )
                    return

            if self._handle_secret_confirmation_control(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    is_channel_admin=is_channel_admin,
                    text=text,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                    images=images,
                    audio_refs=audio_refs,
                    files=files,
                    has_audio_input=has_audio_input,
            ):
                return

            interaction_context = InteractionContext(
                channel=channel,
                source=source,
                user_id=userid,
                username=username,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                is_channel_admin=is_channel_admin,
            )

            if self._plugin_input_interaction_handler().handle_text(
                    context=interaction_context,
                    text=text,
                    reply_to_message_id=reply_to_message_id,
                    images=images,
                    audio_refs=audio_refs,
                    files=files,
                    has_audio_input=has_audio_input,
            ):
                return

            is_agent_message = self._is_agent_message(
                userid=userid,
                text=text,
                callback_data=normalized_callback,
                images=images,
                files=files,
                has_audio_input=has_audio_input,
            )

            # 回调消息不写入普通用户消息历史
            if normalized_callback is None and not is_agent_message:
                self._record_user_message(
                    channel=channel,
                    source=source,
                    userid=userid,
                    username=username,
                    text=text,
                )

            if not is_agent_message:
                processing_status = self._mark_message_processing_started(
                    channel=channel,
                    source=source,
                    userid=userid,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                    text=text,
                )

            processing_finish_deferred = self._handle_message_core(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                is_channel_admin=is_channel_admin,
                text=text,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                reply_to_message_id=reply_to_message_id,
                images=images,
                audio_refs=audio_refs,
                files=files,
                has_audio_input=has_audio_input,
                processing_status=processing_status,
                callback_data=normalized_callback,
            ) is True
        finally:
            if not processing_finish_deferred:
                self._mark_message_processing_finished(
                    channel=channel,
                    source=source,
                    userid=userid,
                    status=processing_status,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                )

    def _handle_secret_confirmation_control(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: Optional[str],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            images: Optional[List[IncomingMessage.MessageImage]] = None,
            audio_refs: Optional[List[str]] = None,
            files: Optional[List[IncomingMessage.MessageAttachment]] = None,
            has_audio_input: bool = False,
            is_channel_admin: Optional[bool] = None,
    ) -> bool:
        """将 TG/飞书中的确认控制文本交回所属 Agent 会话。"""
        if channel not in {NotificationChannel.Telegram, NotificationChannel.Feishu}:
            return False
        if str(text or "").strip() not in {"确认", "取消"}:
            return False
        if images or audio_refs or files or has_audio_input:
            return False

        session_info = self._user_sessions.get(userid)
        if not session_info:
            return False
        session_id, _ = session_info
        manager = get_running_agent_manager()
        if manager is None or not manager.matches_secret_confirmation(
            session_id,
            str(userid),
            channel=channel.value,
            source=source,
        ):
            return False
        return self._handle_ai_message(
            text=str(text).strip(),
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            is_channel_admin=is_channel_admin,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            images=images,
            files=files,
            session_id=session_id,
            has_audio_input=has_audio_input,
        )

    def _handle_message_core(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: Optional[str],
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            images: Optional[List[IncomingMessage.MessageImage]] = None,
            audio_refs: Optional[List[str]] = None,
            files: Optional[List[IncomingMessage.MessageAttachment]] = None,
            has_audio_input: bool = False,
            processing_status: Optional[_ProcessingStatus] = None,
            reply_to_message_id: Optional[Union[str, int]] = None,
            is_channel_admin: Optional[bool] = None,
            callback_data: Optional[str] = None,
    ) -> bool:
        """执行实际消息路由，便于统一包裹处理中状态。"""

        context = InteractionContext(
            channel=channel,
            source=source,
            user_id=userid,
            username=username,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            is_channel_admin=is_channel_admin,
        )

        if callback_data:
            if ChannelCapabilityManager.supports_callbacks(channel):
                return self._handle_callback(
                    callback_data=callback_data,
                    context=context,
                )
            else:
                logger.warning(
                    "渠道 %s 不支持回调，但收到了回调消息：%s",
                    channel.value,
                    callback_data,
                )
            return False

        if self._plugin_input_interaction_handler().handle_text(
                context=context,
                text=text,
                reply_to_message_id=reply_to_message_id,
                images=images,
                audio_refs=audio_refs,
                files=files,
                has_audio_input=has_audio_input,
        ):
            return False

        no_ai_requested, no_ai_text = self._strip_no_ai_prefix(text)
        if no_ai_requested:
            text = no_ai_text
            if not text:
                self.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="请输入要使用传统交互处理的内容",
                        save_history=False,
                    )
                )
                return False

        if text.startswith("/") and not self._has_ai_prefix(text):
            self.eventmanager.send_event(
                EventType.CommandExcute,
                {
                    "cmd": text,
                    "user": userid,
                    "channel": channel,
                    "source": source,
                    "processing_status": processing_status.to_dict()
                    if processing_status
                    else None,
                },
            )
            return bool(processing_status)

        if not no_ai_requested and self._has_ai_prefix(text):
            return self._handle_ai_message(
                text=text,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                is_channel_admin=is_channel_admin,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                images=images,
                files=files,
                has_audio_input=has_audio_input,
            )

        # 最近活动的传统交互会话（按创建时间选择，避免旧会话抢占新输入）
        if self._interaction_router().dispatch_active_text(context, text):
            return False

        if (
                not no_ai_requested
                and
                self.runtime_config.ai_agent_enable
                and (
                    self.runtime_config.ai_agent_global
                    or images
                    or files
                    or has_audio_input
                )
        ):
            return self._handle_ai_message(
                text=text,
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                is_channel_admin=is_channel_admin,
                original_message_id=original_message_id,
                original_chat_id=original_chat_id,
                images=images,
                files=files,
                has_audio_input=has_audio_input,
            )

        if _MediaInteractionChain().handle_text_interaction(
                channel=channel,
                source=source,
                userid=userid,
                username=username,
                text=text,
        ):
            return False

        self.eventmanager.send_event(
            EventType.UserMessage,
            {
                "text": text,
                "userid": userid,
                "channel": channel,
                "source": source,
                "chat_id": original_chat_id,
                "reply_to_message_id": reply_to_message_id,
            },
        )
        return False

    @classmethod
    def _strip_no_ai_prefix(cls, text: str) -> Tuple[bool, str]:
        """
        解析 /noai 前缀，显式要求本条消息绕过全局智能体。
        """
        normalized = (text or "").strip()
        pattern = rf"^{re.escape(cls._no_ai_prefix)}(?:\s+|[:：]\s*|$)(.*)$"
        match = re.match(pattern, normalized, re.IGNORECASE | re.DOTALL)
        if not match:
            return False, text
        return True, match.group(1).strip()

    @classmethod
    def _has_ai_prefix(cls, text: str) -> bool:
        """
        判断消息是否使用显式 AI 前缀。
        """
        return (text or "").lower().startswith(cls._ai_prefix)

    def _is_agent_message(
            self,
            userid: Union[str, int],
            text: str,
            callback_data: Optional[str] = None,
            images: Optional[List[IncomingMessage.MessageImage]] = None,
            files: Optional[List[IncomingMessage.MessageAttachment]] = None,
            has_audio_input: bool = False,
    ) -> bool:
        """
        判断本条消息是否会进入 Agent worker，由 Agent worker 管理 typing 生命周期。
        """
        if callback_data:
            return parse_agent_choice_callback(callback_data) is not None
        if self._has_ai_prefix(text):
            return True
        if text.startswith("/"):
            return False
        if not (
                self.runtime_config.ai_agent_enable
                and (
                    self.runtime_config.ai_agent_global
                    or images
                    or files
                    or has_audio_input
                )
        ):
            return False
        if self._interaction_router().has_pending(userid):
            return False
        return True

    def _mark_message_processing_started(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            original_message_id: Optional[Union[str, int]],
            original_chat_id: Optional[Union[str, int]],
            text: str,
    ) -> Optional[_ProcessingStatus]:
        """为支持的渠道标记“消息正在处理”。"""
        status = self.start_message_processing_status(
            channel=channel,
            source=source,
            userid=userid,
            message_id=original_message_id,
            chat_id=original_chat_id,
            text=text,
        )
        if not status:
            return None

        metadata = status.get("metadata")
        return self._ProcessingStatus(
            channel=channel,
            source=source,
            userid=status.get("userid", userid),
            message_id=status.get("message_id", original_message_id),
            chat_id=status.get("chat_id", original_chat_id),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def _mark_message_processing_finished(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            status: Optional[_ProcessingStatus] = None,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[Union[str, int]] = None,
    ) -> None:
        """
        结束渠道侧“消息正在处理”状态。
        不同渠道的表现可能是 reaction、typing 等，消息链只负责调用通用模块接口。
        """
        if not status:
            return
        self.finish_message_processing_status(
            status=status.to_dict(),
            channel=channel,
            source=source,
            userid=userid,
            message_id=status.message_id or original_message_id,
            chat_id=status.chat_id or original_chat_id,
        )

    def _interaction_router(self) -> InteractionRouter:
        """构造交互路由器，文本会话按创建时间选择，回调路由注册顺序即优先级。"""

        def session_text(handle):
            """包装传统交互入口为会话路由的文本处理函数，保持懒构造。"""
            def _handle(context: InteractionContext, text: str) -> bool:
                return bool(handle(
                    channel=context.channel,
                    source=context.source,
                    userid=context.user_id,
                    username=context.username,
                    text=text,
                ))
            return _handle

        def callback_dispatch(handle):
            """包装传统回调入口为回调路由的派发函数，保持懒构造。"""
            def _dispatch(callback_data: str, context: InteractionContext) -> InteractionDispatch:
                return InteractionDispatch(handled=bool(handle(
                    callback_data=callback_data,
                    channel=context.channel,
                    source=context.source,
                    userid=context.user_id,
                    username=context.username,
                    original_message_id=context.original_message_id,
                    original_chat_id=context.original_chat_id,
                )))
            return _dispatch

        session_routes = [
            SessionRoute(
                name="sites",
                get_pending=site_interaction_manager.get_by_user,
                handle_text=session_text(lambda **kw: SiteChain().handle_text_interaction(**kw)),
            ),
            SessionRoute(
                name="subscribes",
                get_pending=subscribe_interaction_manager.get_by_user,
                handle_text=session_text(lambda **kw: SubscribeChain().handle_text_interaction(**kw)),
            ),
            SessionRoute(
                name="skills",
                get_pending=skill_interaction_manager.get_by_user,
                handle_text=session_text(
                    lambda **kw: SkillInteractionHandler(messenger=self).handle_text_interaction(**kw)
                ),
            ),
            SessionRoute(
                name="media",
                get_pending=media_interaction_manager.get_by_user,
                handle_text=session_text(lambda **kw: _MediaInteractionChain().handle_text_interaction(**kw)),
            ),
        ]

        def _dispatch_agent_choice(callback_data: str, context: InteractionContext) -> InteractionDispatch:
            handled = self._handle_agent_choice_callback(
                callback_data=callback_data,
                context=context,
            )
            # Agent 选择回调会接续会话，需要延迟结束处理中状态
            return InteractionDispatch(handled=handled, defer_processing_finish=handled)

        def _dispatch_plugin_callback(callback_data: str, context: InteractionContext) -> InteractionDispatch:
            parsed = PluginInputInteractionHandler.parse_callback(callback_data)
            if not parsed:
                return InteractionDispatch(handled=False)
            plugin_id, content = parsed
            # 广播给插件处理
            self.eventmanager.send_event(
                EventType.MessageAction,
                {
                    "plugin_id": plugin_id,
                    "text": content,
                    "userid": context.user_id,
                    "channel": context.channel,
                    "source": context.source,
                    "original_message_id": context.original_message_id,
                    "original_chat_id": context.original_chat_id,
                },
            )
            return InteractionDispatch(handled=True)

        callback_routes = [
            CallbackRoute(
                name="transfer",
                matches=lambda data: TransferChain.parse_failed_transfer_callback(data) is not None,
                dispatch=lambda data, context: InteractionDispatch(
                    handled=TransferChain().handle_failed_transfer_callback(
                        callback_data=data,
                        channel=context.channel,
                        source=context.source,
                        userid=context.user_id,
                        username=context.username,
                    )
                ),
            ),
            CallbackRoute(
                name="skill",
                matches=lambda data: data.startswith("skills:"),
                dispatch=callback_dispatch(
                    lambda **kw: SkillInteractionHandler(messenger=self).handle_callback_interaction(**kw)
                ),
            ),
            CallbackRoute(
                name="site",
                matches=lambda data: data.startswith("sites:"),
                dispatch=callback_dispatch(lambda **kw: SiteChain().handle_callback_interaction(**kw)),
            ),
            CallbackRoute(
                name="subscribe",
                matches=lambda data: data.startswith("subscribes:"),
                dispatch=callback_dispatch(lambda **kw: SubscribeChain().handle_callback_interaction(**kw)),
            ),
            CallbackRoute(
                name="media",
                matches=lambda data: _MediaInteractionChain.parse_callback(data) is not None,
                dispatch=callback_dispatch(lambda **kw: _MediaInteractionChain().handle_callback_interaction(**kw)),
            ),
            CallbackRoute(
                name="agent_choice",
                matches=lambda data: parse_agent_choice_callback(data) is not None,
                dispatch=_dispatch_agent_choice,
            ),
            CallbackRoute(
                name="plugin",
                matches=lambda data: data.startswith("[PLUGIN]"),
                dispatch=_dispatch_plugin_callback,
            ),
        ]
        return InteractionRouter(session_routes=session_routes, callback_routes=callback_routes)

    def _handle_callback(
            self,
            callback_data: str,
            context: InteractionContext,
    ) -> bool:
        """
        处理按钮回调。

        :return: 是否延迟结束处理中状态（Agent 选择回调会等待会话接续）
        """
        logger.info(f"处理按钮回调：{callback_data}")
        result = self._interaction_router().dispatch_callback(context, callback_data)
        if result.handled:
            return result.defer_processing_finish

        logger.error(f"回调数据格式错误：{callback_data}")
        self.post_message(
            Message(
                channel=context.channel,
                source=context.source,
                userid=context.user_id,
                username=context.username,
                title="回调数据格式错误，请检查！",
                save_history=False,
            )
        )
        return False


    def _handle_agent_choice_callback(
            self,
            *,
            callback_data: str,
            context: InteractionContext,
    ) -> bool:
        """
        将 Agent 按钮选择回传为同一会话中的下一条用户消息。
        """
        callback = parse_agent_choice_callback(callback_data)
        if not callback:
            return False

        request_id, option_index = callback
        resolved = agent_interaction_manager.resolve(
            request_id=request_id,
            option_index=option_index,
            user_id=str(context.user_id),
        )
        if not resolved:
            self.post_message(
                Message(
                    channel=context.channel,
                    source=context.source,
                    userid=context.user_id,
                    username=context.username,
                    title="该选择已失效，请重新发起选择",
                    save_history=False,
                )
            )
            return False

        request, option = resolved
        selected_text = option.value
        self._update_interaction_message_feedback(
            channel=context.channel,
            source=context.source,
            original_message_id=context.original_message_id,
            original_chat_id=context.original_chat_id,
            title=request.title,
            prompt=request.prompt,
            selected_label=option.label,
        )
        self._bind_session_id(context.user_id, request.session_id)
        return self._handle_ai_message(
            text=selected_text,
            channel=context.channel,
            source=context.source,
            userid=context.user_id,
            username=context.username,
            is_channel_admin=context.is_channel_admin,
            session_id=request.session_id,
        )

    def _update_interaction_message_feedback(
            self,
            channel: NotificationChannel,
            source: str,
            original_message_id: Optional[Union[str, int]],
            original_chat_id: Optional[str],
            prompt: str,
            selected_label: str,
            title: Optional[str] = None,
    ) -> None:
        """
        在用户点击交互按钮后，立即更新原消息，明确显示已选择的内容。
        """
        if not original_message_id or not original_chat_id:
            return

        lines = [prompt.strip()]
        if selected_label:
            lines.append(f"已选择：{selected_label}")
        feedback_text = "\n\n".join(line for line in lines if line)
        self.edit_message(
            channel=channel,
            source=source,
            message_id=original_message_id,
            chat_id=original_chat_id,
            title=title,
            text=feedback_text,
        )

    def _get_or_create_session_id(self, userid: Union[str, int]) -> str:
        """
        获取或创建会话ID
        如果用户上次会话在15分钟内，则复用相同的会话ID；否则创建新的会话ID
        """
        resolution = self._message_session_service().resolve(userid)
        if resolution.reused:
            logger.info(
                f"复用会话ID: {resolution.session_id}, 用户: {userid}, "
                f"距离上次会话: {resolution.inactive_minutes:.1f}分钟"
            )
        else:
            logger.info(f"创建新会话ID: {resolution.session_id}, 用户: {userid}")
        return resolution.session_id

    def _bind_session_id(self, userid: Union[str, int], session_id: str) -> None:
        """
        将用户会话绑定到指定的 session_id，并刷新最后活动时间。
        """
        self._message_session_service().bind(userid, session_id)

    def bind_user_session(self, userid: Union[str, int], session_id: str) -> None:
        """
        绑定用户与指定智能体会话，供非传统入口复用远程命令状态查询。

        :param userid: 用户 ID
        :param session_id: 智能体会话 ID
        """
        self._bind_session_id(userid, session_id)

    def _record_user_message(
            self,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            text: str,
    ) -> None:
        """
        保存一条用户消息到消息历史与数据库。
        """
        self.messagehelper.put(
            IncomingMessage(
                userid=userid,
                username=username,
                channel=channel,
                source=source,
                text=text,
            ),
            role="user",
        )
        self.messageoper.add(
            channel=channel,
            source=source,
            userid=username or userid,
            text=text,
            action=0,
        )

    def clear_user_session(self, userid: Union[str, int]) -> bool:
        """
        清除指定用户的会话信息
        返回是否成功清除
        """
        session_id = self._message_session_service().clear(userid)
        if session_id:
            logger.info(f"已清除用户 {userid} 的会话: {session_id}")
            return True
        return False

    def remote_clear_session(
            self,
            channel: NotificationChannel,
            userid: Union[str, int],
            source: Optional[str] = None,
    ):
        """
        清除用户会话（远程命令接口）
        """
        # 获取并清除会话信息
        session_id = self._message_session_service().clear(userid)
        if session_id:
            logger.info(f"已清除用户 {userid} 的会话: {session_id}")

        # 如果有会话ID，同时清除智能体的会话记忆
        if session_id:
            self._schedule_agent_session_clear(session_id, userid)

            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    title="智能体会话已清除，下次将创建新的会话",
                    userid=userid,
                    save_history=False,
                )
            )
        else:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    title="您当前没有活跃的智能体会话",
                    userid=userid,
                    save_history=False,
                )
            )

    def remote_stop_agent(
            self,
            channel: NotificationChannel,
            userid: Union[str, int],
            source: Optional[str] = None,
    ):
        """
        应急停止当前正在执行的Agent推理（远程命令接口）。
        与 /clear_session 不同，此命令不会清除会话和记忆，
        停止后用户仍可继续对话。
        """
        # 查找用户的会话ID（不弹出，保留会话）
        session_info = self._message_session_service().get(userid)
        if session_info:
            session_id, _ = session_info
            manager = get_running_agent_manager()
            try:
                if manager is None:
                    stopped = False
                else:
                    future = asyncio.run_coroutine_threadsafe(
                        manager.stop_current_task(session_id=session_id),
                        main_loop_registry.require(),
                    )
                    stopped = future.result(timeout=10)
            except Exception as e:
                logger.warning(f"停止Agent推理失败: {e}")
                stopped = False

            if stopped:
                self.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        title="智能体推理已应急停止，会话记忆已保留，您可以继续对话",
                        userid=userid,
                        save_history=False,
                    )
                )
            else:
                self.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        title="当前没有正在执行的智能体任务",
                        userid=userid,
                        save_history=False,
                    )
                )
        else:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    title="您当前没有活跃的智能体会话",
                    userid=userid,
                    save_history=False,
                )
            )

    @staticmethod
    def _format_token_count(value: Optional[int]) -> str:
        return f"{value:,}" if value is not None else "未知"

    @classmethod
    def _format_session_status_text(cls, status: Dict[str, Any]) -> str:
        context_window_tokens = status.get("context_window_tokens")
        last_input_tokens = status.get("last_input_tokens")
        if context_window_tokens and status.get("model_call_count"):
            context_ratio = status.get("last_context_usage_ratio")
            if (
                context_ratio is None
                and status.get("last_input_usage_available") is True
                and last_input_tokens is not None
            ):
                context_ratio = last_input_tokens / context_window_tokens
            context_usage_text = (
                f"{cls._format_token_count(last_input_tokens)} / "
                f"{cls._format_token_count(context_window_tokens)} "
                f"({context_ratio * 100:.2f}%)"
                if context_ratio is not None
                else f"{cls._format_token_count(last_input_tokens)} / "
                     f"{cls._format_token_count(context_window_tokens)}"
            )
        else:
            context_usage_text = "暂无模型调用数据"

        lines = [
            f"会话ID: {status.get('session_id') or '未知'}",
            f"执行状态: {'运行中' if status.get('is_processing') else '空闲'}",
            f"当前模型: {status.get('model') or '未知'}",
            f"上下文窗口: {cls._format_token_count(context_window_tokens)} tokens",
            f"最近一次上下文占用: {context_usage_text}",
        ]
        if status.get("last_request_estimate_available"):
            estimated_tokens = status.get("last_estimated_input_tokens")
            estimated_ratio = status.get("last_estimated_input_ratio")
            estimate_text = (
                f"{cls._format_token_count(estimated_tokens)} / "
                f"{cls._format_token_count(context_window_tokens)}"
            )
            if estimated_ratio is not None:
                estimate_text += f" ({estimated_ratio * 100:.2f}%)"
            if status.get("last_estimated_over_input_limit"):
                estimate_text += "，估算已超输入上限"
            lines.extend(
                [
                    f"最终请求估算: {estimate_text}",
                "估算组成: "
                f"消息 {cls._format_token_count(status.get('last_estimated_message_tokens'))} / "
                f"系统 {cls._format_token_count(status.get('last_estimated_system_tokens'))} / "
                f"工具 {cls._format_token_count(status.get('last_estimated_tool_tokens'))} / "
                    f"其中图片固定成本 {cls._format_token_count(status.get('last_estimated_multimodal_tokens'))}",
                ]
            )
            actual_input_tokens = status.get("last_actual_input_tokens")
            estimate_error_tokens = status.get("last_estimate_error_tokens")
            if actual_input_tokens is not None and estimate_error_tokens is not None:
                estimate_error_ratio = status.get("last_estimate_error_ratio")
                error_text = (
                    f"实际 {cls._format_token_count(actual_input_tokens)} / "
                    f"误差 {estimate_error_tokens:+,}"
                )
                if estimate_error_ratio is not None:
                    error_text += f" ({estimate_error_ratio:+.2%})"
                lines.append(f"估算校准: {error_text}")
        lines.append(
            f"最近一次 tokens: 输入 {cls._format_token_count(status.get('last_input_tokens'))} / 输出 {cls._format_token_count(status.get('last_output_tokens'))} / 总计 {cls._format_token_count(status.get('last_total_tokens'))}"
        )
        if status.get("last_cache_usage_available"):
            last_cache_ratio = status.get("last_cache_hit_ratio")
            lines.append(
                "最近一次缓存: "
                f"命中 {cls._format_token_count(status.get('last_cache_read_input_tokens'))} / "
                f"写入 {cls._format_token_count(status.get('last_cache_write_input_tokens'))} / "
                f"未命中 {cls._format_token_count(status.get('last_uncached_input_tokens'))}"
                + (
                    f" ({last_cache_ratio * 100:.2f}%)"
                    if last_cache_ratio is not None
                    else ""
                ),
            )
        if status.get("cache_usage_available"):
            total_cache_ratio = status.get("total_cache_hit_ratio")
            lines.append(
                "当前会话累计缓存: "
                f"命中 {cls._format_token_count(status.get('total_cache_read_input_tokens'))} / "
                f"写入 {cls._format_token_count(status.get('total_cache_write_input_tokens'))} / "
                f"未命中 {cls._format_token_count(status.get('total_uncached_input_tokens'))}"
                + (
                    f" ({total_cache_ratio * 100:.2f}%)"
                    if total_cache_ratio is not None
                    else ""
                ),
            )
        pending_messages = status.get("pending_messages", 0)
        queue_capacity = status.get("queue_capacity")
        pending_text = (
            f"{pending_messages} / {queue_capacity}"
            if queue_capacity
            else str(pending_messages)
        )
        lines.extend(
            [
                f"当前会话累计 tokens: 输入 {cls._format_token_count(status.get('total_input_tokens'))} / 输出 {cls._format_token_count(status.get('total_output_tokens'))} / 总计 {cls._format_token_count(status.get('total_tokens'))}",
                f"模型调用次数: {status.get('model_call_count', 0)}",
                f"排队消息数: {pending_text}",
                f"最后更新: {status.get('last_updated_at') or '暂无'}",
            ]
        )
        if status.get("queue_rejections"):
            lines.append(f"排队拒绝次数: {status['queue_rejections']}")
        if status.get("shutdown_pending"):
            lines.append("会话状态: 正在停止")
        return "\n".join(lines)

    def remote_session_status(
            self,
            channel: NotificationChannel,
            userid: Union[str, int],
            source: Optional[str] = None,
    ):
        """查询当前用户的智能体会话状态。"""
        session_info = self._message_session_service().get(userid)
        if not session_info:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    title="您当前没有活跃的智能体会话",
                    userid=userid,
                    save_history=False,
                )
            )
            return

        session_id, _ = session_info
        manager = get_running_agent_manager()
        if manager is None:
            self.post_message(
                Message(
                    channel=channel,
                    source=source,
                    title="您当前没有活跃的智能体会话",
                    userid=userid,
                    save_history=False,
                )
            )
            return
        status = manager.get_session_status(session_id=session_id)
        self.post_message(
            Message(
                channel=channel,
                source=source,
                title="当前智能体会话状态",
                text=self._format_session_status_text(status),
                userid=userid,
                save_history=False,
            )
        )

    def _handle_ai_message(
            self,
            text: str,
            channel: NotificationChannel,
            source: str,
            userid: Union[str, int],
            username: str,
            original_message_id: Optional[Union[str, int]] = None,
            original_chat_id: Optional[str] = None,
            images: Optional[List[IncomingMessage.MessageImage]] = None,
            files: Optional[List[IncomingMessage.MessageAttachment]] = None,
            session_id: Optional[str] = None,
            has_audio_input: bool = False,
            is_channel_admin: Optional[bool] = None,
    ) -> bool:
        """
        处理AI智能体消息
        """
        try:
            # 检查AI智能体是否启用
            if not self.runtime_config.ai_agent_enable:
                self.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="MoviePilot智能助手未启用，请在系统设置中启用",
                        save_history=False,
                    )
                )
                return False

            manager = get_running_agent_manager()
            if manager is None:
                self.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="MoviePilot智能助手服务尚未就绪，请稍后重试",
                        save_history=False,
                    )
                )
                return False

            images = IncomingMessage.MessageImage.normalize_list(images)

            # 提取用户消息
            if self._has_ai_prefix(text):
                # 前缀匹配不区分大小写，但保留原始正文避免改变用户输入内容。
                user_message = text[len(self._ai_prefix):].strip()
            else:
                user_message = text.strip()  # 按原消息处理

            if not user_message and not images and not files:
                self.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="请输入您的问题或需求",
                        save_history=False,
                    )
                )
                return False

            # 生成或复用会话ID
            session_id = session_id or self._get_or_create_session_id(userid)
            self._bind_session_id(userid, session_id)

            # 将可直接输入给 LLM 的附件统一转换为 data URL
            original_images = images
            all_files = list(files or [])
            if images and supports_image_input(
                    provider=self.runtime_config.llm_provider,
                    model=self.runtime_config.llm_model,
            ):
                images = self._download_attachments_to_data_urls(
                    images, channel, source
                )
                if original_images and not images and not user_message and not files:
                    self.post_message(
                        Message(
                            channel=channel,
                            source=source,
                            userid=userid,
                            username=username,
                            title="附件读取失败，请稍后重试",
                            save_history=False,
                        )
                    )
                    return False
            elif images:
                image_attachments = self._build_image_attachments(images)
                if (
                        original_images
                        and not image_attachments
                        and not user_message
                        and not files
                ):
                    self.post_message(
                        Message(
                            channel=channel,
                            source=source,
                            userid=userid,
                            username=username,
                            title="附件读取失败，请稍后重试",
                            save_history=False,
                        )
                    )
                    return False
                all_files.extend(image_attachments)
                images = None

            prepared_files = self._prepare_agent_files(
                session_id=session_id,
                files=all_files,
                channel=channel,
                source=source,
            )
            if all_files and not prepared_files and not user_message and not images:
                self.post_message(
                    Message(
                        channel=channel,
                        source=source,
                        userid=userid,
                        username=username,
                        title="文件读取失败，请稍后重试",
                        save_history=False,
                    )
                )
                return False

            process_kwargs = {
                "session_id": session_id,
                "user_id": str(userid),
                "message": user_message,
                "images": images,
                "files": prepared_files,
                "channel": channel.value if channel else None,
                "source": source,
                "username": username,
                "is_channel_admin": is_channel_admin,
                "original_message_id": str(original_message_id)
                if original_message_id
                else None,
                "original_chat_id": original_chat_id,
            }
            if has_audio_input:
                process_kwargs["has_audio_input"] = True
            # 在事件循环中处理，并消费跨线程 Future 的失败，避免队列满时静默丢消息。
            submission_future = asyncio.run_coroutine_threadsafe(
                manager.process_message(**process_kwargs),
                main_loop_registry.require(),
            )

            def _report_agent_submission_failure(completed) -> None:
                try:
                    completed.result()
                except BaseException as error:
                    if isinstance(
                            error,
                            (asyncio.CancelledError, FutureCancelledError),
                    ):
                        return
                    error_code = getattr(error, "code", None)
                    if error_code == "agent_manager_queue_full":
                        title = "智能助手当前排队已满，请稍后重试"
                    elif error_code == "agent_manager_unavailable":
                        title = "智能助手服务暂不可用，请稍后重试"
                    else:
                        title = "智能助手处理失败，请查看日志"
                    logger.warning(f"Agent 消息提交失败: {error}")
                    try:
                        self.post_message(
                            Message(
                                channel=channel,
                                source=source,
                                userid=userid,
                                username=username,
                                title=title,
                                original_message_id=original_message_id,
                                original_chat_id=original_chat_id,
                                save_history=False,
                            )
                        )
                    except Exception as report_error:
                        logger.error(f"发送 Agent 提交失败提示失败: {report_error}")

            if submission_future is not None:
                submission_future.add_done_callback(_report_agent_submission_failure)
            return True

        except Exception as e:
            logger.error(f"处理AI智能体消息失败: {e}", exc_info=True)
            self.messagehelper.put("智能助手执行失败，请稍后重试", role="system", title="MoviePilot助手")
            return False

    def _transcribe_audio_refs(
            self, audio_refs: List[str], channel: NotificationChannel, source: str
    ) -> Optional[str]:
        """
        下载并识别语音消息，仅处理当前已接入的渠道。
        """
        if not audio_refs:
            return None
        if not is_audio_input_available():
            logger.warning("音频输入能力未配置或未启用，跳过语音识别")
            return None

        transcripts = []
        for audio_ref in audio_refs:
            try:
                if audio_ref.startswith("tg://voice_file_id/"):
                    file_id = audio_ref.replace("tg://voice_file_id/", "", 1)
                    content = self.run_module(
                        "download_telegram_file_bytes", file_id=file_id, source=source
                    )
                    filename = "input.ogg"
                elif audio_ref.startswith("tg://audio_file_id/"):
                    file_id = audio_ref.replace("tg://audio_file_id/", "", 1)
                    content = self.run_module(
                        "download_telegram_file_bytes", file_id=file_id, source=source
                    )
                    filename = "input.mp3"
                elif audio_ref.startswith("wxwork://voice_media_id/"):
                    content = self.run_module(
                        "download_wechat_media_bytes",
                        media_ref=audio_ref,
                        source=source,
                    )
                    filename = "input.amr"
                elif audio_ref.startswith("wxclaw://voice/"):
                    content = self.run_module(
                        "download_wechat_media_bytes",
                        media_ref=audio_ref,
                        source=source,
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.amr"
                    )
                elif audio_ref.startswith("slack://file/"):
                    content = self.run_module(
                        "download_slack_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("discord://file/"):
                    content = self.run_module(
                        "download_discord_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("qq://file/"):
                    content = self.run_module(
                        "download_qq_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("vocechat://file/"):
                    content = self.run_module(
                        "download_vocechat_file_bytes",
                        file_ref=audio_ref,
                        source=source,
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("synology://file/"):
                    content = self.run_module(
                        "download_synologychat_file_bytes",
                        file_ref=audio_ref,
                        source=source,
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                elif audio_ref.startswith("wxbot://voice"):
                    continue
                elif audio_ref.startswith("feishu://file/"):
                    content = self.run_module(
                        "download_feishu_file_bytes", file_ref=audio_ref, source=source
                    )
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.opus"
                    )
                elif audio_ref.startswith("http"):
                    content, _ = _read_message_http(audio_ref)
                    filename = self._guess_audio_filename(
                        audio_ref, default="input.ogg"
                    )
                else:
                    logger.debug(
                        "暂不支持的语音引用: channel=%s, source=%s, ref=%s",
                        channel.value if channel else None,
                        source,
                        audio_ref,
                    )
                    continue

                if not content:
                    logger.warning(
                        "语音下载失败，跳过识别: channel=%s, source=%s, ref=%s",
                        channel.value if channel else None,
                        source,
                        audio_ref,
                    )
                    continue

                transcript = transcribe_audio(
                    content=content, filename=filename
                )
                if transcript:
                    transcripts.append(transcript)
                    logger.info(
                        "语音识别成功: channel=%s, source=%s, ref=%s, text_len=%s",
                        channel.value if channel else None,
                        source,
                        audio_ref,
                        len(transcript),
                    )
            except Exception as err:
                logger.error(f"语音识别失败: {err}")

        return "\n".join(transcripts).strip() if transcripts else None

    @staticmethod
    def _guess_audio_filename(audio_ref: str, default: str = "input.ogg") -> str:
        """
        根据引用中的扩展名推测音频文件名，便于 STT 服务识别格式。
        """
        if not audio_ref:
            return default
        raw_ref = unquote(audio_ref).split("?", 1)[0].split("#", 1)[0]
        match = re.search(
            r"([^/]+\.(mp3|m4a|wav|ogg|oga|opus|aac|amr|flac|mpga|mpeg|webm))$",
            raw_ref,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
        return default

    def _download_attachments_to_data_urls(
            self,
            attachments: List[IncomingMessage.MessageImage],
            channel: NotificationChannel,
            source: str,
    ) -> Optional[List[str]]:
        """
        下载可直接提供给 LLM 的附件内容，并统一转换为 data URL。
        """
        normalized_attachments = IncomingMessage.MessageImage.normalize_list(attachments) or []
        if not normalized_attachments:
            return None
        data_urls = []
        for attachment in normalized_attachments:
            attachment_ref = attachment.ref
            try:
                before_count = len(data_urls)
                if attachment_ref.startswith("data:"):
                    data_urls.append(attachment_ref)
                elif attachment_ref.startswith("tg://file_id/"):
                    file_id = attachment_ref.replace("tg://file_id/", "")
                    base64_data = self.run_module(
                        "download_telegram_file_to_base64",
                        file_id=file_id,
                        source=source,
                    )
                    if base64_data:
                        data_urls.append(f"data:image/jpeg;base64,{base64_data}")
                elif attachment_ref.startswith(
                        "wxwork://media_id/"
                ) or attachment_ref.startswith(
                    "wxbot://image/"
                ) or attachment_ref.startswith(
                    "wxclaw://image/"
                ):
                    data_url = self.run_module(
                        "download_wechat_image_to_data_url",
                        image_ref=attachment_ref,
                        source=source,
                    )
                    if data_url:
                        data_urls.append(data_url)
                elif attachment_ref.startswith("feishu://image/"):
                    data_url = self.run_module(
                        "download_feishu_image_to_data_url",
                        image_ref=attachment_ref,
                        source=source,
                    )
                    if data_url:
                        data_urls.append(data_url)
                elif channel == NotificationChannel.Slack:
                    data_url = self.run_module(
                        "download_slack_file_to_data_url",
                        file_url=attachment_ref,
                        source=source,
                    )
                    if data_url:
                        data_urls.append(data_url)
                elif attachment_ref.startswith("vocechat://file/"):
                    data_url = self.run_module(
                        "download_vocechat_image_to_data_url",
                        image_ref=attachment_ref,
                        source=source,
                    )
                    if data_url:
                        data_urls.append(data_url)
                elif attachment_ref.startswith("http"):
                    content, headers = _read_message_http(attachment_ref)
                    if content:
                        base64_data = base64.b64encode(content).decode()
                        mime_type = headers.get("Content-Type", "image/jpeg")
                        data_urls.append(f"data:{mime_type};base64,{base64_data}")
                else:
                    logger.debug(
                        "暂不支持直接转换为 data URL 的附件引用: channel=%s, source=%s, ref=%s",
                        channel.value if channel else None,
                        source,
                        attachment_ref,
                    )
                    continue

                if len(data_urls) > before_count:
                    logger.info(
                        "附件读取成功并已转换为 data URL: channel=%s, source=%s, ref=%s, mime_type=%s",
                        channel.value if channel else None,
                        source,
                        attachment_ref,
                        attachment.mime_type,
                    )
            except Exception as err:
                logger.error(
                    "附件读取失败，无法转换为 data URL: channel=%s, source=%s, ref=%s, error=%s",
                    channel.value if channel else None,
                    source,
                    attachment_ref,
                    err,
                )
        return data_urls if data_urls else None

    def _build_image_attachments(
            self, images: List[IncomingMessage.MessageImage]
    ) -> List[IncomingMessage.MessageAttachment]:
        """
        将图片引用转换为附件描述，以便按文件方式交给 Agent 处理。
        """
        images = IncomingMessage.MessageImage.normalize_list(images)
        if not images:
            return []

        attachments = []
        for index, image in enumerate(images, start=1):
            image_ref = image.ref
            if not image_ref:
                continue
            name = image.name or self._guess_image_attachment_name(image_ref, index)
            mime_type = image.mime_type or self._guess_image_mime_type(image_ref, name)
            attachments.append(
                IncomingMessage.MessageAttachment(
                    ref=image_ref,
                    name=name,
                    mime_type=mime_type,
                    size=image.size,
                )
            )
        return attachments

    def _prepare_agent_files(
            self,
            session_id: str,
            files: Optional[List[IncomingMessage.MessageAttachment]],
            channel: NotificationChannel,
            source: str,
    ) -> Optional[List[dict]]:
        """
        下载用户上传的附件，落盘到临时目录，并生成 Agent 可消费的文件描述。
        """
        if not files:
            return None

        prepared_files = []
        for attachment in files:
            payload = {
                "name": attachment.name,
                "mime_type": attachment.mime_type,
                "size": attachment.size,
                "ref": attachment.ref,
                "status": "download_failed",
            }
            try:
                content = self._download_message_file_bytes(
                    file_ref=attachment.ref,
                    channel=channel,
                    source=source,
                )
                if not content:
                    prepared_files.append(payload)
                    continue

                local_path = self._save_agent_attachment(
                    session_id=session_id,
                    filename=attachment.name,
                    content=content,
                    mime_type=attachment.mime_type,
                )
                payload.update(
                    {
                        "local_path": str(local_path),
                        "status": "ready",
                    }
                )
            except Exception as err:
                logger.error(f"准备附件上下文失败: {attachment.ref}, error: {err}", exc_info=True)
                payload["error"] = "附件读取失败，请稍后重试"
            prepared_files.append(payload)

        return prepared_files or None

    def _download_message_file_bytes(
            self, file_ref: str, channel: NotificationChannel, source: str
    ) -> Optional[bytes]:
        """
        下载消息附件的原始字节内容。
        """
        if not file_ref:
            return None
        if file_ref.startswith("data:"):
            return self._decode_data_url_bytes(file_ref)
        if file_ref.startswith("tg://file_id/"):
            file_id = file_ref.replace("tg://file_id/", "", 1)
            return self.run_module(
                "download_telegram_file_bytes", file_id=file_id, source=source
            )
        if file_ref.startswith("tg://document_file_id/"):
            file_id = file_ref.replace("tg://document_file_id/", "", 1)
            return self.run_module(
                "download_telegram_file_bytes", file_id=file_id, source=source
            )
        if file_ref.startswith("wxwork://media_id/"):
            return self.run_module(
                "download_wechat_media_bytes", media_ref=file_ref, source=source
            )
        if file_ref.startswith("wxwork://file_media_id/"):
            return self.run_module(
                "download_wechat_media_bytes", media_ref=file_ref, source=source
            )
        if file_ref.startswith("wxbot://image/"):
            data_url = self.run_module(
                "download_wechat_image_to_data_url", image_ref=file_ref, source=source
            )
            return self._decode_data_url_bytes(data_url) if data_url else None
        if file_ref.startswith("wxclaw://image/"):
            data_url = self.run_module(
                "download_wechat_image_to_data_url", image_ref=file_ref, source=source
            )
            return self._decode_data_url_bytes(data_url) if data_url else None
        if file_ref.startswith("wxbot://file/"):
            file_url = unquote(file_ref.replace("wxbot://file/", "", 1))
            content, _ = _read_message_http(file_url)
            return content
        if file_ref.startswith("wxclaw://file/") or file_ref.startswith("wxclaw://voice/"):
            return self.run_module(
                "download_wechat_media_bytes", media_ref=file_ref, source=source
            )
        if file_ref.startswith("feishu://file/"):
            return self.run_module(
                "download_feishu_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("slack://file/"):
            return self.run_module(
                "download_slack_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("discord://file/"):
            return self.run_module(
                "download_discord_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("qq://file/"):
            return self.run_module(
                "download_qq_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("vocechat://file/"):
            return self.run_module(
                "download_vocechat_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("synology://file/"):
            return self.run_module(
                "download_synologychat_file_bytes", file_ref=file_ref, source=source
            )
        if file_ref.startswith("http"):
            if channel == NotificationChannel.Slack:
                data_url = self.run_module(
                    "download_slack_file_to_data_url", file_url=file_ref, source=source
                )
                return self._decode_data_url_bytes(data_url) if data_url else None
            content, _ = _read_message_http(file_ref)
            return content
        logger.debug(
            "暂不支持的附件引用: channel=%s, source=%s, ref=%s",
            channel.value if channel else None,
            source,
            file_ref,
        )
        return None

    def _save_agent_attachment(
            self,
            session_id: str,
            filename: Optional[str],
            content: bytes,
            mime_type: Optional[str] = None,
    ) -> Path:
        """
        将用户上传文件写入临时目录，并返回本地路径。
        """
        safe_name = self._sanitize_attachment_name(filename, mime_type)
        base_dir = self.runtime_config.temporary_path / "agent_uploads" / session_id
        base_dir.mkdir(parents=True, exist_ok=True)

        file_id = uuid.uuid4().hex[:8]
        local_path = base_dir / f"{file_id}_{safe_name}"
        local_path.write_bytes(content or b"")
        return local_path

    @staticmethod
    def _sanitize_attachment_name(
            filename: Optional[str], mime_type: Optional[str] = None
    ) -> str:
        """
        规范化附件文件名，避免路径穿越和非法字符。
        """
        name = Path(filename or "attachment").name
        name = re.sub(r"[^\w.\-]+", "_", name, flags=re.ASCII).strip("._")
        if not name:
            name = "attachment"
        if "." not in name:
            mime = (mime_type or "").split(";", 1)[0].strip().lower()
            default_ext = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "image/bmp": ".bmp",
                "application/json": ".json",
                "text/plain": ".txt",
                "text/markdown": ".md",
                "text/csv": ".csv",
            }.get(mime)
            if default_ext:
                name = f"{name}{default_ext}"
        return name

    @staticmethod
    def _guess_image_attachment_name(image_ref: str, index: int) -> str:
        """
        根据图片引用推测附件名。
        """
        if not image_ref:
            return f"image_{index}.jpg"
        if image_ref.startswith("data:"):
            mime_part = image_ref[5:].split(";", 1)[0].strip().lower()
            ext = mimetypes.guess_extension(mime_part) or ".jpg"
            return f"image_{index}{ext}"

        parsed = urlparse(unquote(image_ref))
        name = Path(parsed.path).name if parsed.path else ""
        if name and "." in name:
            return name
        return f"image_{index}.jpg"

    @staticmethod
    def _guess_image_mime_type(image_ref: str, filename: Optional[str]) -> str:
        """
        根据图片引用或文件名推测 MIME 类型。
        """
        if image_ref and image_ref.startswith("data:"):
            mime = image_ref[5:].split(";", 1)[0].strip().lower()
            return mime or "image/jpeg"
        guessed, _ = mimetypes.guess_type(filename or "")
        if guessed and guessed.startswith("image/"):
            return guessed
        return "image/jpeg"

    @staticmethod
    def _decode_data_url_bytes(data_url: Optional[str]) -> Optional[bytes]:
        """
        将 data URL 解码为原始字节。
        """
        if not data_url or not data_url.startswith("data:"):
            return None
        try:
            _, payload = data_url.split(",", 1)
        except ValueError:
            return None
        try:
            return base64.b64decode(payload)
        except Exception as e:
            logger.error(e)
            return None
