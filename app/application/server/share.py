"""中心服务订阅和工作流分享用例。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from app.schemas.media import resolve_media_identity


class ServerSharingService:
    """协调本地订阅、工作流读取与中心服务分享传输。"""

    SUBSCRIBE_FIELDS = frozenset({
        "share_title", "share_comment", "share_user", "share_uid", "name",
        "year", "type", "keyword", "media_source", "media_id", "music_type",
        "total_tracks", "season", "poster", "backdrop", "vote", "description",
        "genre_ids", "include", "exclude", "quality", "resolution", "effect",
        "total_episode", "custom_words", "media_category", "episode_group",
        "date",
    })

    def __init__(
            self,
            *,
            subscribe_provider: Callable[[int], Any],
            async_subscribe_provider: Callable[[int], Awaitable[Any]],
            workflow_provider: Callable[[int], Any],
            async_workflow_provider: Callable[[int], Awaitable[Any]],
            user_uuid_provider: Callable[[], str],
            subscribe_sender: Callable[[dict], Any],
            async_subscribe_sender: Callable[[dict], Awaitable[Any]],
            workflow_sender: Callable[[dict], Any],
            async_workflow_sender: Callable[[dict], Awaitable[Any]],
            response_handler: Callable[[Any, Callable[[], None]], tuple[bool, str]],
            subscribe_cache_clearer: Callable[[], None],
            workflow_cache_clearer: Callable[[], None],
    ) -> None:
        """保存本地数据端口、中心服务传输端口和缓存失效端口。"""
        self._subscribe_provider = subscribe_provider
        self._async_subscribe_provider = async_subscribe_provider
        self._workflow_provider = workflow_provider
        self._async_workflow_provider = async_workflow_provider
        self._user_uuid_provider = user_uuid_provider
        self._subscribe_sender = subscribe_sender
        self._async_subscribe_sender = async_subscribe_sender
        self._workflow_sender = workflow_sender
        self._async_workflow_sender = async_workflow_sender
        self._response_handler = response_handler
        self._subscribe_cache_clearer = subscribe_cache_clearer
        self._workflow_cache_clearer = workflow_cache_clearer

    def build_subscribe_payload(self, item: Optional[dict]) -> Optional[dict]:
        """构造订阅分享载荷并隔离本地字段和旧专用 ID。"""
        if not isinstance(item, dict):
            return None
        media_source, media_id = resolve_media_identity(media=item)
        if not media_source or not media_id:
            return None
        payload = {
            key: value
            for key, value in item.items()
            if key in self.SUBSCRIBE_FIELDS
        }
        payload["media_source"] = str(media_source)
        payload["media_id"] = media_id
        return payload

    @staticmethod
    def prepare_workflow(workflow: Any) -> dict:
        """移除本地字段并把动作和流程编码为中心服务兼容格式。"""
        workflow_dict = workflow.to_dict()
        workflow_dict.pop("id", None)
        workflow_dict.pop("context", None)
        workflow_dict["actions"] = json.dumps(workflow_dict["actions"] or [])
        workflow_dict["flows"] = json.dumps(workflow_dict["flows"] or [])
        return workflow_dict

    @staticmethod
    def validate_workflow(workflow: Any) -> tuple[bool, str]:
        """验证工作流存在且同时包含动作与流程。"""
        if not workflow:
            return False, "工作流不存在"
        if not workflow.actions or not workflow.flows:
            return False, "请分享有动作和流程的工作流"
        return True, ""

    def share_subscribe(
            self,
            *,
            enabled: bool,
            subscribe_id: int,
            share_title: str,
            share_comment: str,
            share_user: str,
    ) -> tuple[bool, str]:
        """同步读取并分享指定订阅。"""
        if not enabled:
            return False, "当前没有开启订阅数据共享功能"
        subscribe = self._subscribe_provider(subscribe_id)
        if not subscribe:
            return False, "订阅不存在"
        payload = self.build_subscribe_payload({
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": self._user_uuid_provider(),
            **subscribe.to_dict(),
        })
        if not payload:
            return False, "订阅媒体身份不完整"
        return self._response_handler(
            self._subscribe_sender(payload),
            self._subscribe_cache_clearer,
        )

    async def async_share_subscribe(
            self,
            *,
            enabled: bool,
            subscribe_id: int,
            share_title: str,
            share_comment: str,
            share_user: str,
    ) -> tuple[bool, str]:
        """异步读取并分享指定订阅。"""
        if not enabled:
            return False, "当前没有开启订阅数据共享功能"
        subscribe = await self._async_subscribe_provider(subscribe_id)
        if not subscribe:
            return False, "订阅不存在"
        payload = self.build_subscribe_payload({
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": self._user_uuid_provider(),
            **subscribe.to_dict(),
        })
        if not payload:
            return False, "订阅媒体身份不完整"
        return self._response_handler(
            await self._async_subscribe_sender(payload),
            self._subscribe_cache_clearer,
        )

    def share_workflow(
            self,
            *,
            enabled: bool,
            workflow_id: int,
            share_title: str,
            share_comment: str,
            share_user: str,
    ) -> tuple[bool, str]:
        """同步读取并分享指定工作流。"""
        if not enabled:
            return False, "当前没有开启工作流数据共享功能"
        workflow = self._workflow_provider(workflow_id)
        valid, message = self.validate_workflow(workflow)
        if not valid:
            return False, message
        payload = {
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": self._user_uuid_provider(),
            **self.prepare_workflow(workflow),
        }
        return self._response_handler(
            self._workflow_sender(payload),
            self._workflow_cache_clearer,
        )

    async def async_share_workflow(
            self,
            *,
            enabled: bool,
            workflow_id: int,
            share_title: str,
            share_comment: str,
            share_user: str,
    ) -> tuple[bool, str]:
        """异步读取并分享指定工作流。"""
        if not enabled:
            return False, "当前没有开启工作流数据共享功能"
        workflow = await self._async_workflow_provider(workflow_id)
        valid, message = self.validate_workflow(workflow)
        if not valid:
            return False, message
        payload = {
            "share_title": share_title,
            "share_comment": share_comment,
            "share_user": share_user,
            "share_uid": self._user_uuid_provider(),
            **self.prepare_workflow(workflow),
        }
        return self._response_handler(
            await self._async_workflow_sender(payload),
            self._workflow_cache_clearer,
        )
