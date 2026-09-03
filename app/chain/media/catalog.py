"""音乐来源、目录搜索与详情路由 owner。"""

from copy import deepcopy
from typing import Any, Iterable, Optional, cast

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.music.catalog import MusicCatalogService
from app.chain.douban import DoubanChain
from app.chain.media.contract import _MediaOwnerBase
from app.chain.musicbrainz import MusicBrainzChain, MusicMetadataSourceChain
from app.chain.theaudiodb import TheAudioDbChain
from app.domain.context import (
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
)
from app.domain.meta.metamusic import MetaMusic
from app.foundation.text import convert as zhconv_convert
from app.runtime.log import logger
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.schemas.types import (
    MediaSource,
    MediaSourceSelection,
)


class MediaCatalogOwner(_MediaOwnerBase):
    """音乐来源、目录搜索与详情路由 owner。"""

    @staticmethod
    def _music_source_chain(
        media_source: MediaSource,
    ) -> Optional[MusicMetadataSourceChain | DoubanChain]:
        """返回内置来源专用链，或绑定插件扩展来源的通用音乐端口。"""
        source = normalize_media_source(media_source)
        if not source:
            return None
        chains: dict[
            MediaSource,
            type[MusicMetadataSourceChain] | type[DoubanChain],
        ] = {
            MediaSource.MusicBrainz: MusicBrainzChain,
            MediaSource.TheAudioDB: TheAudioDbChain,
            MediaSource.DoubanMusic: DoubanChain,
        }
        chain_type = chains.get(source)
        if chain_type:
            return chain_type()
        plugin_chain = MusicMetadataSourceChain()
        plugin_chain.source = source
        return plugin_chain

    @classmethod
    def _music_search_sources(
        cls,
        media_source: Optional[MediaSourceSelection],
    ) -> list[MediaSource]:
        """解析有序音乐搜索来源集合，保留合法插件扩展来源并去重。"""
        return MusicCatalogService(
            source_resolver=cls._music_source_chain,
            warning=logger.warning,
            primary_source=cls._music_primary_source,
        ).search_sources(media_source)

    @staticmethod
    async def _async_search_music_source(
        chain: MusicMetadataSourceChain | DoubanChain,
        source: MediaSource,
        meta: MetaMusic,
        limit: int,
    ) -> list[MusicInfo]:
        """异步搜索单个音乐来源，来源失败时保留其它来源的候选。"""
        try:
            return await chain.async_search_music(meta, limit=limit)
        except Exception as err:
            logger.warning(f"音乐来源 {source} 搜索失败：{str(err)}")
            return []

    @classmethod
    def normalize_music_candidates(
        cls,
        candidates: Optional[Iterable[MusicInfo | dict[str, Any]]],
        limit: Optional[int] = None,
    ) -> list[MusicInfo]:
        """标准化并按来源身份或元数据去重音乐候选。"""
        return MusicCatalogService.normalize_candidates(candidates, limit)

    def _music_catalog(self) -> MusicCatalogService:
        """构造绑定当前来源解析规则的音乐目录服务。"""
        return MusicCatalogService(
            source_resolver=self._music_source_chain,
            warning=logger.warning,
            primary_source=self._music_primary_source,
        )

    def search_music(
        self,
        query: str,
        limit: int = 20,
        media_source: Optional[MediaSourceSelection] = None,
    ) -> list[MusicInfo]:
        """按一个或多个音乐来源搜索候选，未指定时使用 MusicBrainz。"""
        return self._music_catalog().search(query, limit, media_source)

    async def async_search_music(
        self,
        query: str,
        limit: int = 20,
        media_source: Optional[MediaSourceSelection] = None,
    ) -> list[MusicInfo]:
        """并行搜索一个或多个音乐来源，单一来源失败不影响其它结果。"""
        return await self._music_catalog().async_search(
            query,
            limit,
            media_source,
        )

    @classmethod
    def _validate_music_result(
        cls,
        result: Optional[MusicInfo],
        media_source: MediaSource,
        media_id: Optional[str],
        music_type: Optional[str],
    ) -> Optional[MusicInfo]:
        """校验来源链返回的音乐身份和实体类型。"""
        if not isinstance(result, MusicInfo):
            return None
        if result.media_source and result.media_source != media_source:
            return None
        if music_type and result.music_type != music_type:
            return None
        if media_id and (result.media_source != media_source or str(result.media_id or "") != media_id):
            return None
        return cls._simplify_recognized_music_info(result)

    @classmethod
    def _simplify_recognized_music_info(cls, info: MusicInfo) -> MusicInfo:
        """按开关转换标准音乐文本字段，并避免修改来源模块的缓存对象。"""
        if not get_chain_runtime_config_snapshot().music_metadata_to_simplified:
            return info
        updates: dict[str, Any] = {}
        for field_name in cls._music_simplified_text_fields:
            value = getattr(info, field_name, None)
            if isinstance(value, str):
                converted = zhconv_convert(value, "zh-hans")
                if converted != value:
                    updates[field_name] = converted
        for field_name in cls._music_simplified_list_fields:
            value = getattr(info, field_name, None)
            if isinstance(value, list):
                converted_items = [
                    zhconv_convert(item, "zh-hans")
                    if isinstance(item, str)
                    else item
                    for item in value
                ]
                if converted_items != value:
                    updates[field_name] = converted_items
        if not updates:
            return info
        simplified = deepcopy(info)
        for field_name, value in updates.items():
            setattr(simplified, field_name, value)
        return simplified

    @classmethod
    def _simplify_recognized_music_mapping(
        cls,
        matched: dict[str, MusicInfo],
    ) -> dict[str, MusicInfo]:
        """转换目录识别结果，同时让缓存始终保留来源返回的原始文本。"""
        simplified = {path: cls._simplify_recognized_music_info(info) for path, info in matched.items()}
        if all(simplified[path] is info for path, info in matched.items()):
            return matched
        return simplified

    def recognize_music_from_source(
        self,
        media_source: MediaSource,
        meta: Optional[MetaMusic] = None,
        media_id: Optional[str] = None,
        cache: bool = True,
        music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """通过固定来源链同步识别音乐实体。"""
        source = normalize_media_source(media_source)
        normalized_id = str(media_id).strip() if media_id is not None else None
        if not source or normalized_id == "0":
            return None
        chain = self._music_source_chain(source)
        if not chain:
            return None
        result = chain.recognize_music(
            meta=meta,
            media_id=normalized_id,
            cache=cache,
            music_type=music_type,
        )
        validated = self._validate_music_result(
            result,
            source,
            normalized_id,
            music_type,
        )
        return cast(
            Optional[MusicInfo],
            self._finalize_recognition_result(validated),
        )

    async def async_recognize_music_from_source(
        self,
        media_source: MediaSource,
        meta: Optional[MetaMusic] = None,
        media_id: Optional[str] = None,
        cache: bool = True,
        music_type: Optional[str] = None,
    ) -> Optional[MusicInfo]:
        """通过固定来源链异步识别音乐实体。"""
        source = normalize_media_source(media_source)
        normalized_id = str(media_id).strip() if media_id is not None else None
        if not source or normalized_id == "0":
            return None
        chain = self._music_source_chain(source)
        if not chain:
            return None
        result = await chain.async_recognize_music(
            meta=meta,
            media_id=normalized_id,
            cache=cache,
            music_type=music_type,
        )
        validated = self._validate_music_result(
            result,
            source,
            normalized_id,
            music_type,
        )
        return cast(
            Optional[MusicInfo],
            await self._async_finalize_recognition_result(validated),
        )

    def get_music_album(
        self,
        media_source: MediaSource,
        media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """按音乐来源和原生 ID 同步获取专辑详情。"""
        source, normalized_id = resolve_media_identity(media_source=media_source, media_id=media_id)
        if not source or not normalized_id:
            return None
        chain = self._music_source_chain(source)
        result = chain.get_music_album(normalized_id) if chain else None
        return cast(
            Optional[MusicAlbumInfo],
            self._finalize_recognition_result(result),
        )

    async def async_get_music_album(
        self,
        media_source: MediaSource,
        media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """按音乐来源和原生 ID 异步获取专辑详情。"""
        source, normalized_id = resolve_media_identity(media_source=media_source, media_id=media_id)
        if not source or not normalized_id:
            return None
        chain = self._music_source_chain(source)
        result = await chain.async_get_music_album(normalized_id) if chain else None
        return cast(
            Optional[MusicAlbumInfo],
            await self._async_finalize_recognition_result(result),
        )

    async def async_get_music_album_related(
        self,
        media_source: MediaSource,
        media_id: str,
        count: int = 24,
    ) -> list[MusicInfo]:
        """按音乐来源读取指定专辑的关联条目。"""
        source, normalized_id = resolve_media_identity(media_source=media_source, media_id=media_id)
        if not source or not normalized_id:
            return []
        chain = self._music_source_chain(source)
        if not chain:
            return []
        return self.normalize_music_candidates(
            await chain.async_get_music_album_related(normalized_id, count=count),
            limit=count,
        )

    async def async_get_music_artist(
        self,
        media_source: MediaSource,
        media_id: str,
    ) -> Optional[MusicArtistInfo]:
        """按音乐来源读取艺术家详情。"""
        source, normalized_id = resolve_media_identity(media_source=media_source, media_id=media_id)
        if not source or not normalized_id:
            return None
        chain = self._music_source_chain(source)
        if not chain or not hasattr(chain, "async_get_music_artist"):
            return None
        result = await chain.async_get_music_artist(normalized_id)
        return cast(
            Optional[MusicArtistInfo],
            await self._async_finalize_recognition_result(result),
        )

    async def async_get_music_artist_albums(
        self,
        media_source: MediaSource,
        media_id: str,
        page: int = 1,
        count: int = 30,
        album_type: Optional[str] = None,
    ) -> list[MusicInfo]:
        """按音乐来源分页读取艺术家的专辑目录。"""
        source, normalized_id = resolve_media_identity(media_source=media_source, media_id=media_id)
        if not source or not normalized_id:
            return []
        chain = self._music_source_chain(source)
        if not chain or not hasattr(chain, "async_get_music_artist_albums"):
            return []
        return self.normalize_music_candidates(
            await chain.async_get_music_artist_albums(normalized_id, page=page, count=count, album_type=album_type),
            limit=count,
        )

    async def async_get_music_artist_related(
        self,
        media_source: MediaSource,
        media_id: str,
        count: int = 24,
    ) -> list[MusicArtistInfo]:
        """按音乐来源读取关联艺术家。"""
        source, normalized_id = resolve_media_identity(media_source=media_source, media_id=media_id)
        if not source or not normalized_id:
            return []
        chain = self._music_source_chain(source)
        if not chain or not hasattr(chain, "async_get_music_artist_related"):
            return []
        return await chain.async_get_music_artist_related(normalized_id, count=count)
