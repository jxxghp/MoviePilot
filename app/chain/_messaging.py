"""消息处理与通知发送 mixin。

从 ChainBase 拆出的消息域：渠道输入状态机、通知派发规范化、消息渲染、
隔离路由与队列发送。方法经 MRO 解析，依赖 ChainBase 实例的 run_module、
eventmanager、messageoper、messagequeue 等协作对象。
"""
import copy
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, cast

from app.application.messaging.message import MessageTemplateHelper
from app.application.notification import get_notification_switch
from app.application.security.user import ChainUserRepository
from app.chain._contracts import ChainRuntimeMixinHost
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.foundation.identity import normalize_internal_user_id
from app.runtime.correlation import correlation_scope
from app.runtime.log import logger
from app.schemas.message import Message, MessageResponse
from app.schemas.notification import ChannelCapability, ChannelCapabilityManager
from app.schemas.transfer import TransferInfo
from app.schemas.types import EventType, NotificationChannel


@dataclass(frozen=True, slots=True)
class _NotificationRouteLookup:
    """通知路由状态机请求读取指定用户的渠道设置。"""

    username: str
    log_message: str


@dataclass(frozen=True, slots=True)
class _NotificationRouteDelivery:
    """通知路由状态机产出的单次投递决策。"""

    message: Message
    immediately: bool


@dataclass(frozen=True, slots=True)
class _NotificationRouteLog:
    """通知路由状态机产出的无投递日志决策。"""

    message: str


_NotificationRouteStep = Union[
    _NotificationRouteLookup,
    _NotificationRouteDelivery,
    _NotificationRouteLog,
]


def _notification_route_steps(
    message: Message,
    *,
    action: Optional[str],
    superuser: str,
) -> Generator[
    _NotificationRouteStep,
    Optional[Mapping[str, object]],
    None,
]:
    """
    生成同步和异步通知共用的路由状态转换。

    生成器只表达用户设置查询、日志和投递决策，调用方分别执行真实同步或
    异步 I/O，避免两套外壳各自维护管理员回退和原消息投递规则。
    """
    admin_sent = False
    send_original = not action
    for route_action in action.split(",") if action else ():
        routed_message = copy.deepcopy(message)
        if route_action == "admin" and not admin_sent:
            settings = yield _NotificationRouteLookup(
                username=superuser,
                log_message=f"{routed_message.mtype} 的消息已设置发送给管理员",
            )
            routed_message.targets = cast(dict[str, Any], settings)
            admin_sent = True
        elif route_action == "user" and routed_message.username:
            username = cast(str, routed_message.username)
            settings = yield _NotificationRouteLookup(
                username=username,
                log_message=(
                    f"{routed_message.mtype} 的消息已设置发送给用户 {username}"
                ),
            )
            routed_message.targets = cast(dict[str, Any], settings)
            if settings is None:
                if not admin_sent:
                    settings = yield _NotificationRouteLookup(
                        username=superuser,
                        log_message=(
                            f"用户 {username} 不存在，消息将发送给管理员"
                        ),
                    )
                    routed_message.targets = cast(dict[str, Any], settings)
                    admin_sent = True
                else:
                    yield _NotificationRouteLog(
                        message=f"用户 {username} 不存在，消息无法发送到对应用户"
                    )
                    continue
            elif username == superuser:
                admin_sent = True
        else:
            send_original = not admin_sent
            break
        yield _NotificationRouteDelivery(
            message=routed_message,
            immediately=False,
        )

    if send_original:
        yield _NotificationRouteDelivery(
            message=message,
            immediately=bool(message.userid),
        )


def _render_notification_message(
    *,
    message: Optional[Message],
    meta: Optional[MetaBase],
    mediainfo: Optional[Union[MediaInfo, MusicInfo]],
    torrentinfo: Optional[TorrentInfo],
    transferinfo: Optional[TransferInfo],
    kwargs: dict[str, Any],
) -> Optional[Message]:
    """使用同步和异步入口共用的上下文规范化并渲染通知。"""
    kwargs.setdefault("current_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return MessageTemplateHelper.render(
        message=message,
        meta=meta,
        mediainfo=mediainfo,
        torrentinfo=torrentinfo,
        transferinfo=transferinfo,
        **kwargs,
    )


def _notification_route_action(message: Message) -> Optional[str]:
    """仅为未绑定真实用户的业务通知读取隔离路由配置。"""
    if message.userid or not message.mtype:
        return None
    return get_notification_switch(message.mtype)


class MessageProcessingMixin:
    """消息输入/处理状态机与通知派发规范化。"""

    __mixin_host_protocol__ = ChainRuntimeMixinHost

    def start_message_processing_status(
            self,
            channel: NotificationChannel,
            source: Optional[str],
            userid: Optional[Union[str, int]] = None,
            message_id: Optional[Union[str, int]] = None,
            chat_id: Optional[Union[str, int]] = None,
            text: Optional[str] = None,
    ) -> Optional[dict]:
        """
        启动渠道侧消息输入/处理状态。
        具体表现由消息模块实现，例如 typing 保活或消息 reaction。
        """
        if not channel or not ChannelCapabilityManager.supports_capability(
                channel, ChannelCapability.PROCESSING_STATUS
        ):
            return None
        try:
            status = self.run_module(
                "mark_message_processing_started",
                channel=channel,
                source=source,
                userid=userid,
                message_id=message_id,
                chat_id=chat_id,
                text=text,
            )
        except Exception as err:
            logger.debug(f"启动消息处理状态失败: {err}")
            return None
        return status if isinstance(status, dict) else None

    def finish_message_processing_status(
            self,
            status: Optional[dict] = None,
            channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None,
            userid: Optional[Union[str, int]] = None,
            message_id: Optional[Union[str, int]] = None,
            chat_id: Optional[Union[str, int]] = None,
    ) -> None:
        """
        结束渠道侧消息输入/处理状态。
        优先使用 start 返回的 status，缺失时使用显式渠道和消息定位参数。
        """
        target_channel = channel
        if status:
            try:
                target_channel = NotificationChannel(status.get("channel"))
            except Exception:
                target_channel = channel
        if not target_channel or not ChannelCapabilityManager.supports_capability(
                target_channel, ChannelCapability.PROCESSING_STATUS
        ):
            return
        try:
            self.run_module(
                "mark_message_processing_finished",
                channel=target_channel,
                source=(status or {}).get("source") or source,
                userid=(status or {}).get("userid") or userid,
                message_id=(status or {}).get("message_id") or message_id,
                chat_id=(status or {}).get("chat_id") or chat_id,
                status=status,
            )
        except Exception as err:
            logger.debug(f"结束消息处理状态失败: {err}")

    @staticmethod
    def _normalize_notification_for_dispatch(
            message: Message
    ) -> Message:
        """
        规范化待发送的通知消息。
        后台任务会复用内部占位用户ID作为会话身份，这里在真正发送前清空，
        让消息重新走默认通知路由或基于 targets 的目标解析。
        """
        dispatch_message = copy.deepcopy(message)
        dispatch_message.userid = normalize_internal_user_id(
            dispatch_message.userid
        )
        return dispatch_message

    @staticmethod
    def _build_notice_message_data(message: Message) -> dict:
        """
        构造消息通知事件数据。
        """
        return {**message.model_dump(exclude={"save_history"}), "type": message.mtype}


class NotificationMixin:
    """通知消息发送域：渲染、隔离路由、队列发送与消息编辑。"""

    __mixin_host_protocol__ = ChainRuntimeMixinHost
    user_repository: ChainUserRepository

    def post_message(
            self,
            message: Optional[Message] = None,
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
            torrentinfo: Optional[TorrentInfo] = None,
            transferinfo: Optional[TransferInfo] = None,
            **kwargs,
    ) -> None:
        """
        发送消息
        :param message:  Notification实例
        :param meta:  元数据
        :param mediainfo:  媒体信息
        :param torrentinfo:  种子信息
        :param transferinfo:  文件整理信息
        :param kwargs:  其他参数(覆盖业务对象属性值)
        :return: 成功或失败
        """
        strict_delivery = bool(kwargs.pop("_strict_delivery", False))
        strict_source = kwargs.pop("_strict_source", None)
        message = _render_notification_message(
            message=message,
            meta=meta,
            mediainfo=mediainfo,
            torrentinfo=torrentinfo,
            transferinfo=transferinfo,
            kwargs=kwargs,
        )
        if not message:
            logger.warning("消息为空，跳过发送")
            return
        if message.save_history:
            if not strict_source or not self.messageoper.exists_by_source(
                strict_source
            ):
                self.messageoper.add(**message.model_dump())
        dispatch_message = self._normalize_notification_for_dispatch(message)
        if strict_source and dispatch_message.source == strict_source:
            dispatch_message.source = None
        self._dispatch_notification_steps(
            dispatch_message,
            strict_delivery=strict_delivery,
            kwargs=kwargs,
        )

    def post_message_strict(self, message: Message, *, event_key: str) -> None:
        """同步执行真实通知 provider，并通过调用上下文携带稳定事件键。"""
        durable_message = message.model_copy(deep=True)
        strict_source = None
        if not durable_message.source:
            strict_source = f"outbox:{event_key}"
            durable_message.source = strict_source
        with correlation_scope(event_key):
            self.post_message(
                durable_message,
                _strict_delivery=True,
                _strict_source=strict_source,
            )

    def _deliver_notification(
        self,
        message: Message,
        *,
        strict_delivery: bool,
        immediately: bool,
        kwargs: dict[str, object],
    ) -> None:
        """普通通知使用调度队列；durable 恢复同步执行并传播 provider 错误。"""
        if strict_delivery:
            host = cast(ChainRuntimeMixinHost, self)
            host.run_module_strict(
                "post_message",
                message=message,
            )
            return
        self.messagequeue.send_message(
            "post_message",
            message=message,
            immediately=immediately,
            **kwargs,
        )

    def _dispatch_notification_steps(
        self,
        message: Message,
        *,
        strict_delivery: bool,
        kwargs: dict[str, object],
    ) -> None:
        """执行通知路由状态机，并在同步边界完成查询、事件和投递。"""
        steps = _notification_route_steps(
            message,
            action=_notification_route_action(message),
            superuser=self.runtime_config.superuser,
        )
        lookup_result: Optional[Mapping[str, object]] = None
        try:
            step = next(steps)
        except StopIteration:
            return
        while True:
            lookup_result = None
            if isinstance(step, _NotificationRouteLookup):
                logger.info(step.log_message)
                lookup_result = self.user_repository.get_notification_settings(
                    step.username
                )
            elif isinstance(step, _NotificationRouteLog):
                logger.info(step.message)
            else:
                self.eventmanager.send_event(
                    etype=EventType.NoticeMessage,
                    data=self._build_notice_message_data(step.message),
                )
                self._deliver_notification(
                    step.message,
                    strict_delivery=strict_delivery,
                    immediately=step.immediately,
                    kwargs=kwargs,
                )
            try:
                step = steps.send(lookup_result)
            except StopIteration:
                return

    async def async_post_message(
            self,
            message: Optional[Message] = None,
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
            torrentinfo: Optional[TorrentInfo] = None,
            transferinfo: Optional[TransferInfo] = None,
            **kwargs,
    ) -> None:
        """
        异步发送消息
        :param message:  Notification实例
        :param meta:  元数据
        :param mediainfo:  媒体信息
        :param torrentinfo:  种子信息
        :param transferinfo:  文件整理信息
        :param kwargs:  其他参数(覆盖业务对象属性值)
        :return: 成功或失败
        """
        message = _render_notification_message(
            message=message,
            meta=meta,
            mediainfo=mediainfo,
            torrentinfo=torrentinfo,
            transferinfo=transferinfo,
            kwargs=kwargs,
        )
        if not message:
            logger.warning("消息为空，跳过发送")
            return
        if message.save_history:
            await self.messageoper.async_add(**message.model_dump())
        dispatch_message = self._normalize_notification_for_dispatch(message)
        await self._async_dispatch_notification_steps(
            dispatch_message,
            kwargs=kwargs,
        )

    async def _async_dispatch_notification_steps(
        self,
        message: Message,
        *,
        kwargs: dict[str, object],
    ) -> None:
        """执行通知路由状态机，并在异步边界完成查询、事件和投递。"""
        steps = _notification_route_steps(
            message,
            action=_notification_route_action(message),
            superuser=self.runtime_config.superuser,
        )
        lookup_result: Optional[Mapping[str, object]] = None
        try:
            step = next(steps)
        except StopIteration:
            return
        while True:
            lookup_result = None
            if isinstance(step, _NotificationRouteLookup):
                logger.info(step.log_message)
                lookup_result = (
                    await self.user_repository.async_get_notification_settings(
                        step.username
                    )
                )
            elif isinstance(step, _NotificationRouteLog):
                logger.info(step.message)
            else:
                await self.eventmanager.async_send_event(
                    etype=EventType.NoticeMessage,
                    data=self._build_notice_message_data(step.message),
                )
                await self.messagequeue.async_send_message(
                    "post_message",
                    message=step.message,
                    immediately=step.immediately,
                    **kwargs,
                )
            try:
                step = steps.send(lookup_result)
            except StopIteration:
                return

    def post_medias_message(
            self, message: Message, medias: List[MediaInfo]
    ) -> None:
        """
        发送媒体信息选择列表
        :param message:  消息体
        :param medias:  媒体列表
        :return: 成功或失败
        """
        note_list = [media.to_dict() for media in medias]
        if message.save_history:
            self.messageoper.add(**message.model_dump(), note=note_list)
        dispatch_message = self._normalize_notification_for_dispatch(message)
        return self.messagequeue.send_message(
            "post_medias_message",
            message=dispatch_message,
            medias=medias,
            immediately=True if dispatch_message.userid else False,
        )

    def post_torrents_message(
            self, message: Message, torrents: List[Context]
    ) -> None:
        """
        发送种子信息选择列表
        :param message:  消息体
        :param torrents:  种子列表
        :return: 成功或失败
        """
        note_list = [torrent.torrent_info.to_dict() for torrent in torrents]
        if message.save_history:
            self.messageoper.add(**message.model_dump(), note=note_list)
        dispatch_message = self._normalize_notification_for_dispatch(message)
        return self.messagequeue.send_message(
            "post_torrents_message",
            message=dispatch_message,
            torrents=torrents,
            immediately=True if dispatch_message.userid else False,
        )

    def delete_message(
            self,
            channel: NotificationChannel,
            source: str,
            message_id: Union[str, int],
            chat_id: Optional[Union[str, int]] = None,
    ) -> bool:
        """
        删除消息
        :param channel: 消息渠道
        :param source: 消息源（指定特定的消息模块）
        :param message_id: 消息ID
        :param chat_id: 聊天ID（如群组ID）
        :return: 删除是否成功
        """
        return self.run_module(
            "delete_message",
            channel=channel,
            source=source,
            message_id=message_id,
            chat_id=chat_id,
        )

    def edit_message(
            self,
            channel: NotificationChannel,
            source: str,
            message_id: Union[str, int],
            chat_id: Union[str, int],
            text: str,
            title: Optional[str] = None,
            buttons: Optional[List[List[dict]]] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        编辑已发送的消息
        :param channel: 消息渠道
        :param source: 消息源（指定特定的消息模块）
        :param message_id: 消息ID
        :param chat_id: 聊天ID
        :param text: 新的消息内容
        :param title: 消息标题
        :param buttons: 更新后的按钮列表
        :param metadata: 其他消息元数据
        :return: 编辑是否成功
        """
        if channel == NotificationChannel.WebAgent:
            try:
                from app.application.messaging.agent import edit_web_agent_message

                return edit_web_agent_message(
                    user_id=str((metadata or {}).get("userid") or ""),
                    message_id=message_id,
                    title=title,
                    text=text,
                    buttons=buttons,
                )
            except Exception as err:
                logger.debug(f"编辑 WebAgent 消息失败: {err}")
                return False

        return self.run_module(
            "edit_message",
            channel=channel,
            source=source,
            message_id=message_id,
            chat_id=chat_id,
            text=text,
            title=title,
            buttons=buttons,
            metadata=metadata,
        )

    def send_direct_message(self, message: Message) -> Optional[MessageResponse]:
        """
        直接发送消息并返回消息ID等信息（用于后续编辑消息的场景）
        不经过消息队列、不保存消息历史
        :param message: 消息体
        :return: 消息响应（包含message_id, chat_id等）
        """
        return self.run_module(
            "send_direct_message",
            message=self._normalize_notification_for_dispatch(message),
        )

    def finalize_message(
            self,
            response: MessageResponse,
    ) -> bool:
        """
        对已发送消息执行渠道收尾动作。
        例如关闭流式卡片状态；无特殊收尾的渠道直接返回 False。
        """
        return self.run_module("finalize_message", response=response)
