from typing import Any, Optional

from app.application.orchestration import ChainBase
from app.domain.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import MediaSource, MediaType


class _MusicMetadataSourceChain(ChainBase):
    """固定音乐元数据来源链的共用端口适配。"""

    source: MediaSource

    def search_music(self, meta: MetaMusic, limit: int = 20) -> list[MusicInfo]:
        """按音乐元数据搜索当前来源候选。"""
        result = self.unicast(
            "search_music",
            meta=meta,
            limit=limit,
            media_source=self.source,
        )
        return self._music_infos(result, limit=limit)

    async def async_search_music(self, meta: MetaMusic, limit: int = 20) -> list[MusicInfo]:
        """异步按音乐元数据搜索当前来源候选。"""
        result = await self.async_unicast(
            "search_music",
            meta=meta,
            limit=limit,
            media_source=self.source,
        )
        return self._music_infos(result, limit=limit)

    def recognize_music(
            self,
            meta: Optional[MetaMusic] = None,
            media_id: Optional[str] = None,
            cache: bool = True,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """按当前来源身份或音乐元数据识别标准音乐信息。"""
        normalized_id = self._normalize_media_id(media_id)
        result = self.unicast(
            "recognize_media",
            meta=meta,
            mtype=MediaType.MUSIC,
            media_source=self.source,
            media_id=normalized_id,
            cache=cache,
            music_type=music_type,
        )
        return self._music_info(result, media_id=normalized_id)

    async def async_recognize_music(
            self,
            meta: Optional[MetaMusic] = None,
            media_id: Optional[str] = None,
            cache: bool = True,
            music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """异步按当前来源身份或音乐元数据识别标准音乐信息。"""
        normalized_id = self._normalize_media_id(media_id)
        result = await self.async_unicast(
            "async_recognize_media",
            meta=meta,
            mtype=MediaType.MUSIC,
            media_source=self.source,
            media_id=normalized_id,
            cache=cache,
            music_type=music_type,
        )
        return self._music_info(result, media_id=normalized_id)

    def get_music_album(self, media_id: str) -> Optional[MusicAlbumInfo]:
        """按当前来源原生 ID 获取专辑详情。"""
        normalized_id = self._normalize_media_id(media_id)
        if not normalized_id:
            return None
        result = self.unicast(
            "music_album",
            media_source=self.source,
            media_id=normalized_id,
        )
        return self._music_album(result, media_id=normalized_id)

    async def async_get_music_album(self, media_id: str) -> Optional[MusicAlbumInfo]:
        """异步按当前来源原生 ID 获取专辑详情。"""
        normalized_id = self._normalize_media_id(media_id)
        if not normalized_id:
            return None
        result = await self.async_unicast(
            "music_album",
            media_source=self.source,
            media_id=normalized_id,
        )
        return self._music_album(result, media_id=normalized_id)

    async def async_get_music_album_related(
            self,
            media_id: str,
            count: int = 24,
    ) -> list[MusicInfo]:
        """异步获取当前来源的专辑关联条目。"""
        normalized_id = self._normalize_media_id(media_id)
        if not normalized_id:
            return []
        result = await self.async_unicast(
            "music_album_related",
            media_source=self.source,
            media_id=normalized_id,
            count=count,
        )
        return self._music_infos(result, limit=count)

    async def async_get_music_artist(self, media_id: str) -> Optional[MusicArtistInfo]:
        """异步按当前来源原生 ID 获取艺术家详情。"""
        normalized_id = self._normalize_media_id(media_id)
        if not normalized_id:
            return None
        result = await self.async_unicast(
            "music_artist",
            media_source=self.source,
            media_id=normalized_id,
        )
        return self._music_artist(result, media_id=normalized_id)

    async def async_get_music_artist_albums(
            self,
            media_id: str,
            page: int = 1,
            count: int = 30,
            album_type: Optional[str] = None,
    ) -> list[MusicInfo]:
        """异步分页获取当前来源艺术家的专辑目录，来源不匹配的提供者以空列表出让，按多播展平后返回。"""
        normalized_id = self._normalize_media_id(media_id)
        if not normalized_id:
            return []
        groups = await self.async_multicast(
            "music_artist_albums",
            media_source=self.source,
            media_id=normalized_id,
            page=page,
            count=count,
            album_type=album_type,
        )
        result = [item for group in groups if isinstance(group, list) for item in group]
        return self._music_infos(result, limit=count)

    async def async_get_music_artist_related(
            self,
            media_id: str,
            count: int = 24,
    ) -> list[MusicArtistInfo]:
        """异步获取当前来源艺术家的关联艺术家。"""
        normalized_id = self._normalize_media_id(media_id)
        if not normalized_id:
            return []
        result = await self.async_unicast(
            "music_artist_related",
            media_source=self.source,
            media_id=normalized_id,
            count=count,
        )
        return self._music_artists(result, limit=count)

    @staticmethod
    def _normalize_media_id(media_id: Optional[str]) -> Optional[str]:
        """清理来源原生 ID，空值和历史零哨兵按无身份处理。"""
        normalized = str(media_id).strip() if media_id is not None else ""
        return normalized if normalized and normalized != "0" else None

    def _music_infos(
            self,
            result: Any,
            limit: Optional[int] = None,
    ) -> list[MusicInfo]:
        """将模块或插件结果统一转换为当前来源的音乐候选列表。"""
        candidates = result if isinstance(result, list) else []
        infos = [
            item if isinstance(item, MusicInfo) else MusicInfo.from_dict(item)
            for item in candidates
            if isinstance(item, (MusicInfo, dict))
        ]
        infos = [info for info in infos if info.media_source == self.source]
        return infos[:limit] if limit else infos

    def _music_info(
            self,
            result: Any,
            media_id: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """校验单条识别结果的来源与显式身份。"""
        if isinstance(result, MusicInfo):
            info = result
        elif isinstance(result, dict):
            info = MusicInfo.from_dict(result)
        else:
            return None
        if info.media_source and info.media_source != self.source:
            return None
        if media_id and (info.media_source != self.source or info.media_id != media_id):
            return None
        return info

    def _music_album(
            self,
            result: Any,
            media_id: Optional[str] = None,
    ) -> Optional[MusicAlbumInfo]:
        """将模块或插件结果统一转换为专辑详情。"""
        if isinstance(result, MusicAlbumInfo):
            album = result
        elif isinstance(result, dict):
            album = MusicAlbumInfo.from_dict(result)
        else:
            return None
        if album.media_source != self.source:
            return None
        if media_id and album.media_id != media_id:
            return None
        return album

    def _music_artist(
            self,
            result: Any,
            media_id: Optional[str] = None,
    ) -> Optional[MusicArtistInfo]:
        """将模块或插件结果统一转换为艺术家详情。"""
        if isinstance(result, MusicArtistInfo):
            artist = result
        elif isinstance(result, dict):
            artist = MusicArtistInfo.from_dict(result)
        else:
            return None
        if artist.media_source != self.source:
            return None
        if media_id and artist.media_id != media_id:
            return None
        return artist

    def _music_artists(
            self,
            result: Any,
            limit: Optional[int] = None,
    ) -> list[MusicArtistInfo]:
        """将模块或插件结果统一转换为艺术家列表。"""
        candidates = result if isinstance(result, list) else []
        artists = [
            item if isinstance(item, MusicArtistInfo) else MusicArtistInfo.from_dict(item)
            for item in candidates
            if isinstance(item, (MusicArtistInfo, dict))
        ]
        artists = [artist for artist in artists if artist.media_source == self.source]
        return artists[:limit] if limit else artists


class MusicBrainzChain(_MusicMetadataSourceChain):
    """MusicBrainz 音乐搜索、识别与详情来源链。"""

    source = MediaSource.MusicBrainz

    def match_music_album(
            self,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            limit: int = 5,
    ) -> Optional[MusicAlbumInfo]:
        """按目录元数据与曲目证据匹配 MusicBrainz 发行版本。"""
        result = self.unicast(
            "match_music_album",
            meta=meta,
            tracks=tracks,
            limit=limit,
        )
        return self._music_album(result)

    async def async_match_music_album(
            self,
            meta: MetaMusic,
            tracks: list[MetaMusic],
            limit: int = 5,
    ) -> Optional[MusicAlbumInfo]:
        """异步按目录元数据与曲目证据匹配 MusicBrainz 发行版本。"""
        result = await self.async_unicast(
            "async_match_music_album",
            meta=meta,
            tracks=tracks,
            limit=limit,
        )
        return self._music_album(result)

    def cache_items(self) -> list[dict]:
        """查询音乐识别缓存条目列表。"""
        result = self.unicast("music_cache_items")
        return result or []

    def delete_cache(self, cache_key: str) -> dict:
        """按缓存键删除单条音乐识别缓存。"""
        result = self.unicast("music_cache_delete", cache_key=cache_key)
        return result or {}

    def clear_cache(self) -> None:
        """清空全部音乐识别缓存。"""
        self.broadcast("music_cache_clear")
