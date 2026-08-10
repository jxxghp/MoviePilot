import re
from pathlib import Path
from typing import Any, Iterable, Optional

from fastapi.concurrency import run_in_threadpool

from app import schemas
from app.chain import ChainBase
from app.core.config import settings
from app.core.context import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
    MusicLyrics,
)
from app.core.meta import MetaMusic
from app.helper.audio import AudioMetadataHelper
from app.log import logger


class MusicChain(ChainBase):
    """音乐元数据搜索、识别与站点搜索参数编排链。"""

    _artist_title_pattern = re.compile(r"^\s*(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)\s*$")
    _spaces_pattern = re.compile(r"\s+")

    @classmethod
    def parse_query(cls, query: str) -> MetaMusic:
        """将用户输入的搜索关键词解析为音乐元数据。"""
        normalized = cls._normalize_text(query)
        meta = MetaMusic(org_string=query, title=normalized)
        meta.apply_audio_quality(normalized)
        match = cls._artist_title_pattern.match(normalized)
        if match:
            meta.artists = [match.group("artist").strip()]
            meta.title = match.group("title").strip()
        return meta

    @classmethod
    def build_site_keywords(cls, music: MetaMusic | MusicInfo) -> list[str]:
        """按单曲或专辑实体生成站点关键词，避免单曲订阅优先搜到所属整专。"""
        artists = music.artists or []
        artist = artists[0] if artists else music.album_artist
        keywords = []
        if getattr(music, "music_type", None) == MUSIC_ENTITY_ALBUM:
            album = music.album or music.title
            if artist and album:
                keywords.append(f"{artist} {album}")
            if album:
                keywords.append(album)
        else:
            if artist and music.title:
                keywords.append(f"{artist} {music.title}")
            if music.title:
                keywords.append(music.title)
        return cls._unique_texts(keywords)

    @classmethod
    def matches_site_resource(cls, music: MusicInfo, resource_title: str) -> bool:
        """判断站点资源标题是否包含订阅目标名称，避免宽泛搜索结果串专辑或串单曲。"""
        normalized_resource = cls._normalize_match_text(resource_title)
        if not normalized_resource:
            return False
        if music.music_type == MUSIC_ENTITY_ALBUM:
            candidates = cls._unique_texts([
                music.album or music.title,
                *(music.names or []),
            ])
        else:
            # Recording 的 names 兼容字段会包含所属专辑名；单曲匹配只能使用曲名，
            # 否则整专资源会被当成单曲下载并在首个任务后误销订阅。
            candidates = cls._unique_texts([music.title])
        title_matches = any(
            normalized_target and normalized_target in normalized_resource
            for normalized_target in (
                cls._normalize_match_text(candidate) for candidate in candidates
            )
        )
        if not title_matches:
            return False
        artists = cls._unique_texts([
            music.artist,
            music.album_artist,
            *(music.artists or []),
        ])
        if not artists:
            return True
        # 同名歌曲和专辑十分常见，已知艺术家时必须同时出现在资源标题中。
        return any(
            normalized_artist and normalized_artist in normalized_resource
            for normalized_artist in (
                cls._normalize_match_text(artist) for artist in artists
            )
        )

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
            sort_by: str = "listen_count.desc",
            min_listen_count: int = 0,
            with_cover: bool = False,
            entity: str = MUSIC_ENTITY_RECORDING,
    ) -> list[MusicInfo]:
        """异步读取 ListenBrainz 热门榜单，并应用音乐探索筛选和排序。"""
        candidates = await self.async_run_module(
            "music_chart",
            range_name=range_name,
            offset=max(page - 1, 0) * count,
            count=count,
            entity=entity,
        )
        results = self._filter_candidates(
            self.normalize_candidates(candidates),
            min_listen_count=min_listen_count,
            with_cover=with_cover,
        )
        results.sort(
            key=lambda info: info.listen_count or 0,
            reverse=sort_by != "listen_count.asc",
        )
        return results[:count]

    async def async_fresh_releases(
            self,
            days: int = 14,
            sort: str = "release_date",
            past: bool = True,
            future: bool = True,
            page: int = 1,
            count: int = 30,
            with_cover: bool = False,
    ) -> list[MusicInfo]:
        """异步读取 ListenBrainz 官方新发行专辑，排序由官方接口决定。"""
        candidates = await self.async_run_module(
            "music_fresh_releases",
            days=days,
            sort=sort,
            past=past,
            future=future,
            offset=max(page - 1, 0) * count,
            count=count,
        )
        results = self._filter_candidates(
            self.normalize_candidates(candidates),
            min_listen_count=0,
            with_cover=with_cover,
        )
        return results[:count]

    async def async_album(self, source: str, media_id: str) -> Optional[MusicAlbumInfo]:
        """异步按来源和专辑 ID 获取标准化专辑详情及曲目。"""
        result = await self.async_run_module(
            "music_album",
            source=source,
            media_id=media_id,
        )
        if isinstance(result, MusicAlbumInfo):
            return result
        if isinstance(result, dict):
            return MusicAlbumInfo.from_dict(result)
        return None

    def album(self, source: str, media_id: str) -> Optional[MusicAlbumInfo]:
        """同步按来源和专辑 ID 获取标准化专辑详情及曲目。"""
        result = self.run_module(
            "music_album",
            source=source,
            media_id=media_id,
        )
        if isinstance(result, MusicAlbumInfo):
            return result
        if isinstance(result, dict):
            return MusicAlbumInfo.from_dict(result)
        return None

    def lyrics(self, music: MetaMusic | MusicInfo) -> Optional[MusicLyrics]:
        """按单曲元数据调用已启用的歌词模块并返回标准歌词。"""
        result = self.run_module("music_lyrics", music=music)
        if isinstance(result, MusicLyrics):
            return result
        if isinstance(result, dict):
            return MusicLyrics.from_dict(result)
        return None

    async def async_artist(self, source: str, media_id: str) -> Optional[MusicArtistInfo]:
        """异步按来源和艺术家 ID 获取标准化艺术家详情。"""
        result = await self.async_run_module(
            "music_artist",
            source=source,
            media_id=media_id,
        )
        if isinstance(result, MusicArtistInfo):
            return result
        if isinstance(result, dict):
            return MusicArtistInfo.from_dict(result)
        return None

    async def async_artist_albums(
            self,
            source: str,
            media_id: str,
            page: int = 1,
            count: int = 30,
            album_type: Optional[str] = None,
    ) -> list[MusicInfo]:
        """异步分页读取艺术家名下的专辑、EP 和单曲。"""
        candidates = await self.async_run_module(
            "music_artist_albums",
            source=source,
            media_id=media_id,
            page=page,
            count=count,
            album_type=album_type,
        )
        return self.normalize_candidates(candidates, limit=count)

    async def async_artist_related(
            self,
            source: str,
            media_id: str,
            count: int = 24,
    ) -> list[MusicArtistInfo]:
        """异步读取关联艺术家，供详情页继续浏览。"""
        candidates = await self.async_run_module(
            "music_artist_related",
            source=source,
            media_id=media_id,
            count=count,
        )
        results: list[MusicArtistInfo] = []
        identities: set[str] = set()
        for candidate in candidates or []:
            info = (
                candidate
                if isinstance(candidate, MusicArtistInfo)
                else MusicArtistInfo.from_dict(candidate)
            )
            identity = (info.media_id or info.name or "").casefold()
            if not identity or identity in identities:
                continue
            identities.add(identity)
            results.append(info)
        return results[:count]

    @staticmethod
    def _filter_candidates(
            candidates: list[MusicInfo],
            min_listen_count: int,
            with_cover: bool,
    ) -> list[MusicInfo]:
        """按热度和封面条件过滤音乐探索候选。"""
        results = candidates
        if min_listen_count > 0:
            results = [
                info for info in results
                if (info.listen_count or 0) >= min_listen_count
            ]
        if with_cover:
            results = [info for info in results if info.cover_url]
        return list(results)

    @staticmethod
    def _normalize_match_text(value: Optional[str]) -> str:
        """移除大小写、空白和标点差异，生成站点标题匹配使用的紧凑文本。"""
        return re.sub(r"[\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)

    @classmethod
    def is_audio_path(cls, path: str | Path) -> bool:
        """判断路径是否指向系统支持的音频文件。"""
        return Path(path).suffix.lower() in settings.RMT_AUDIOEXT

    @classmethod
    def read_path_meta(cls, path: str | Path) -> MetaMusic:
        """读取本地音频标签，不可访问时按文件名构造最小音乐元数据。"""
        file_path = Path(path)
        if file_path.exists() and file_path.is_file():
            return AudioMetadataHelper.read(file_path)
        return cls.parse_query(file_path.stem)

    async def async_recognize_by_path(
            self,
            path: str | Path,
            source: str = "musicbrainz",
    ) -> tuple[MetaMusic, MusicInfo]:
        """根据音频标签和文件名识别音乐，远端不可用时仍返回最小音乐信息。"""
        # Mutagen 会同步读取本地文件，异步识别入口需要移出事件循环。
        meta = await run_in_threadpool(self.read_path_meta, path)
        # 统一识别入口分发到音乐模块，模块负责详情/搜索/匹配/兜底
        info = await self.async_recognize_media(meta=meta, source=source)
        return meta, self._merge_audio_quality(info or self._info_from_meta(meta), meta)

    def recognize_by_path(
            self,
            path: str | Path,
            source: str = "musicbrainz",
    ) -> tuple[MetaMusic, MusicInfo]:
        """同步根据音频标签和文件名识别音乐，并保留离线最小结果。"""
        meta = self.read_path_meta(path)
        # 统一识别入口分发到音乐模块，模块负责详情/搜索/匹配/兜底
        info = self.recognize_media(meta=meta, source=source)
        return meta, self._merge_audio_quality(info or self._info_from_meta(meta), meta)

    @classmethod
    def to_meta(cls, info: MusicInfo) -> MetaMusic:
        """将用户选中的标准音乐信息转换为下载和整理上下文元数据。"""
        return MetaMusic(
            title=info.title,
            artists=list(info.artists),
            album=info.album,
            album_artist=info.album_artist,
            year=info.year,
            disc_number=info.disc_number,
            track_number=info.track_number,
            total_tracks=info.total_tracks,
            version=info.version,
            audio_format=info.audio_format,
            audio_lossless=info.audio_lossless,
            bit_depth=info.bit_depth,
            sample_rate=info.sample_rate,
            bitrate=info.bitrate,
            duration=info.duration,
            isrc=info.isrc,
            media_source=info.source,
            media_id=info.media_id,
        )

    @classmethod
    def _info_from_meta(cls, meta: MetaMusic) -> MusicInfo:
        """把音频标签转换为文件管理可展示的最小音乐信息。"""
        return MusicInfo(
            source=meta.media_source,
            media_id=meta.media_id,
            title=meta.title,
            artists=list(meta.artists),
            album=meta.album,
            album_artist=meta.album_artist,
            year=meta.year,
            disc_number=meta.disc_number,
            track_number=meta.track_number,
            total_tracks=meta.total_tracks,
            duration=meta.duration,
            isrc=meta.isrc,
            version=meta.version,
            audio_format=meta.audio_format,
            audio_lossless=meta.audio_lossless,
            bit_depth=meta.bit_depth,
            sample_rate=meta.sample_rate,
            bitrate=meta.bitrate,
            names=[name for name in (meta.title, meta.album) if name],
        )

    @staticmethod
    def _merge_audio_quality(info: MusicInfo, meta: MetaMusic) -> MusicInfo:
        """将本地文件的实际音频参数合并到远端音乐身份识别结果。"""
        for key in ("audio_format", "audio_lossless", "bit_depth", "sample_rate", "bitrate"):
            value = getattr(meta, key, None)
            if value is not None:
                setattr(info, key, value)
        return info

    @classmethod
    def _candidate_identity(cls, info: MusicInfo) -> tuple[str, ...]:
        """构造跨来源稳定的候选去重键。"""
        if info.source and info.media_id:
            return "id", info.source.casefold(), info.music_type.casefold(), info.media_id.casefold()
        return (
            "metadata",
            info.music_type.casefold(),
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
