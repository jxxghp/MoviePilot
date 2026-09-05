"""音乐关键词、资源匹配与搜索编排 owner。"""

import copy
from collections import Counter
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

from app.chain.search.contract import _SearchOwnerBase
from app.domain.context import Context, MusicInfo, TorrentInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import (
    match_music_resource,
    music_artists,
    music_base_title,
    music_text_key,
    music_titles,
    unique_music_texts,
)
from app.foundation.text import convert as zhconv_convert
from app.schemas.types import MUSIC_ENTITY_ALBUM

_MAX_MUSIC_KEYWORDS = 12


class SearchMusicOwner(_SearchOwnerBase):
    """音乐搜索共用严格身份规则，人工候选通过显式参数单独开放。"""

    @classmethod
    def music_site_keywords(cls, music: MetaMusic | MusicInfo) -> list[str]:
        """依次查询主名称、艺术家组合和可信别名，保留原文且不单搜艺术家。"""
        info = MusicInfo.from_meta(music) if isinstance(music, MetaMusic) else music
        artists = music_artists(info)
        artist = info.artists[0] if info.artists else info.album_artist
        titles = music_titles(info)
        values: list[Optional[str]] = list(titles)
        for title in titles:
            values.append(f"{artist} {title}" if artist else None)
        for title in titles:
            base = music_base_title(title)
            if base != title:
                values.extend([base, f"{artist} {base}" if artist else None])
        if titles:
            values.extend(f"{alias} {titles[0]}" for alias in artists if alias != artist)
        search_values: list[Optional[str]] = []
        for value in values:
            search_values.extend([zhconv_convert(value, "zh-hans") if value else None, value])
        return cls._unique_music_texts(search_values)[:_MAX_MUSIC_KEYWORDS]

    @classmethod
    def matches_music_resource(
        cls, music: MusicInfo, resource_title: str, resource_description: Optional[str] = None,
    ) -> bool:
        """只返回可用于自动订阅的精确身份命中，不放行待确认或关联专辑。"""
        return match_music_resource(music, resource_title, resource_description).status == "exact"

    @staticmethod
    def _normalize_music_match_text(value: Optional[str]) -> str:
        """保留公开门面的名称归一化契约。"""
        return music_text_key(value)

    @staticmethod
    def _unique_music_texts(values: Iterable[Optional[str]]) -> list[str]:
        """保留公开门面的有序关键词去重契约。"""
        return unique_music_texts(values)

    @staticmethod
    def _music_keywords(mediainfo: MusicInfo, keyword: Optional[str], include_candidates: bool) -> list[str]:
        """人工单曲搜索在严格关键词之后追加所属专辑，仍由匹配结果区分实体。"""
        if keyword:
            return [keyword]
        keywords = SearchMusicOwner.music_site_keywords(mediainfo)
        if include_candidates and mediainfo.music_type != MUSIC_ENTITY_ALBUM and mediainfo.album:
            album = copy.copy(mediainfo)
            album.music_type = MUSIC_ENTITY_ALBUM
            album.title = mediainfo.album
            album.title_aliases = list(mediainfo.album_aliases or [])
            album.names = []
            keywords.extend(SearchMusicOwner.music_site_keywords(album))
        return unique_music_texts(keywords)

    def _build_music_contexts(
        self, torrents: List[TorrentInfo], mediainfo: MusicInfo,
        rule_groups: Optional[List[str]] = None, filter_params: Optional[Dict[str, str]] = None,
        include_candidates: bool = False, diagnostics: Optional[Counter[str]] = None,
    ) -> List[Context]:
        """保留音乐兼容入口，全部结果交给通用资源过滤、匹配和上下文构造流程。"""
        return self._parse_result(
            torrents=torrents, mediainfo=mediainfo, rule_groups=rule_groups,
            filter_params=filter_params, include_candidates=include_candidates, diagnostics=diagnostics,
        )

    @staticmethod
    def _matching_music_torrents(torrents: Optional[List[TorrentInfo]], mediainfo: MusicInfo) -> List[TorrentInfo]:
        """保留严格音乐候选筛选入口；底层匹配消费资源解析结果。"""
        return [torrent for torrent in torrents or [] if match_music_resource(
            mediainfo, torrent.title, torrent.description, torrent.category,
        ).status == "exact"]

    def _process_music(
        self, mediainfo: MusicInfo, keyword: Optional[str] = None, sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None, filter_params: Optional[Dict[str, str]] = None,
        include_candidates: bool = False,
    ) -> List[Context]:
        """兼容同步音乐入口，不再拥有独立站点查询和停止循环。"""
        return self.process(
            mediainfo=mediainfo, keyword=keyword, sites=sites, rule_groups=rule_groups,
            filter_params=filter_params, include_candidates=include_candidates,
        )

    async def _async_process_music(
        self, mediainfo: MusicInfo, keyword: Optional[str] = None, sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None, filter_params: Optional[Dict[str, str]] = None,
        include_candidates: bool = False,
    ) -> List[Context]:
        """兼容异步音乐入口，复用所有媒体共用的异步搜索状态机。"""
        return await self.async_process(
            mediainfo=mediainfo, keyword=keyword, sites=sites, rule_groups=rule_groups,
            filter_params=filter_params, include_candidates=include_candidates,
        )

    async def _async_process_music_stream(
        self, mediainfo: MusicInfo, keyword: Optional[str] = None, sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None, filter_params: Optional[Dict[str, str]] = None,
        include_candidates: bool = False,
    ) -> AsyncIterator[Dict[str, Any]]:
        """兼容音乐 SSE 入口，复用相同的候选预览、过滤、统计及完成事件。"""
        async for event in self.async_process_stream(
            mediainfo=mediainfo, keyword=keyword, sites=sites, rule_groups=rule_groups,
            filter_params=filter_params, include_candidates=include_candidates,
        ):
            yield event
