import re
from typing import Any, Iterable, Optional

from app.chain import ChainBase
from app.core.music import MusicInfo, MusicMeta


class MusicChain(ChainBase):
    """音乐元数据搜索、识别与站点搜索参数编排链。"""

    _artist_title_pattern = re.compile(r"^\s*(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)\s*$")
    _spaces_pattern = re.compile(r"\s+")

    @classmethod
    def parse_query(cls, query: str) -> MusicMeta:
        """将用户输入解析为最小可用的音乐搜索元数据。"""
        normalized = cls._normalize_text(query)
        meta = MusicMeta(org_string=query, title=normalized)
        match = cls._artist_title_pattern.match(normalized)
        if match:
            meta.artists = [match.group("artist").strip()]
            meta.title = match.group("title").strip()
        return meta

    @classmethod
    def build_site_keywords(cls, music: MusicMeta | MusicInfo) -> list[str]:
        """根据音乐元数据生成按精确度递减的站点搜索关键词。"""
        artists = music.artists or []
        artist = artists[0] if artists else music.album_artist
        keywords = []
        if artist and music.album:
            keywords.append(f"{artist} {music.album}")
        if artist and music.title:
            keywords.append(f"{artist} {music.title}")
        if music.album:
            keywords.append(music.album)
        if music.title:
            keywords.append(music.title)
        return cls._unique_texts(keywords)

    @classmethod
    def normalize_candidates(
            cls,
            candidates: Optional[Iterable[MusicInfo | dict[str, Any]]],
            limit: Optional[int] = None,
    ) -> list[MusicInfo]:
        """标准化并去重来自一个或多个音乐元数据模块的候选。"""
        results: list[MusicInfo] = []
        identities: set[tuple[str, ...]] = set()
        for candidate in candidates or []:
            info = candidate if isinstance(candidate, MusicInfo) else MusicInfo.from_dict(candidate)
            identity = cls._candidate_identity(info)
            if identity in identities:
                continue
            identities.add(identity)
            results.append(info)
            if limit and len(results) >= limit:
                break
        return results

    def search(self, query: str, limit: int = 20) -> list[MusicInfo]:
        """调用已启用的音乐元数据模块搜索候选。"""
        meta = self.parse_query(query)
        candidates = self.run_module("search_music", meta=meta, limit=limit)
        return self.normalize_candidates(candidates, limit=limit)

    async def async_search(self, query: str, limit: int = 20) -> list[MusicInfo]:
        """异步调用已启用的音乐元数据模块搜索候选。"""
        meta = self.parse_query(query)
        candidates = await self.async_run_module("search_music", meta=meta, limit=limit)
        return self.normalize_candidates(candidates, limit=limit)

    def recognize(
            self,
            source: str,
            media_id: str,
    ) -> Optional[MusicInfo]:
        """按音乐元数据源和媒体 ID 获取标准化详情。"""
        result = self.run_module(
            "recognize_music",
            source=source,
            media_id=media_id,
        )
        if isinstance(result, MusicInfo):
            return result
        if isinstance(result, dict):
            return MusicInfo.from_dict(result)
        return None

    async def async_recognize(
            self,
            source: str,
            media_id: str,
    ) -> Optional[MusicInfo]:
        """异步按音乐元数据源和媒体 ID 获取标准化详情。"""
        result = await self.async_run_module(
            "recognize_music",
            source=source,
            media_id=media_id,
        )
        if isinstance(result, MusicInfo):
            return result
        if isinstance(result, dict):
            return MusicInfo.from_dict(result)
        return None

    def chart(self, range_name: str, page: int = 1, count: int = 30) -> list[MusicInfo]:
        """读取 ListenBrainz 全站音乐榜单并标准化分页结果。"""
        candidates = self.run_module(
            "music_chart",
            range_name=range_name,
            offset=max(page - 1, 0) * count,
            count=count,
        )
        return self.normalize_candidates(candidates, limit=count)

    async def async_chart(
            self,
            range_name: str,
            page: int = 1,
            count: int = 30,
    ) -> list[MusicInfo]:
        """异步读取 ListenBrainz 全站音乐榜单并标准化分页结果。"""
        candidates = await self.async_run_module(
            "music_chart",
            range_name=range_name,
            offset=max(page - 1, 0) * count,
            count=count,
        )
        return self.normalize_candidates(candidates, limit=count)

    @classmethod
    def to_meta(cls, info: MusicInfo) -> MusicMeta:
        """将用户选中的标准音乐信息转换为下载和整理上下文元数据。"""
        return MusicMeta(
            title=info.title,
            artists=list(info.artists),
            album=info.album,
            album_artist=info.album_artist,
            year=info.year,
            disc_number=info.disc_number,
            track_number=info.track_number,
            total_tracks=info.total_tracks,
            version=info.version,
            duration=info.duration,
            isrc=info.isrc,
            media_source=info.source,
            media_id=info.media_id,
        )

    @classmethod
    def _candidate_identity(cls, info: MusicInfo) -> tuple[str, ...]:
        """构造跨来源稳定的候选去重键。"""
        if info.source and info.media_id:
            return "id", info.source.casefold(), info.media_id.casefold()
        return (
            "metadata",
            cls._normalize_text(info.title).casefold(),
            cls._normalize_text(info.artist).casefold(),
            cls._normalize_text(info.album).casefold(),
        )

    @classmethod
    def _unique_texts(cls, values: Iterable[Optional[str]]) -> list[str]:
        """按规范化文本去重并保留原始顺序。"""
        results = []
        seen = set()
        for value in values:
            normalized = cls._normalize_text(value)
            identity = normalized.casefold()
            if not normalized or identity in seen:
                continue
            seen.add(identity)
            results.append(normalized)
        return results

    @classmethod
    def _normalize_text(cls, value: Optional[str]) -> str:
        """清理音乐检索文本中的多余空白。"""
        return cls._spaces_pattern.sub(" ", str(value or "")).strip()
