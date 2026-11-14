"""
复用媒体缓存，将订阅投影为按播出日期查询的日历窗口。
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Optional

from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.runtime.cache import cached
from app.runtime.log import logger
from app.schemas.calendar import (
    CalendarDate,
    CalendarEpisode,
    CalendarSubscription,
    SubscribeCalendarEvent,
    SubscribeCalendarPage,
)
from app.schemas.media import normalize_media_source
from app.schemas.subscribe import Subscribe
from app.schemas.types import MediaSource, MediaType

CALENDAR_DETAIL_CONCURRENCY = 4


class SubscribeCalendarChain:
    """
    有界查询真实播出日期，先筛选日期窗口再分页并返回精简内容。
    """

    @staticmethod
    @cached(maxsize=1024, ttl=300, empty_ttl=60, local_only=True)
    async def _schedule(
        media_source: MediaSource,
        media_id: str,
        mtype: MediaType,
        season: Optional[int],
        episode_group: Optional[str],
    ) -> list[CalendarDate]:
        """
        读取一个媒体身份的播出日期，复用现有预缓存并短期保存精简投影。

        :param media_source: 主媒体来源
        :param media_id: 主来源媒体标识
        :param mtype: 影视媒体类型
        :param season: 订阅季号
        :param episode_group: 可选剧集组
        :return: 按日期聚合的电影上映或电视剧分集摘要
        """
        tmdb_id = int(media_id) if media_source == MediaSource.TMDB and media_id.isdecimal() else None
        if mtype == MediaType.MOVIE or not tmdb_id:
            media = await MediaChain().async_recognize_media(
                media_source=media_source,
                media_id=media_id,
                mtype=mtype,
                episode_group=episode_group,
            )
            if not media:
                return []
            if mtype == MediaType.MOVIE:
                return [CalendarDate(date=media.release_date, runtime=media.runtime)] if media.release_date else []
            tmdb_id = media.tmdb_id
        if not tmdb_id or season is None:
            return []
        episodes = await TmdbChain().async_tmdb_episodes(
            tmdbid=tmdb_id,
            season=season,
            episode_group=episode_group,
        )
        dates: dict[str, CalendarDate] = {}
        for episode in episodes:
            if not episode.air_date:
                continue
            day = dates.setdefault(episode.air_date, CalendarDate(date=episode.air_date))
            day.episodes.append(CalendarEpisode(episode_number=episode.episode_number or 0, name=episode.name))
            if not day.runtime:
                day.runtime = episode.runtime
        return list(dates.values())

    async def window(
        self,
        subscribes: list[Subscribe],
        start: date,
        end: date,
        page: int,
        count: int,
        is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> SubscribeCalendarPage:
        """
        查询左闭右开的真实播出日期窗口，详情最多四路并发，返回日期排序后的当前页。

        :param subscribes: 已按当前用户权限筛选的订阅
        :param start: 包含在窗口内的开始日期
        :param end: 不包含在窗口内的结束日期
        :param page: 从 1 开始的页码
        :param count: 当前页最大事件条目数
        :param is_disconnected: 可选请求断开探测，停止旧窗口继续派发详情查询
        :return: 窗口内的精简事件页，不包含其他月份的分集详情
        """
        pending = iter(subscribes)
        events: list[SubscribeCalendarEvent] = []
        start_key, end_key = start.isoformat(), end.isoformat()

        async def worker() -> None:
            """
            顺序消费剩余订阅，失败时保留其他订阅的可用日历结果。
            """
            for subscribe in pending:
                if is_disconnected and await is_disconnected():
                    return
                source = normalize_media_source(subscribe.media_source)
                if not source or not subscribe.media_id or subscribe.type not in {"电影", "电视剧"}:
                    continue
                try:
                    schedule = await self._schedule(
                        source,
                        subscribe.media_id,
                        MediaType(subscribe.type),
                        subscribe.season,
                        subscribe.episode_group,
                    )
                except Exception as error:
                    logger.warning(f"订阅日历查询失败：{subscribe.name}，{error}")
                    continue
                events.extend(
                    SubscribeCalendarEvent(
                        subscribe=CalendarSubscription.model_validate(subscribe, from_attributes=True),
                        **day.model_dump(),
                    )
                    for day in schedule
                    if start_key <= day.date < end_key
                )

        await asyncio.gather(*(worker() for _ in range(min(CALENDAR_DETAIL_CONCURRENCY, len(subscribes)))))
        events.sort(key=lambda event: (event.date, event.subscribe.name or "", event.subscribe.id or 0))
        offset = (page - 1) * count
        return SubscribeCalendarPage(items=events[offset : offset + count], total=len(events))
