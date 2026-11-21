"""
以共同的 TMDB 身份确认跨来源订阅，保留类型、季、剧集组和用户边界。

只读取快照和注入的身份转换能力，不修改旧订阅，也不把标题相似视为身份等价。
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any, Optional

from app.application.subscription.contract import SubscriptionIdentity, SubscriptionSnapshot
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaSource, MediaType

Projection = Callable[..., Optional[dict[str, Any]]]
AsyncProjection = Callable[..., Awaitable[Optional[dict[str, Any]]]]


def subscription_identity(record: SubscriptionSnapshot) -> SubscriptionIdentity:
    """
    从已持久化的订阅构造包含类型及季集边界的完整身份。

    :param record: 已脱离数据库会话的订阅快照
    :return: 保留来源、类型、季、剧集组及音乐实体的身份
    :raises ValueError: 快照缺少成对的来源和媒体标识
    """
    source, media_id = resolve_media_identity(media=record)
    if source is None or media_id is None:
        raise ValueError("订阅缺少完整媒体身份")
    return SubscriptionIdentity(
        media_source=source,
        media_id=media_id,
        type=record.type,
        season=record.season,
        episode_group=record.episode_group,
        music_type=record.music_type,
    )


class SubscriptionIdentityLookup:
    """
    在一次操作内缓存身份投影，避免同作品多季或批量状态查询重复转换。
    """

    def __init__(self, project: Projection, async_project: AsyncProjection) -> None:
        """
        注入同步、异步转换入口；缓存仅属于本实例，不跨请求保留失败结果。

        :param project: 接收来源、媒体标识、类型和季号的同步媒体转换入口
        :param async_project: 与同步入口保持相同输入和结果语义的异步转换入口
        """
        self._project = project
        self._async_project = async_project
        self._cache: dict[SubscriptionIdentity, Optional[SubscriptionIdentity]] = {}

    @staticmethod
    def _candidates(
        identity: SubscriptionIdentity,
        records: Sequence[SubscriptionSnapshot],
        username: Optional[str],
    ) -> list[SubscriptionSnapshot]:
        """
        在外部查询前限定用户、影视类型及剧集组；空用户名保留跳过查重的兼容语义。
        """
        if username == "" or identity.type not in (MediaType.MOVIE.value, MediaType.TV.value):
            return []
        return [record for record in records
                if record.type == identity.type
                and record.episode_group == identity.episode_group
                and (identity.season is None or record.season == identity.season)
                and (username is None or record.username == username)
                and record.media_source and record.media_id]

    @staticmethod
    def _converted(
        identity: SubscriptionIdentity,
        payload: Optional[dict[str, Any]],
    ) -> Optional[SubscriptionIdentity]:
        """
        只接纳有效 TMDB 标识；显式季优先，未指定季时采用来源投影确定的季号。
        """
        source, media_id = resolve_media_identity(
            media_source=MediaSource.TMDB,
            media_id=str(payload.get("id") or "") if payload else None,
        )
        if source is None or media_id is None:
            return None
        projected_season = payload.get("season") if payload else None
        season = identity.season
        if season is None and isinstance(projected_season, int):
            season = projected_season
        return replace(identity, media_source=source, media_id=media_id, season=season)

    def _resolve(self, identity: SubscriptionIdentity) -> Optional[SubscriptionIdentity]:
        """
        同步取得共同身份，转换故障只影响跨源确认，不阻断原来源订阅。
        """
        if identity.media_source == MediaSource.TMDB:
            return identity
        if identity not in self._cache:
            try:
                payload = self._project(
                    target_source=MediaSource.TMDB, media_source=identity.media_source,
                    media_id=identity.media_id, mtype=MediaType(identity.type), season=identity.season,
                )
                self._cache[identity] = self._converted(identity, payload)
            except Exception:
                logger.warning(f"订阅跨源身份查询失败，沿用原身份：{identity.media_source}:{identity.media_id}")
                self._cache[identity] = None
        return self._cache[identity]

    async def _async_resolve(self, identity: SubscriptionIdentity) -> Optional[SubscriptionIdentity]:
        """
        异步取得共同身份，与同步入口共享转换结果和失败回退规则。
        """
        if identity.media_source == MediaSource.TMDB:
            return identity
        if identity not in self._cache:
            try:
                payload = await self._async_project(
                    target_source=MediaSource.TMDB, media_source=identity.media_source,
                    media_id=identity.media_id, mtype=MediaType(identity.type), season=identity.season,
                )
                self._cache[identity] = self._converted(identity, payload)
            except Exception:
                logger.warning(f"订阅跨源身份查询失败，沿用原身份：{identity.media_source}:{identity.media_id}")
                self._cache[identity] = None
        return self._cache[identity]

    @staticmethod
    def _matches(target: SubscriptionIdentity, candidate: Optional[SubscriptionIdentity]) -> bool:
        """
        在共同媒体身份下比较季，未指定目标季时允许返回该作品的各季订阅。
        """
        return bool(candidate and target.media_id == candidate.media_id
                    and (target.season is None or target.season == candidate.season))

    def find(
        self, identity: SubscriptionIdentity, records: Sequence[SubscriptionSnapshot],
        username: Optional[str] = None,
    ) -> list[SubscriptionSnapshot]:
        """
        同步查找其它来源中身份已确认等价的订阅，不凭名称或数字 ID 跨源查重。

        :param identity: 创建或查询使用的完整影视身份
        :param records: 从持久化端口读取的候选订阅快照
        :param username: 指定时仅匹配该用户；None 不限制，空字符串显式跳过查重
        :return: 共同 TMDB 身份及季集边界匹配的订阅，保持输入顺序
        """
        candidates = [record for record in self._candidates(identity, records, username)
                      if record.media_source != identity.media_source]
        if not candidates:
            return []
        target = self._resolve(identity)
        if target is None:
            return []
        return [record for record in candidates
                if self._matches(target, self._resolve(subscription_identity(record)))]

    async def async_find(
        self, identity: SubscriptionIdentity, records: Sequence[SubscriptionSnapshot],
        username: Optional[str] = None,
    ) -> list[SubscriptionSnapshot]:
        """
        异步确认跨源订阅；顺序执行转换，避免并发复用请求数据库会话。

        :param identity: 创建或查询使用的完整影视身份
        :param records: 从持久化端口读取的候选订阅快照
        :param username: 指定时仅匹配该用户；None 不限制，空字符串显式跳过查重
        :return: 已确认等价的订阅；转换失败的条目不参与匹配
        """
        candidates = [record for record in self._candidates(identity, records, username)
                      if record.media_source != identity.media_source]
        if not candidates:
            return []
        target = await self._async_resolve(identity)
        if target is None:
            return []
        return [record for record in candidates
                if self._matches(target, await self._async_resolve(subscription_identity(record)))]
