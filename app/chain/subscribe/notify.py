"""订阅新增、失败与完成通知"""

import threading
from collections.abc import Mapping
from typing import Any, List, Optional, Protocol, cast

from app.application.configuration import (
    get_chain_runtime_config_snapshot,
)
from app.application.messaging.message import MessageTemplateHelper
from app.application.subscription.contract import (
    SubscriptionSnapshot,
    subscription_added_event_key,
)
from app.chain.subscribe.context import _SubscribeCreateContext, _SubscribePostCommitContext
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.domain.context import (
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.common import JsonData
from app.schemas.message import Message as _SchemaMessage
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    ContentType,
    EventType,
    MediaType,
    MessageType,
)


class SubscriptionSharePort(Protocol):
    """订阅链访问服务端共享与统计能力所需的最小端口。"""

    def report_added(self, payload: dict[str, Any]) -> bool:
        """同步上报新增订阅统计。"""
        ...

    async def async_report_added(self, payload: dict[str, Any]) -> bool:
        """异步上报新增订阅统计。"""
        ...

    def list_shares(self) -> List[dict[str, Any]]:
        """读取当前用户可见的订阅分享。"""
        ...

    def report_completed(self, payload: Mapping[str, JsonData]) -> bool:
        """同步上报订阅完成统计。"""
        ...


_subscription_share_lock = threading.RLock()
_subscription_share_port: Optional[SubscriptionSharePort] = None


def configure_subscription_share_port(
    port: SubscriptionSharePort,
) -> Optional[SubscriptionSharePort]:
    """装配订阅共享端口，并返回旧实现供隔离环境恢复。"""
    global _subscription_share_port
    with _subscription_share_lock:
        previous = _subscription_share_port
        _subscription_share_port = port
    return previous


def reset_subscription_share_port(
    port: Optional[SubscriptionSharePort] = None,
) -> None:
    """恢复指定订阅共享端口；省略参数时回到未装配状态。"""
    global _subscription_share_port
    with _subscription_share_lock:
        _subscription_share_port = port


def _subscription_share_snapshot() -> SubscriptionSharePort:
    """获取当前订阅共享端口，未装配时稳定失败。"""
    with _subscription_share_lock:
        port = _subscription_share_port
    if port is None:
        raise RuntimeError("订阅共享端口尚未由启动组合根装配")
    return port


class SubscribeNotificationOwner(_SubscribeOwnerBase):
    """订阅新增、失败与完成通知，作为 SubscribeChain 的单一职责实现 owner。"""

    @staticmethod
    def _SubscribeChain__subscribe_added_link(mtype: MediaType) -> Optional[str]:
        """返回订阅类型对应的前端详情入口。"""
        if mtype == MediaType.TV:
            result: Optional[str] = get_chain_runtime_config_snapshot().television_subscribe_url
            return result
        if mtype == MediaType.MUSIC:
            result = get_chain_runtime_config_snapshot().music_subscribe_url
            return result
        result = get_chain_runtime_config_snapshot().movie_subscribe_url
        return result

    @staticmethod
    def _SubscribeChain__subscribe_report_payload(context: _SubscribePostCommitContext) -> dict[str, Any]:
        """构造保持旧字段和值语义的订阅统计上报。"""
        mediainfo = context.mediainfo
        music_type = getattr(mediainfo, "music_type", None)
        return {
            "name": context.title,
            "year": context.year,
            "type": context.metainfo.type.value,
            "media_source": context.media_source,
            "media_id": context.media_id,
            "music_type": music_type,
            "total_tracks": getattr(mediainfo, "total_tracks", None) if music_type == MUSIC_ENTITY_ALBUM else None,
            "season": context.season,
            "poster": mediainfo.get_poster_image(),
            "backdrop": mediainfo.get_backdrop_image(),
            "vote": mediainfo.vote_average,
            "description": mediainfo.overview,
        }

    def _SubscribeChain__post_subscribe_added(
        self,
        subscribe_id: int,
        context: _SubscribePostCommitContext,
    ) -> bool:
        """同步执行提交后消息和事件；统计失败留给 outbox 重试。"""
        if context.notification:
            self.post_message(_SchemaMessage.model_validate(context.notification))
        self.eventmanager.send_event(
            EventType.SubscribeAdded,
            {
                "subscribe_id": subscribe_id,
                "idempotency_key": subscription_added_event_key(
                    subscribe_id,
                    {
                        "media_source": str(context.media_source) if context.media_source else None,
                        "media_id": context.media_id,
                    },
                    occurrence_id=context.occurrence_id,
                ),
                "username": context.username,
                "mediainfo": context.mediainfo.to_dict(),
            },
        )
        try:
            self._SubscribeChain__queue_new_subscription_search(subscribe_id)
        except Exception:
            logger.warning("订阅已保存，但自动搜索暂时没有安排成功，系统会在下一次检查时重试")
            logger.debug("安排订阅自动搜索失败", exc_info=True)
        try:
            report_delivered = _subscription_share_snapshot().report_added(
                self._SubscribeChain__subscribe_report_payload(context)
            )
        except Exception:
            logger.warning("订阅新增统计暂时没有上报成功，系统会在后台重试")
            logger.debug("订阅新增统计上报失败", exc_info=True)
            return False
        if not report_delivered:
            logger.warning("订阅新增统计上报未确认，将由后台重试")
        return report_delivered

    async def _SubscribeChain__async_post_subscribe_added(
        self,
        subscribe_id: int,
        context: _SubscribePostCommitContext,
    ) -> bool:
        """异步执行提交后消息和事件；统计失败留给 outbox 重试。"""
        if context.notification:
            await self.async_post_message(_SchemaMessage.model_validate(context.notification))
        await self.eventmanager.async_send_event(
            EventType.SubscribeAdded,
            {
                "subscribe_id": subscribe_id,
                "idempotency_key": subscription_added_event_key(
                    subscribe_id,
                    {
                        "media_source": str(context.media_source) if context.media_source else None,
                        "media_id": context.media_id,
                    },
                    occurrence_id=context.occurrence_id,
                ),
                "username": context.username,
                "mediainfo": context.mediainfo.to_dict(),
            },
        )
        try:
            await self._SubscribeChain__async_queue_new_subscription_search(subscribe_id)
        except Exception:
            logger.warning("订阅已保存，但自动搜索暂时没有安排成功，系统会在下一次检查时重试")
            logger.debug("安排订阅自动搜索失败", exc_info=True)
        try:
            report_delivered = await _subscription_share_snapshot().async_report_added(
                self._SubscribeChain__subscribe_report_payload(context)
            )
        except Exception:
            logger.warning("订阅新增统计暂时没有上报成功，系统会在后台重试")
            logger.debug("订阅新增统计上报失败", exc_info=True)
            return False
        if not report_delivered:
            logger.warning("订阅新增统计上报未确认，将由后台重试")
        return report_delivered

    def _SubscribeChain__build_subscribe_notification(
        self,
        context: _SubscribeCreateContext,
    ) -> Optional[dict[str, Any]]:
        """在事务提交前冻结已渲染消息，供即时发送与 outbox 恢复共用。"""
        if not context.message:
            return None
        message = _SchemaMessage(
            channel=context.channel,
            source=context.source,
            mtype=MessageType.Subscribe,
            ctype=ContentType.SubscribeAdded,
            image=context.mediainfo.get_message_image(),
            link=self._SubscribeChain__subscribe_added_link(context.mediainfo.type),
            userid=context.userid,
            username=context.username,
        )
        rendered = (
            MessageTemplateHelper.render(
                message,
                meta=context.metainfo,
                mediainfo=context.mediainfo,
                username=context.username,
            )
            or message
        )
        return cast(dict[str, Any], rendered.model_dump(mode="json"))

    def _SubscribeChain__notify_subscribe_create_failure(
        self,
        context: _SubscribeCreateContext,
        err_msg: str,
    ) -> None:
        """同步记录持久化失败，并按旧规则向原用户反馈。"""
        logger.error(f"{context.mediainfo.title_year} {err_msg}")
        if context.exist_ok or not context.message:
            return
        self.post_message(self._SubscribeChain__subscribe_create_failure_message(context, err_msg))

    async def _SubscribeChain__async_notify_subscribe_create_failure(
        self,
        context: _SubscribeCreateContext,
        err_msg: str,
    ) -> None:
        """异步记录持久化失败，并按旧规则向原用户反馈。"""
        logger.error(f"{context.mediainfo.title_year} {err_msg}")
        if context.exist_ok or not context.message:
            return
        await self.async_post_message(self._SubscribeChain__subscribe_create_failure_message(context, err_msg))

    @staticmethod
    def _SubscribeChain__subscribe_create_failure_message(
        context: _SubscribeCreateContext,
        err_msg: str,
    ) -> _SchemaMessage:
        """构造保持旧标题、图片和接收人字段的订阅失败消息。"""
        return _SchemaMessage(
            channel=context.channel,
            source=context.source,
            mtype=MessageType.Subscribe,
            title=(f"{context.mediainfo.title_year} {context.metainfo.season} 添加订阅失败！"),
            text=err_msg,
            image=context.mediainfo.get_message_image(),
            userid=context.userid,
        )

    def _SubscribeChain__build_completion_notification(
        self,
        subscribe: SubscriptionSnapshot,
        mediainfo: MediaInfo,
        meta: MetaBase,
    ) -> _SchemaMessage:
        """构造订阅完成通知，保持模板、链接与接收人字段语义。"""
        msgstr = "订阅" if not subscribe.best_version else "洗版"
        if mediainfo.type == MediaType.TV:
            link = self.runtime_config.television_subscribe_url
        elif mediainfo.type == MediaType.MUSIC:
            link = self.runtime_config.music_subscribe_url
        else:
            link = self.runtime_config.movie_subscribe_url

        _completion_message = _SchemaMessage(
            mtype=MessageType.Subscribe,
            ctype=ContentType.SubscribeComplete,
            image=mediainfo.get_message_image(),
            link=link,
            username=subscribe.username,
        )
        _completion_message = (
            MessageTemplateHelper.render(
                _completion_message,
                meta=meta,
                mediainfo=mediainfo,
                msgstr=msgstr,
                username=subscribe.username,
            )
            or _completion_message
        )
        return _completion_message

    @staticmethod
    def _SubscribeChain__report_completed(payload: Mapping[str, JsonData]) -> bool:
        """通过装配端口投递订阅完成统计。"""
        return _subscription_share_snapshot().report_completed(payload)
