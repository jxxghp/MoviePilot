"""音乐关键词、资源匹配与搜索编排 owner。"""

import asyncio
import random
import re
import time
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Optional, cast
from unicodedata import normalize

from app.application.configuration import (
    get_configured_system_config,
)
from app.application.torrent.download import TorrentHelper
from app.chain.search.contract import _SearchOwnerBase as _SearchOwnerBase
from app.domain.context import Context, MusicInfo, TorrentInfo
from app.domain.meta.metamusic import MetaMusic
from app.foundation.text import convert as zhconv_convert
from app.runtime.execution import run_in_threadpool
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MediaType,
    SystemConfigKey,
)


class SearchMusicOwner(_SearchOwnerBase):
    """音乐关键词、资源匹配与搜索编排 owner。"""

    @classmethod
    def music_site_keywords(cls, music: MetaMusic | MusicInfo) -> list[str]:
        """按实体生成站点关键词，繁体字段优先使用简体写法扩大召回。"""
        artists = music.artists or []
        artist = artists[0] if artists else music.album_artist
        values: list[Optional[str]] = []
        if getattr(music, "music_type", None) == MUSIC_ENTITY_ALBUM:
            album = music.album or music.title
            values.extend([album, f"{artist} {album}" if artist and album else None])
        else:
            values.extend(
                [
                    music.title,
                    f"{artist} {music.title}" if artist and music.title else None,
                ]
            )
        search_values: list[Optional[str]] = []
        for value in values:
            search_values.extend(
                [
                    zhconv_convert(value, "zh-hans") if value else None,
                    value,
                ]
            )
        return cls._unique_music_texts(search_values)

    @classmethod
    def matches_music_resource(
        cls,
        music: MusicInfo,
        resource_title: str,
        resource_description: Optional[str] = None,
    ) -> bool:
        """校验标题与副标题同时命中目标专辑/曲名和艺术家。"""
        normalized_resource = cls._normalize_music_match_text(f"{resource_title or ''} {resource_description or ''}")
        if not normalized_resource:
            return False
        if music.music_type == MUSIC_ENTITY_ALBUM:
            candidates = cls._unique_music_texts(
                [
                    music.album or music.title,
                    *(music.names or []),
                ]
            )
        else:
            candidates = cls._unique_music_texts([music.title])
        normalized_candidates = [cls._normalize_music_match_text(candidate) for candidate in candidates]
        if not any(candidate in normalized_resource for candidate in normalized_candidates if candidate):
            return False
        artists = cls._unique_music_texts(
            [
                music.artist,
                music.album_artist,
                *(music.artists or []),
            ]
        )
        normalized_artists = [cls._normalize_music_match_text(artist) for artist in artists]
        return bool(normalized_artists) and any(
            artist in normalized_resource for artist in normalized_artists if artist
        )

    @staticmethod
    def _normalize_music_match_text(value: Optional[str]) -> str:
        """去除音乐名称干扰字符并转换为简体小写文本。"""
        compact_text = "".join(char for char in normalize("NFKC", str(value or "")).casefold() if char.isalnum())
        return str(zhconv_convert(compact_text, "zh-hans"))

    @staticmethod
    def _unique_music_texts(values: Iterable[Optional[str]]) -> list[str]:
        """按清理后的文本去重，并保留站点搜索词原始顺序。"""
        results: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = re.sub(r"\s+", " ", str(value or "")).strip()
            identity = normalized.casefold()
            if not normalized or identity in seen:
                continue
            seen.add(identity)
            results.append(normalized)
        return results

    def _build_music_contexts(
        self,
        torrents: List[TorrentInfo],
        mediainfo: MusicInfo,
        rule_groups: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None,
    ) -> List[Context]:
        """过滤音乐分类资源并组装携带目标音乐身份的下载上下文。"""
        torrents = self._matching_music_torrents(torrents, mediainfo)
        if filter_params:
            torrents = [torrent for torrent in torrents if TorrentHelper.filter_torrent(torrent, filter_params)]
        if rule_groups is None:
            rule_groups = get_configured_system_config().get(SystemConfigKey.SearchFilterRuleGroups) or []
        if rule_groups and torrents:
            filter_torrents = cast(
                Callable[..., List[TorrentInfo]],
                self.filter_torrents,
            )
            torrents = (
                filter_torrents(
                    rule_groups=rule_groups,
                    torrent_list=torrents,
                    mediainfo=mediainfo,
                )
                or []
            )

        contexts: List[Context] = []
        for torrent in torrents:
            meta = MetaMusic.from_music_info(mediainfo)
            meta.org_string = torrent.title
            meta.apply_audio_quality(f"{torrent.title} {torrent.description or ''}", overwrite=True)
            contexts.append(
                Context(
                    torrent_info=torrent,
                    media_info=mediainfo,
                    meta_info=meta,
                    resource_source="search",
                    match_source=str(mediainfo.media_source or "title"),
                    candidate_recognized=False,
                    media_info_is_target=True,
                )
            )
        return cast(
            List[Context],
            self._remove_duplicate(TorrentHelper.sort_torrents(contexts)),
        )

    @staticmethod
    def _matching_music_torrents(
        torrents: Optional[List[TorrentInfo]],
        mediainfo: MusicInfo,
    ) -> List[TorrentInfo]:
        """筛出音乐分类且标题、副标题匹配目标名称与艺术家的站点资源。"""
        return [
            torrent
            for torrent in torrents or []
            if torrent.category in (MediaType.MUSIC, MediaType.MUSIC.value)
            and SearchMusicOwner.matches_music_resource(
                mediainfo,
                torrent.title,
                torrent.description,
            )
        ]

    def _process_music(
        self,
        mediainfo: MusicInfo,
        keyword: Optional[str] = None,
        sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None,
    ) -> List[Context]:
        """按音乐元数据生成站点关键词并执行同步资源搜索。"""
        keywords = [keyword] if keyword else type(self).music_site_keywords(mediainfo)
        torrents: List[TorrentInfo] = []
        for index, search_word in enumerate(keywords or [mediainfo.title]):
            if index:
                time.sleep(random.randint(1, 10))
            matched_torrents = self._matching_music_torrents(
                self._SearchChain__search_all_sites(
                    keyword=search_word,
                    mediainfo=mediainfo,
                    sites=sites,
                    mtype=MediaType.MUSIC,
                ),
                mediainfo,
            )
            torrents.extend(matched_torrents)
            if matched_torrents and not self.runtime_config.search_multiple_name:
                break
        return self._build_music_contexts(
            torrents=torrents,
            mediainfo=mediainfo,
            rule_groups=rule_groups,
            filter_params=filter_params,
        )

    async def _async_process_music(
        self,
        mediainfo: MusicInfo,
        keyword: Optional[str] = None,
        sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None,
    ) -> List[Context]:
        """按音乐元数据生成站点关键词并执行异步资源搜索。"""
        keywords = [keyword] if keyword else type(self).music_site_keywords(mediainfo)
        torrents: List[TorrentInfo] = []
        for index, search_word in enumerate(keywords or [mediainfo.title]):
            if index:
                await asyncio.sleep(random.randint(1, 10))
            matched_torrents = self._matching_music_torrents(
                await self._SearchChain__async_search_all_sites(
                    keyword=search_word,
                    mediainfo=mediainfo,
                    sites=sites,
                    mtype=MediaType.MUSIC,
                ),
                mediainfo,
            )
            torrents.extend(matched_torrents)
            if matched_torrents and not self.runtime_config.search_multiple_name:
                break
        return cast(
            List[Context],
            await run_in_threadpool(
                self._build_music_contexts,
                torrents=torrents,
                mediainfo=mediainfo,
                rule_groups=rule_groups,
                filter_params=filter_params,
            ),
        )

    async def _async_process_music_stream(
        self,
        mediainfo: MusicInfo,
        keyword: Optional[str] = None,
        sites: Optional[List[int]] = None,
        rule_groups: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        按音乐元数据渐进式搜索资源，逐站点输出进度并在结束时返回过滤后的完整结果。

        音乐候选需要同时匹配名称、艺术家和音乐分类，因此站点批次只负责推进搜索进度，
        最终结果仍统一交给音乐上下文构造逻辑过滤、排序和去重。
        """
        keywords = [keyword] if keyword else type(self).music_site_keywords(mediainfo)
        torrents: List[TorrentInfo] = []
        for index, search_word in enumerate(keywords or [mediainfo.title]):
            if index:
                await asyncio.sleep(random.randint(1, 10))
            keyword_matched = False
            async for event in self._SearchChain__async_search_all_sites_stream(
                keyword=search_word, mediainfo=mediainfo, sites=sites, mtype=MediaType.MUSIC
            ):
                result = event.pop("items", []) or []
                matched_torrents = self._matching_music_torrents(result, mediainfo)
                if matched_torrents:
                    keyword_matched = True
                    torrents.extend(matched_torrents)
                yield {
                    **event,
                    "type": "append",
                    "items": [],
                    "total_items": len(torrents),
                }
            if keyword_matched and not self.runtime_config.search_multiple_name:
                break

        contexts = await run_in_threadpool(
            self._build_music_contexts,
            torrents=torrents,
            mediainfo=mediainfo,
            rule_groups=rule_groups,
            filter_params=filter_params,
        )
        items = [context.to_dict() for context in contexts]
        yield {
            "type": "replace",
            "stage": "filtered",
            "value": 100,
            "text": f"过滤匹配完成，共 {len(contexts)} 个资源",
            "items": items,
            "total_items": len(contexts),
            "candidate_items": len(torrents),
        }
        yield {
            "type": "done",
            "stage": "done",
            "text": f"搜索完成，共 {len(contexts)} 个资源",
            "items": items,
            "total_items": len(contexts),
            "candidate_items": len(torrents),
            "contexts": contexts,
        }
