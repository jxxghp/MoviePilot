"""多来源音乐目录搜索应用服务。"""

import asyncio
from typing import Any, Callable, Iterable, Optional

from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.media import normalize_media_source
from app.schemas.types import MediaSource, MediaSourceSelection


class MusicCatalogService:
    """编排音乐来源选择、容错搜索和候选归一化。"""

    def __init__(
        self,
        source_resolver: Callable[[MediaSource], Any],
        warning: Callable[[str], None],
        primary_source: MediaSource = MediaSource.MusicBrainz,
    ) -> None:
        """注入来源解析器、告警输出和默认音乐来源。"""
        self._source_resolver = source_resolver
        self._warning = warning
        self._primary_source = primary_source

    def search_sources(
        self,
        media_source: Optional[MediaSourceSelection],
    ) -> list[MediaSource]:
        """解析有序音乐来源，保留合法插件扩展来源并去重。"""
        if not media_source:
            return [self._primary_source]
        raw_sources = (
            (media_source,)
            if isinstance(media_source, MediaSource)
            else media_source
        )
        sources = []
        for raw_source in raw_sources:
            source = normalize_media_source(raw_source)
            if source and source not in sources:
                sources.append(source)
        return sources

    @staticmethod
    def normalize_candidates(
        candidates: Optional[Iterable[MusicInfo | dict[str, Any]]],
        limit: Optional[int] = None,
    ) -> list[MusicInfo]:
        """标准化并按来源身份或元数据去重音乐候选。"""
        results = []
        identities = set()
        for candidate in candidates or []:
            info = candidate if isinstance(candidate, MusicInfo) else MusicInfo.from_dict(candidate)
            if info.media_source and info.media_id:
                identity = (
                    "id",
                    str(info.media_source).casefold(),
                    str(info.music_type).casefold(),
                    str(info.media_id).casefold(),
                )
            else:
                identity = (
                    "metadata",
                    str(info.music_type).casefold(),
                    MetaMusic.compact_text(info.title),
                    MetaMusic.compact_text(info.artist),
                    MetaMusic.compact_text(info.album),
                )
            if identity in identities:
                continue
            identities.add(identity)
            results.append(info)
            if limit and len(results) >= limit:
                break
        return results

    def search(
        self,
        query: str,
        limit: int = 20,
        media_source: Optional[MediaSourceSelection] = None,
    ) -> list[MusicInfo]:
        """顺序搜索一个或多个音乐来源，隔离单一来源失败。"""
        meta = MetaMusic.parse_query(query)
        candidates = []
        for source in self.search_sources(media_source):
            chain = self._source_resolver(source)
            if not chain:
                continue
            try:
                candidates.extend(chain.search_music(meta, limit=limit))
            except Exception as error:
                self._warning(f"音乐来源 {source} 搜索失败：{str(error)}")
        return self.normalize_candidates(candidates, limit=limit)

    async def async_search(
        self,
        query: str,
        limit: int = 20,
        media_source: Optional[MediaSourceSelection] = None,
    ) -> list[MusicInfo]:
        """并行搜索一个或多个音乐来源，隔离单一来源失败。"""
        meta = MetaMusic.parse_query(query)
        searches = []
        for source in self.search_sources(media_source):
            chain = self._source_resolver(source)
            if chain:
                searches.append(self._async_search_source(chain, source, meta, limit))
        source_results = await asyncio.gather(*searches) if searches else []
        return self.normalize_candidates(
            [candidate for results in source_results for candidate in results],
            limit=limit,
        )

    async def _async_search_source(
        self,
        chain: Any,
        source: MediaSource,
        meta: MetaMusic,
        limit: int,
    ) -> list[MusicInfo]:
        """异步搜索单个来源，并把异常降级为空候选。"""
        try:
            return await chain.async_search_music(meta, limit=limit)
        except Exception as error:
            self._warning(f"音乐来源 {source} 搜索失败：{str(error)}")
            return []
