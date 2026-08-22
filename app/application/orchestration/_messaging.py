"""消息处理与通知发送 mixin。

从 ChainBase 拆出的消息域：渠道输入状态机、通知派发规范化、消息渲染、
隔离路由与队列发送。方法经 MRO 解析，依赖 ChainBase 实例的 unicast、broadcast、
eventmanager、messageoper、messagequeue 等协作对象。
"""
import copy
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from app.application.orchestration.data import UserPortProxy as UserOper
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.foundation.identity import normalize_internal_user_id
from app.application.messaging.message import MessageTemplateHelper
from app.runtime.extensions.service_config import ServiceConfigHelper
from app.runtime.log import logger
from app.schemas.message import MessageResponse
from app.schemas.message import Message
from app.schemas.transfer import TransferInfo
from app.schemas.notification import (
    ChannelCapability,
    ChannelCapabilityManager,
    ChannelRef,
    resolve_channel,
)
from app.schemas.types import EventType, NotificationChannel


class MessageProcessingMixin:
    """消息输入/处理状态机与通知派发规范化。"""

    def start_message_processing_status(
            self,
            channel: ChannelRef,
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
            status = self.unicast(
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
            channel: Optional[ChannelRef] = None,
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
            target_channel = resolve_channel(status.get("channel")) or channel
        if not target_channel or not ChannelCapabilityManager.supports_capability(
                target_channel, ChannelCapability.PROCESSING_STATUS
        ):
            return
        try:
            self.broadcast(
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
        # 添加格式化的时间参数
        kwargs.setdefault("current_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # 渲染消息
        message = MessageTemplateHelper.render(
            message=message,
            meta=meta,
            mediainfo=mediainfo,
            torrentinfo=torrentinfo,
            transferinfo=transferinfo,
            **kwargs,
        )
        # 检查消息是否有效
        if not message:
            logger.warning("消息为空，跳过发送")
            return
        if message.save_history:
            self.messageoper.add(**message.model_dump())
        dispatch_message = self._normalize_notification_for_dispatch(message)
        # 发送消息按设置隔离
        if not dispatch_message.userid and dispatch_message.mtype:
            # 消息隔离设置
            notify_action = ServiceConfigHelper.get_notification_switch(
                dispatch_message.mtype
            )
            if notify_action:
                # 'admin' 'user,admin' 'user' 'all'
                actions = notify_action.split(",")
                # 是否已发送管理员标志
                admin_sended = False
                send_orignal = False
                useroper = UserOper()
                for action in actions:
                    send_message = copy.deepcopy(dispatch_message)
                    if action == "admin" and not admin_sended:
                        # 仅发送管理员
                        logger.info(f"{send_message.mtype} 的消息已设置发送给管理员")
                        # 读取管理员消息IDS
                        send_message.targets = useroper.get_settings(
                            self.runtime_config.superuser
                        )
                        admin_sended = True
                    elif action == "user" and send_message.username:
                        # 发送对应用户
                        logger.info(
                            f"{send_message.mtype} 的消息已设置发送给用户 {send_message.username}"
                        )
                        # 读取用户消息IDS
                        send_message.targets = useroper.get_settings(
                            send_message.username
                        )
                        if send_message.targets is None:
                            # 没有找到用户
                            if not admin_sended:
                                # 回滚发送管理员
                                logger.info(
                                    f"用户 {send_message.username} 不存在，消息将发送给管理员"
                                )
                                # 读取管理员消息IDS
                                send_message.targets = useroper.get_settings(
                                    self.runtime_config.superuser
                                )
                                admin_sended = True
                            else:
                                # 管理员发过了，此消息不发了
                                logger.info(
                                    f"用户 {send_message.username} 不存在，消息无法发送到对应用户"
                                )
                                continue
                        elif send_message.username == self.runtime_config.superuser:
                            # 管理员同名已发送
                            admin_sended = True
                    else:
                        # 按原消息发送全体
                        if not admin_sended:
                            send_orignal = True
                        break
                    # 按设定发送
                    self.eventmanager.send_event(
                        etype=EventType.NoticeMessage,
                        data=self._build_notice_message_data(send_message),
                    )
                    self.messagequeue.send_message(
                        "post_message", message=send_message, **kwargs
                    )
                if not send_orignal:
                    return
        # 发送消息事件
        self.eventmanager.send_event(
            etype=EventType.NoticeMessage,
            data=self._build_notice_message_data(dispatch_message),
        )
        # 按原消息发送
        self.messagequeue.send_message(
            "post_message",
            message=dispatch_message,
            immediately=True if dispatch_message.userid else False,
            **kwargs,
        )

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
        # 添加格式化的时间参数
        kwargs.setdefault("current_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # 渲染消息
        message = MessageTemplateHelper.render(
            message=message,
            meta=meta,
            mediainfo=mediainfo,
            torrentinfo=torrentinfo,
            transferinfo=transferinfo,
            **kwargs,
        )
        # 检查消息是否有效
        if not message:
            logger.warning("消息为空，跳过发送")
            return
        if message.save_history:
            await self.messageoper.async_add(**message.model_dump())
        dispatch_message = self._normalize_notification_for_dispatch(message)
        # 发送消息按设置隔离
        if not dispatch_message.userid and dispatch_message.mtype:
            # 消息隔离设置
            notify_action = ServiceConfigHelper.get_notification_switch(
                dispatch_message.mtype
            )
            if notify_action:
                # 'admin' 'user,admin' 'user' 'all'
                actions = notify_action.split(",")
                # 是否已发送管理员标志
                admin_sended = False
                send_orignal = False
                useroper = UserOper()
                for action in actions:
                    send_message = copy.deepcopy(dispatch_message)
                    if action == "admin" and not admin_sended:
                        # 仅发送管理员
                        logger.info(f"{send_message.mtype} 的消息已设置发送给管理员")
                        # 读取管理员消息IDS
                        send_message.targets = useroper.get_settings(
                            self.runtime_config.superuser
                        )
                        admin_sended = True
                    elif action == "user" and send_message.username:
                        # 发送对应用户
                        logger.info(
                            f"{send_message.mtype} 的消息已设置发送给用户 {send_message.username}"
                        )
                        # 读取用户消息IDS
                        send_message.targets = useroper.get_settings(
                            send_message.username
                        )
                        if send_message.targets is None:
                            # 没有找到用户
                            if not admin_sended:
                                # 回滚发送管理员
                                logger.info(
                                    f"用户 {send_message.username} 不存在，消息将发送给管理员"
                                )
                                # 读取管理员消息IDS
                                send_message.targets = useroper.get_settings(
                                    self.runtime_config.superuser
                                )
                                admin_sended = True
                            else:
                                # 管理员发过了，此消息不发了
                                logger.info(
                                    f"用户 {send_message.username} 不存在，消息无法发送到对应用户"
                                )
                                continue
                        elif send_message.username == self.runtime_config.superuser:
                            # 管理员同名已发送
                            admin_sended = True
                    else:
                        # 按原消息发送全体
                        if not admin_sended:
                            send_orignal = True
                        break
                    # 按设定发送
                    await self.eventmanager.async_send_event(
                        etype=EventType.NoticeMessage,
                        data=self._build_notice_message_data(send_message),
                    )
                    await self.messagequeue.async_send_message(
                        "post_message", message=send_message, **kwargs
                    )
                if not send_orignal:
                    return
        # 发送消息事件
        await self.eventmanager.async_send_event(
            etype=EventType.NoticeMessage,
            data=self._build_notice_message_data(dispatch_message),
        )
        # 按原消息发送
        await self.messagequeue.async_send_message(
            "post_message",
            message=dispatch_message,
            immediately=True if dispatch_message.userid else False,
            **kwargs,
        )

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
        return self.unicast(
            "delete_message",
            channel=channel,
            source=source,
            message_id=message_id,
            chat_id=chat_id,
        )

    def edit_message(
            self,
            channel: ChannelRef,
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

        return self.unicast(
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
        return self.unicast(
            "send_direct_message",
            message=self._normalize_notification_for_dispatch(message),
        )

    def finalize_message(
            self,
            response: MessageResponse,
    ) -> Optional[bool]:
        """
        对已发送消息执行渠道收尾动作。

        :param response: 消息发送响应，携带渠道与渠道自定义上下文
        :return: 收尾结果；无渠道认领这条消息时为 ``None``
        """
        return self.unicast("finalize_message", response=response)
