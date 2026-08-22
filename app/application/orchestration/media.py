from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, List, Optional, Tuple, Union

from app.runtime.execution import run_in_threadpool

from app.schemas.event import MediaRecognizeConvertEventData as _SchemaMediaRecognizeConvertEventData
from app.application.orchestration import ChainBase
from app.application.orchestration.acoustid import AcoustIdChain
from app.application.orchestration.douban import DoubanChain
from app.application.orchestration.musicbrainz import MusicBrainzChain, _MusicMetadataSourceChain
from app.application.orchestration.theaudiodb import TheAudioDbChain
from app.runtime.cache import async_fresh, fresh
from app.application.configuration import get_chain_runtime_config_snapshot
from app.domain.context import (
    Context,
    MediaInfo,
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
)
from app.runtime.events import eventmanager, Event
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo, MetaInfoPath
from app.application.audio import AudioMetadataHelper
from app.application.music.catalog import MusicCatalogService
from app.runtime.log import logger
from app.schemas.types import (
    MUSIC_ENTITY_RECORDING,
    ChainEventType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
)
from app.domain.media import is_music_media_source
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.foundation.singleton import Singleton
from app.foundation.text import convert as zhconv_convert
from app.domain import title as title_rules

recognize_lock = Lock()


class MediaChain(ChainBase, metaclass=Singleton):
    """
    媒体信息处理链，单例运行
    """

    _video_primary_source = MediaSource.TMDB
    _music_primary_source = MediaSource.MusicBrainz
    _album_dir_cache: dict[str, tuple[tuple[str, ...], dict[str, MusicInfo]]] = {}
    _album_dir_cache_max = 128
    _album_match_min_files = 2
    _music_simplified_text_fields = (
        "title",
        "album",
        "album_artist",
        "album_type",
        "version",
        "category",
    )
    _music_simplified_list_fields = ("artists", "genres", "names")

    @staticmethod
    def _music_source_chain(
            media_source: MediaSource,
    ) -> Optional[_MusicMetadataSourceChain | DoubanChain]:
        """返回内置来源专用链，或绑定插件扩展来源的通用音乐端口。"""
        source = normalize_media_source(media_source)
        if not source:
            return None
        chains = {
            MediaSource.MusicBrainz: MusicBrainzChain,
            MediaSource.TheAudioDB: TheAudioDbChain,
            MediaSource.DoubanMusic: DoubanChain,
        }
        chain_type = chains.get(source)
        if chain_type:
            return chain_type()
        plugin_chain = _MusicMetadataSourceChain()
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
            chain: _MusicMetadataSourceChain | DoubanChain,
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

    @staticmethod
    def _validate_music_result(
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
        if media_id and (
                result.media_source != media_source
                or str(result.media_id or "") != media_id
        ):
            return None
        return MediaChain._simplify_recognized_music_info(result)

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
                converted = [
                    zhconv_convert(item, "zh-hans") if isinstance(item, str) else item
                    for item in value
                ]
                if converted != value:
                    updates[field_name] = converted
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
        simplified = {
            path: cls._simplify_recognized_music_info(info)
            for path, info in matched.items()
        }
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
        chain = self._music_source_chain(source)
        normalized_id = str(media_id).strip() if media_id is not None else None
        if not chain or normalized_id == "0":
            return None
        result = chain.recognize_music(
            meta=meta,
            media_id=normalized_id,
            cache=cache,
            music_type=music_type,
        )
        return self._validate_music_result(result, source, normalized_id, music_type)

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
        chain = self._music_source_chain(source)
        normalized_id = str(media_id).strip() if media_id is not None else None
        if not chain or normalized_id == "0":
            return None
        result = await chain.async_recognize_music(
            meta=meta,
            media_id=normalized_id,
            cache=cache,
            music_type=music_type,
        )
        return self._validate_music_result(result, source, normalized_id, music_type)

    def get_music_album(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """按音乐来源和原生 ID 同步获取专辑详情。"""
        source, normalized_id = resolve_media_identity(
            media_source=media_source, media_id=media_id
        )
        chain = self._music_source_chain(source)
        return chain.get_music_album(normalized_id) if chain and normalized_id else None

    async def async_get_music_album(
            self,
            media_source: MediaSource,
            media_id: str,
    ) -> Optional[MusicAlbumInfo]:
        """按音乐来源和原生 ID 异步获取专辑详情。"""
        source, normalized_id = resolve_media_identity(
            media_source=media_source, media_id=media_id
        )
        chain = self._music_source_chain(source)
        return await chain.async_get_music_album(normalized_id) \
            if chain and normalized_id else None

    async def async_get_music_album_related(
            self,
            media_source: MediaSource,
            media_id: str,
            count: int = 24,
    ) -> list[MusicInfo]:
        """按音乐来源读取指定专辑的关联条目。"""
        source, normalized_id = resolve_media_identity(
            media_source=media_source, media_id=media_id
        )
        chain = self._music_source_chain(source)
        if not chain or not normalized_id:
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
        source, normalized_id = resolve_media_identity(
            media_source=media_source, media_id=media_id
        )
        chain = self._music_source_chain(source)
        if not chain or not normalized_id or not hasattr(chain, "async_get_music_artist"):
            return None
        return await chain.async_get_music_artist(normalized_id)

    async def async_get_music_artist_albums(
            self,
            media_source: MediaSource,
            media_id: str,
            page: int = 1,
            count: int = 30,
            album_type: Optional[str] = None,
    ) -> list[MusicInfo]:
        """按音乐来源分页读取艺术家的专辑目录。"""
        source, normalized_id = resolve_media_identity(
            media_source=media_source, media_id=media_id
        )
        chain = self._music_source_chain(source)
        if not chain or not normalized_id or not hasattr(chain, "async_get_music_artist_albums"):
            return []
        return self.normalize_music_candidates(
            await chain.async_get_music_artist_albums(
                normalized_id, page=page, count=count, album_type=album_type
            ),
            limit=count,
        )

    async def async_get_music_artist_related(
            self,
            media_source: MediaSource,
            media_id: str,
            count: int = 24,
    ) -> list[MusicArtistInfo]:
        """按音乐来源读取关联艺术家。"""
        source, normalized_id = resolve_media_identity(
            media_source=media_source, media_id=media_id
        )
        chain = self._music_source_chain(source)
        if not chain or not normalized_id or not hasattr(chain, "async_get_music_artist_related"):
            return []
        return await chain.async_get_music_artist_related(normalized_id, count=count)

    def _run_native_media_recognize(
            self,
            module_kwargs: dict,
            cache: bool,
    ) -> Optional[MediaInfo]:
        """统一同步媒体识别路由，未指定来源时影视和音乐只使用各自主数据源。"""
        meta = module_kwargs.get("meta")
        mtype = module_kwargs.get("mtype")
        media_source = module_kwargs.get("media_source")
        if (
                isinstance(meta, MetaMusic)
                or mtype == MediaType.MUSIC
                or is_music_media_source(media_source)
        ):
            if media_source:
                recognize_kwargs = {
                    "media_source": media_source,
                    "meta": meta if isinstance(meta, MetaMusic) else None,
                    "media_id": module_kwargs.get("media_id"),
                    "cache": cache,
                }
                if "music_type" in module_kwargs:
                    recognize_kwargs["music_type"] = module_kwargs["music_type"]
                with fresh(not cache):
                    return self.recognize_music_from_source(**recognize_kwargs)
            if isinstance(meta, MetaMusic):
                return self.recognize_music_from_source(
                    media_source=self._music_primary_source,
                    meta=meta,
                    cache=cache,
                    music_type=MUSIC_ENTITY_RECORDING,
                )
            return None
        if not media_source and isinstance(meta, MetaBase):
            module_kwargs = {
                **module_kwargs,
                "media_source": self._video_primary_source,
            }
        return super()._run_native_media_recognize(module_kwargs, cache)

    async def _async_run_native_media_recognize(
            self,
            module_kwargs: dict,
            cache: bool,
    ) -> Optional[MediaInfo]:
        """统一异步媒体识别路由，未指定来源时影视和音乐只使用各自主数据源。"""
        meta = module_kwargs.get("meta")
        mtype = module_kwargs.get("mtype")
        media_source = module_kwargs.get("media_source")
        if (
                isinstance(meta, MetaMusic)
                or mtype == MediaType.MUSIC
                or is_music_media_source(media_source)
        ):
            if media_source:
                recognize_kwargs = {
                    "media_source": media_source,
                    "meta": meta if isinstance(meta, MetaMusic) else None,
                    "media_id": module_kwargs.get("media_id"),
                    "cache": cache,
                }
                if "music_type" in module_kwargs:
                    recognize_kwargs["music_type"] = module_kwargs["music_type"]
                async with async_fresh(not cache):
                    return await self.async_recognize_music_from_source(
                        **recognize_kwargs
                    )
            if isinstance(meta, MetaMusic):
                return await self.async_recognize_music_from_source(
                    media_source=self._music_primary_source,
                    meta=meta,
                    cache=cache,
                    music_type=MUSIC_ENTITY_RECORDING,
                )
            return None
        if not media_source and isinstance(meta, MetaBase):
            module_kwargs = {
                **module_kwargs,
                "media_source": self._video_primary_source,
            }
        return await super()._async_run_native_media_recognize(module_kwargs, cache)











    @staticmethod
    def select_recognize_source(
            log_name: str, log_context: str, native_fn, plugin_fn,
            is_recognized=None,
            plugin_event: ChainEventType = ChainEventType.NameRecognize,
    ) -> Optional[MediaInfo]:
        """
        选择识别模式，插件优先或原生优先

        :param log_name: 用于日志“标题：...”处的名称（如 file_path.name 或 title）
        :param log_context: 用于日志“未识别到...的媒体信息”处的上下文（如 path 或 title）
        :param native_fn: 原生识别函数
        :param plugin_fn: 插件识别函数
        :param is_recognized: 判定识别结果是否有效的谓词；音乐原生兜底结果无远端身份，
            需视为未识别才会请求辅助识别，影视默认按非空判定
        :param plugin_event: 辅助识别对应的链式事件类型，音乐使用音乐名称识别事件
        """
        if is_recognized is None:
            is_recognized = lambda result: bool(result)
        mediainfo = None
        plugin_available = eventmanager.check(plugin_event)
        if get_chain_runtime_config_snapshot().recognize_plugin_first and plugin_available:
            # 插件优先
            logger.info(f"插件识别优先模式已开启。请求辅助识别，标题：{log_name} ...")
            helped = plugin_fn()
            if is_recognized(helped):
                mediainfo = helped
            else:
                logger.info(
                    f"辅助识别未识别到 {log_context} 的媒体信息，尝试使用原生识别 ..."
                )
                mediainfo = native_fn()
                # 辅助结果不采信时保留原生兜底，避免丢失已有识别结果（音乐原生兜底恒非空）
                if helped and not mediainfo:
                    mediainfo = helped
        else:
            # 原生优先
            logger.info(f"开始识别标题：{log_name} ...")
            mediainfo = native_fn()
            if not is_recognized(mediainfo) and plugin_available:
                logger.info(
                    f"原生识别未识别到 {log_context} 的媒体信息，尝试使用辅助识别 ..."
                )
                helped = plugin_fn()
                if is_recognized(helped):
                    mediainfo = helped
        return mediainfo

    def recognize_by_meta(
            self,
            metainfo: MetaBase,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            obtain_images: bool = False,
            mtype: Optional[MediaType] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        根据主副标题识别媒体信息

        :param metainfo: 标题解析元数据
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param obtain_images: 是否补充图片
        :param mtype: 上游已确定的媒体类型
        :param music_type: 音乐实体类型，用于约束显式音乐身份及插件结果
        """
        mediainfo = self._recognize_with_fallback_by_meta(
            metainfo=metainfo,
            mtype=mtype,
            media_source=media_source,
            episode_group=episode_group,
            obtain_images=obtain_images,
            music_type=music_type,
        )
        if not mediainfo:
            logger.warn(f"{metainfo.title} 未识别到媒体信息")
        return mediainfo

    @staticmethod
    def _build_tmdb_supplement_meta(
            mediainfo: MediaInfo,
            metainfo: Optional[MetaBase] = None,
    ) -> MetaBase:
        """
        根据主识别结果构造 TMDB 辅助识别参数。

        :param mediainfo: 主识别源返回的媒体信息
        :param metainfo: 原始标题解析信息
        :return: 不携带主识别源身份的 TMDB 查询参数
        """
        title = mediainfo.title or getattr(metainfo, "name", None) or ""
        tmdb_meta = MetaInfo(title)
        if not tmdb_meta.cn_name and getattr(metainfo, "cn_name", None):
            tmdb_meta.cn_name = metainfo.cn_name
        if not tmdb_meta.en_name:
            tmdb_meta.en_name = mediainfo.en_title or (
                getattr(metainfo, "en_name", None)
            )
        tmdb_meta.type = mediainfo.type or (
            getattr(metainfo, "type", None) or MediaType.UNKNOWN
        )
        season = (
            mediainfo.season
            if mediainfo.season is not None
            else getattr(metainfo, "begin_season", None)
        )
        tmdb_meta.begin_season = season
        season_year = None
        if season is not None and mediainfo.season_years:
            season_year = (
                mediainfo.season_years.get(season)
                or mediainfo.season_years.get(str(season))
            )
        tmdb_meta.year = (
            season_year
            or mediainfo.year
            or getattr(metainfo, "year", None)
        )
        return tmdb_meta

    @staticmethod
    def _merge_tmdb_auxiliary(
            mediainfo: MediaInfo,
            tmdb_media: MediaInfo,
    ) -> MediaInfo:
        """
        将 TMDB 兼容字段合并到主识别结果，不改变主数据源身份和展示信息。

        :param mediainfo: 主识别源返回的媒体信息
        :param tmdb_media: TMDB 辅助识别结果
        :return: 已补充 TMDB 兼容字段的主媒体信息
        """
        if (
                not tmdb_media
                or tmdb_media.media_source != MediaSource.TMDB
                or not tmdb_media.tmdb_id
        ):
            return mediainfo

        mediainfo.tmdb_id = tmdb_media.tmdb_id
        mediainfo.tmdb_info = tmdb_media.tmdb_info or mediainfo.tmdb_info
        if not mediainfo.category:
            mediainfo.category = tmdb_media.category
        if not mediainfo.genre_ids:
            mediainfo.genre_ids = list(tmdb_media.genre_ids or [])
        for field in ("imdb_id", "tvdb_id", "tvdb_slug", "collection_id"):
            if not getattr(mediainfo, field, None):
                setattr(mediainfo, field, getattr(tmdb_media, field, None))
        return mediainfo

    def supplement_tmdb_info(
            self,
            mediainfo: Optional[Union[MediaInfo, MusicInfo]],
            metainfo: Optional[MetaBase] = None,
    ) -> Optional[Union[MediaInfo, MusicInfo]]:
        """
        为任意主识别源补充 TMDB 辅助信息，同时保留原始媒体身份。

        :param mediainfo: 主识别源返回的媒体信息
        :param metainfo: 原始标题解析信息
        :return: 已补充 TMDB 辅助字段的原媒体对象
        """
        if not mediainfo:
            return None
        # 音乐原样返回：下面全是 TMDB 影视字段，MusicInfo 上根本没有。用 isinstance
        # 而不只看 type，一来静态检查能据此收窄（.type == 的比较收窄不了类型），二来
        # type 没被正确赋值的 MusicInfo 也挡得住，不至于到下一行才 AttributeError
        if isinstance(mediainfo, MusicInfo) or mediainfo.type == MediaType.MUSIC:
            return mediainfo
        if mediainfo.tmdb_id and mediainfo.tmdb_info and mediainfo.genre_ids:
            return mediainfo
        tmdb_meta = self._build_tmdb_supplement_meta(mediainfo, metainfo)
        tmdb_module = self.modulemanager.get_running_module("TheMovieDbModule")
        if not tmdb_module:
            logger.warn("TMDB 模块未启用，无法补充 TMDB 辅助信息")
            return mediainfo
        try:
            tmdb_media = tmdb_module.recognize_media(
                meta=tmdb_meta,
                mtype=mediainfo.type,
                media_source=MediaSource.TMDB,
                media_id=str(mediainfo.tmdb_id) if mediainfo.tmdb_id else None,
                episode_group=mediainfo.episode_group,
                cache=True,
            )
        except Exception as err:
            logger.warn(f"{mediainfo.title_year} 补充 TMDB 辅助信息失败：{err}")
            return mediainfo
        if not tmdb_media:
            logger.warn(f"{mediainfo.title_year} 未匹配到 TMDB 辅助信息")
            return mediainfo
        return self._merge_tmdb_auxiliary(mediainfo, tmdb_media)

    def _recognize_with_fallback_by_meta(
            self,
            metainfo: MetaBase,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            obtain_images: bool = False,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        根据标题识别媒体信息，必要时回退到辅助识别。

        :param metainfo: 标题解析元数据
        :param mtype: 上游已确定的媒体类型
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param obtain_images: 是否补充图片
        :param music_type: 音乐实体类型，用于约束显式音乐身份及插件结果
        :return: 统一媒体信息
        """
        if not metainfo:
            return None
        title = metainfo.title
        share_meta = deepcopy(metainfo)
        # 音乐原生兜底结果无远端身份，需按是否取得身份判定，才会请求辅助识别
        is_music = mtype == MediaType.MUSIC or isinstance(metainfo, MetaMusic)
        is_recognized = (
            (lambda result: bool(result and result.media_source)) if is_music else None
        )

        def native_recognize() -> Optional[MediaInfo]:
            """使用请求级数据源执行原生识别。"""
            return self.recognize_media(
                meta=metainfo,
                mtype=mtype,
                media_source=media_source,
                share_meta=share_meta,
                episode_group=episode_group,
                music_type=music_type,
            )

        def plugin_recognize() -> Optional[MediaInfo]:
            """执行辅助识别并保持请求级数据源约束。"""
            if is_music and not isinstance(metainfo, MetaMusic):
                return None
            return self.recognize_help(
                title=title,
                org_meta=metainfo,
                share_meta=share_meta,
                media_source=media_source,
                episode_group=episode_group,
                music_type=music_type,
            )

        # 按 config 中设置的识别顺序识别，影视与音乐共用同一选择流程
        mediainfo = self.select_recognize_source(
            log_name=title,
            log_context=title,
            native_fn=native_recognize,
            plugin_fn=plugin_recognize,
            is_recognized=is_recognized,
            plugin_event=(
                ChainEventType.MusicNameRecognize if is_music
                else ChainEventType.NameRecognize
            ),
        )
        if not mediainfo:
            return None
        # 识别成功
        logger.info(
            f"{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}"
        )
        if obtain_images:
            self.obtain_images(mediainfo=mediainfo)
        return mediainfo

    @staticmethod
    def _parse_recognize_event_number(value) -> Optional[int]:
        """
        解析辅助识别返回的季集号，兼容整数和数字字符串并保留数值 0。
        """
        if value is None:
            return None
        text = str(value).strip()
        return int(text) if text.isdigit() else None

    def recognize_help(
            self,
            title: str,
            org_meta: MetaBase,
            share_meta: MetaBase = None,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        请求辅助识别，返回媒体信息；影视与音乐共用同一流程，仅要素事件与重组方式不同

        :param title: 标题
        :param org_meta: 原始元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param music_type: 音乐实体类型，仅音乐辅助识别使用
        """
        # 音乐标题要素（曲名/艺术家/专辑/年份）与影视不同，走专用名称识别事件
        if isinstance(org_meta, MetaMusic):
            return self._recognize_music_help(
                title=title,
                org_meta=org_meta,
                share_meta=share_meta,
                media_source=media_source,
                music_type=music_type,
            )
        # 发送请求事件，等待结果
        result: Event = eventmanager.send_event(
            ChainEventType.NameRecognize,
            {
                "title": title,
            },
        )
        if not result:
            return None
        # 获取返回事件数据
        event_data = result.event_data or {}
        logger.info(f"获取到辅助识别结果：{event_data}")
        # 处理数据格式
        title, year, season_number, episode_number = None, None, None, None
        if event_data.get("name"):
            title = str(event_data["name"]).split("/")[0].strip().replace(".", " ")
        if event_data.get("year"):
            year = str(event_data["year"]).split("/")[0].strip()
        season_number = self._parse_recognize_event_number(event_data.get("season"))
        episode_number = self._parse_recognize_event_number(event_data.get("episode"))
        if not title:
            return None
        if title == "Unknown":
            return None
        if not str(year).isdigit():
            year = None
        # 结果赋值
        if title == org_meta.name and year == org_meta.year:
            logger.info(f"辅助识别与原始识别结果一致，无需重新识别媒体信息")
            return None
        logger.info(f"辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        org_meta.name = title
        org_meta.year = year
        org_meta.begin_season = season_number
        org_meta.begin_episode = episode_number
        if org_meta.begin_season is not None or org_meta.begin_episode is not None:
            org_meta.type = MediaType.TV
        # 重新识别
        return self.recognize_media(
            meta=org_meta,
            media_source=media_source,
            share_meta=share_meta,
            episode_group=episode_group,
        )

    def _recognize_music_help(
            self,
            title: str,
            org_meta: MetaMusic,
            share_meta: MetaBase = None,
            media_source: Optional[MediaSource] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        请求插件辅助识别音乐标题要素，并按修正后的要素重新匹配媒体信息

        :param title: 原始音乐标题
        :param org_meta: 原始音乐元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param media_source: 请求级识别数据源
        :param music_type: 音乐实体类型
        """
        # 发送音乐名称识别事件，等待插件返回标题要素
        result: Event = eventmanager.send_event(
            ChainEventType.MusicNameRecognize,
            {
                "title": title,
                "artist": org_meta.artist,
                "album": org_meta.album,
                "year": org_meta.year,
                "music_type": music_type,
            },
        )
        if not result:
            return None
        event_data = result.event_data or {}
        logger.info(f"获取到音乐辅助识别结果：{event_data}")
        name, artist, album, year = self._parse_music_recognize_event(event_data)
        if not name:
            return None
        # 辅助识别要素与原始一致时无需重新匹配
        if (
                name == org_meta.title
                and (not artist or artist in org_meta.artists)
                and (not album or album == org_meta.album)
                and (not year or year == org_meta.year)
        ):
            logger.info("音乐辅助识别与原始识别结果一致，无需重新匹配媒体信息")
            return None
        logger.info("音乐辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        new_meta = self._build_music_help_meta(
            org_meta=org_meta,
            name=name,
            artist=artist,
            album=album,
            year=year,
        )
        # 重新识别，仅采信取得远端身份的结果，否则由选择流程保留原生兜底
        mediainfo = self.recognize_media(
            meta=new_meta,
            media_source=media_source,
            share_meta=share_meta,
            music_type=music_type,
        )
        return mediainfo if mediainfo and mediainfo.media_source else None

    @staticmethod
    def _parse_music_recognize_event(
            event_data: dict,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
        """
        解析音乐辅助识别返回的标题要素，曲名为空或未知时返回 None
        """
        name = None
        if event_data.get("name"):
            name = str(event_data["name"]).split("/")[0].strip().replace(".", " ")
        artist = None
        if event_data.get("artist"):
            artist = str(event_data["artist"]).split("/")[0].strip()
        album = None
        if event_data.get("album"):
            album = str(event_data["album"]).split("/")[0].strip()
        year = None
        year_text = str(event_data.get("year") or "").split("/")[0].strip()
        if year_text.isdigit():
            year = int(year_text)
        if not name or name == "Unknown":
            name = None
        return name, artist, album, year

    @staticmethod
    def _build_music_help_meta(
            org_meta: MetaMusic,
            name: str,
            artist: Optional[str],
            album: Optional[str],
            year: Optional[int],
    ) -> MetaMusic:
        """按插件修正标题要素，并保留本地轨道与音频判定证据。"""
        return MetaMusic(
            org_string=org_meta.org_string,
            title=name,
            artists=[artist] if artist else list(org_meta.artists or []),
            album=album or org_meta.album,
            album_artist=artist or org_meta.album_artist,
            year=year or org_meta.year,
            disc_number=org_meta.disc_number,
            track_number=org_meta.track_number,
            total_discs=org_meta.total_discs,
            total_tracks=org_meta.total_tracks,
            version=org_meta.version,
            audio_format=org_meta.audio_format,
            audio_lossless=org_meta.audio_lossless,
            bit_depth=org_meta.bit_depth,
            sample_rate=org_meta.sample_rate,
            bitrate=org_meta.bitrate,
            duration=org_meta.duration,
            isrc=org_meta.isrc,
        )

    @classmethod
    def is_audio_path(cls, path: Union[str, Path]) -> bool:
        """判断路径是否指向系统支持的音频文件。"""
        return Path(path).suffix.lower() in get_chain_runtime_config_snapshot().audio_extensions

    @classmethod
    def read_path_meta(cls, path: Union[str, Path]) -> MetaMusic:
        """读取本地音频标签，不可访问时回退到文件名和目录线索。"""
        file_path = Path(path)
        if file_path.exists() and file_path.is_file():
            return AudioMetadataHelper.read(file_path)
        return AudioMetadataHelper.read_filename(file_path)

    @classmethod
    def _music_info_from_path_meta(cls, meta: MetaMusic) -> MusicInfo:
        """把音频标签转换为文件管理可展示的最小音乐信息。"""
        return MusicInfo.from_meta(meta)

    @staticmethod
    def _merge_music_audio_quality(info: MusicInfo, meta: MetaMusic) -> MusicInfo:
        """将本地文件的实际音频参数合并到远端音乐身份识别结果。"""
        for key in ("audio_format", "audio_lossless", "bit_depth", "sample_rate", "bitrate"):
            value = getattr(meta, key, None)
            if value is not None:
                setattr(info, key, value)
        return info

    @staticmethod
    def _clear_music_identity(meta: MetaMusic) -> MetaMusic:
        """复制音乐元数据并清除远程身份，供直查失败后按要素重新匹配。"""
        clean_meta = MetaMusic.from_dict(meta.to_dict())
        clean_meta.media_source = None
        clean_meta.media_id = None
        return clean_meta

    @staticmethod
    def _is_remote_music_info(info: Optional[MusicInfo]) -> bool:
        """判断音乐识别结果是否携带可复用的远程身份。"""
        return bool(info and info.media_source and info.media_id)

    def _recognize_musicbrainz_recording(
            self,
            meta: MetaMusic,
            recording_id: str,
    ) -> Optional[MusicInfo]:
        """按已知 MusicBrainz Recording ID 直接读取单曲详情。"""
        identity_meta = MetaMusic.from_dict(meta.to_dict())
        identity_meta.media_source = MediaSource.MusicBrainz
        identity_meta.media_id = recording_id
        return self.recognize_music_from_source(
            media_source=MediaSource.MusicBrainz,
            meta=identity_meta,
            media_id=recording_id,
            music_type=MUSIC_ENTITY_RECORDING,
        )

    async def _async_recognize_musicbrainz_recording(
            self,
            meta: MetaMusic,
            recording_id: str,
    ) -> Optional[MusicInfo]:
        """异步按已知 MusicBrainz Recording ID 直接读取单曲详情。"""
        identity_meta = MetaMusic.from_dict(meta.to_dict())
        identity_meta.media_source = MediaSource.MusicBrainz
        identity_meta.media_id = recording_id
        return await self.async_recognize_music_from_source(
            media_source=MediaSource.MusicBrainz,
            meta=identity_meta,
            media_id=recording_id,
            music_type=MUSIC_ENTITY_RECORDING,
        )

    def _recognize_music_meta_tier(
            self,
            meta: Optional[MetaMusic],
            media_source: Optional[MediaSource],
            tier_name: str,
    ) -> Optional[MusicInfo]:
        """识别单个音乐元数据证据层，标签中的 MBID 优先直查。"""
        if not meta:
            return None
        normalized_source = normalize_media_source(media_source)
        search_meta = meta
        if meta.media_source == MediaSource.MusicBrainz and meta.media_id:
            if normalized_source in (None, MediaSource.MusicBrainz):
                direct = self._recognize_musicbrainz_recording(
                    meta=meta,
                    recording_id=str(meta.media_id),
                )
                if self._is_remote_music_info(direct):
                    logger.info(f"音乐识别命中{tier_name}层 MusicBrainz ID 直查")
                    return direct
            search_meta = self._clear_music_identity(meta)
        if not search_meta.title:
            return None
        result = self.recognize_media(
            meta=search_meta,
            media_source=media_source,
            music_type=MUSIC_ENTITY_RECORDING,
        )
        if self._is_remote_music_info(result):
            logger.info(f"音乐识别命中{tier_name}层：{result.title}")
            return result
        return None

    async def _async_recognize_music_meta_tier(
            self,
            meta: Optional[MetaMusic],
            media_source: Optional[MediaSource],
            tier_name: str,
    ) -> Optional[MusicInfo]:
        """异步识别单个音乐元数据证据层，标签中的 MBID 优先直查。"""
        if not meta:
            return None
        normalized_source = normalize_media_source(media_source)
        search_meta = meta
        if meta.media_source == MediaSource.MusicBrainz and meta.media_id:
            if normalized_source in (None, MediaSource.MusicBrainz):
                direct = await self._async_recognize_musicbrainz_recording(
                    meta=meta,
                    recording_id=str(meta.media_id),
                )
                if self._is_remote_music_info(direct):
                    logger.info(f"音乐识别命中{tier_name}层 MusicBrainz ID 直查")
                    return direct
            search_meta = self._clear_music_identity(meta)
        if not search_meta.title:
            return None
        result = await self.async_recognize_media(
            meta=search_meta,
            media_source=media_source,
            music_type=MUSIC_ENTITY_RECORDING,
        )
        if self._is_remote_music_info(result):
            logger.info(f"音乐识别命中{tier_name}层：{result.title}")
            return result
        return None

    def _music_album_dir_fallback(
            self,
            path: Union[str, Path],
    ) -> Optional[MusicInfo]:
        """单曲识别无远端身份时，查找所在目录专辑匹配中属于当前文件的结果。"""
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            return None
        try:
            matched = self.recognize_music_album_directory(file_path.parent)
        except Exception as err:
            logger.debug(f"专辑目录匹配失败：{file_path.parent} - {err}")
            return None
        return matched.get(str(file_path.resolve()))

    async def _async_music_album_dir_fallback(
            self,
            path: Union[str, Path],
    ) -> Optional[MusicInfo]:
        """异步查找所在目录专辑匹配中属于当前文件的结果。"""
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            return None
        try:
            matched = await self.async_recognize_music_album_directory(
                file_path.parent
            )
        except Exception as err:
            logger.debug(f"专辑目录匹配失败：{file_path.parent} - {err}")
            return None
        return matched.get(str(file_path.resolve()))

    @classmethod
    def _directory_audio_files(cls, directory: Path) -> list[Path]:
        """收集专辑目录及其一级碟片子目录中的音频文件。"""
        files: list[Path] = []

        def collect(current: Path) -> None:
            """收集单层目录中的可见音频文件。"""
            try:
                entries = sorted(current.iterdir())
            except OSError:
                return
            files.extend(
                item for item in entries
                if not item.name.startswith(".")
                and item.is_file()
                and item.suffix.lower() in get_chain_runtime_config_snapshot().audio_extensions
            )

        collect(directory)
        try:
            subdirectories = sorted(
                item for item in directory.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            )
        except OSError:
            subdirectories = []
        for subdirectory in subdirectories:
            collect(subdirectory)
        return files

    @staticmethod
    def _album_directory_signature(
            directory: Path,
            files: list[Path],
    ) -> tuple[str, ...]:
        """按相对音频路径生成目录匹配缓存签名。"""
        return tuple(str(path.relative_to(directory)).casefold() for path in files)

    @staticmethod
    def _align_music_album_tracks(
            files: list[Path],
            metas: list[MetaMusic],
            tracks: list[MusicInfo],
    ) -> dict[Path, MusicInfo]:
        """优先按碟号和曲序把专辑曲目对位到本地文件。"""
        matched: dict[Path, MusicInfo] = {}
        used: set[tuple[int, int]] = set()
        by_position = {
            (track.disc_number or 1, track.track_number): track
            for track in tracks if track.track_number
        }
        pending: list[tuple[Path, MetaMusic]] = []
        for file, meta in zip(files, metas):
            key = (meta.disc_number or 1, meta.track_number or 0)
            track = by_position.get(key) if meta.track_number else None
            if track and key not in used:
                matched[file] = track
                used.add(key)
            else:
                pending.append((file, meta))
        remaining = [
            track for track in tracks
            if (track.disc_number or 1, track.track_number or 0) not in used
        ]
        pending.sort(key=lambda item: (item[1].disc_number or 1, item[0].name.casefold()))
        for (file, _), track in zip(pending, remaining):
            matched[file] = track
        return matched

    def _match_music_album_directory(
            self,
            directory: Path,
            files: list[Path],
    ) -> dict[str, MusicInfo]:
        """同步汇总本地专辑证据并委托 MusicBrainz 来源链匹配。"""
        metas = AudioMetadataHelper.read_many(files)
        album_meta = MetaMusic.from_album_context(directory.name, metas)
        album = MusicBrainzChain().match_music_album(album_meta, metas)
        if not album or not album.tracks:
            return {}
        return {
            str(file.resolve()): info
            for file, info in self._align_music_album_tracks(
                files, metas, album.tracks
            ).items()
        }

    async def _async_match_music_album_directory(
            self,
            directory: Path,
            files: list[Path],
    ) -> dict[str, MusicInfo]:
        """异步汇总本地专辑证据并委托 MusicBrainz 来源链匹配。"""
        metas = await run_in_threadpool(AudioMetadataHelper.read_many, files)
        album_meta = MetaMusic.from_album_context(directory.name, metas)
        album = await MusicBrainzChain().async_match_music_album(album_meta, metas)
        if not album or not album.tracks:
            return {}
        return {
            str(file.resolve()): info
            for file, info in self._align_music_album_tracks(
                files, metas, album.tracks
            ).items()
        }

    def recognize_music_album_directory(
            self,
            path: Union[str, Path],
    ) -> dict[str, MusicInfo]:
        """按目录级线索批量识别整张专辑并返回文件到曲目的映射。"""
        directory = Path(path)
        if not directory.is_dir():
            return {}
        files = self._directory_audio_files(directory)
        if len(files) < self._album_match_min_files:
            return {}
        key = str(directory)
        signature = self._album_directory_signature(directory, files)
        cached = self._album_dir_cache.get(key)
        if cached and cached[0] == signature:
            return self._simplify_recognized_music_mapping(cached[1])
        matched = self._match_music_album_directory(directory, files)
        if len(self._album_dir_cache) >= self._album_dir_cache_max:
            self._album_dir_cache.clear()
        self._album_dir_cache[key] = signature, matched
        return self._simplify_recognized_music_mapping(matched)

    async def async_recognize_music_album_directory(
            self,
            path: Union[str, Path],
    ) -> dict[str, MusicInfo]:
        """异步按目录级线索批量识别整张专辑。"""
        directory = Path(path)
        if not directory.is_dir():
            return {}
        files = await run_in_threadpool(self._directory_audio_files, directory)
        if len(files) < self._album_match_min_files:
            return {}
        key = str(directory)
        signature = self._album_directory_signature(directory, files)
        cached = self._album_dir_cache.get(key)
        if cached and cached[0] == signature:
            return self._simplify_recognized_music_mapping(cached[1])
        matched = await self._async_match_music_album_directory(directory, files)
        if len(self._album_dir_cache) >= self._album_dir_cache_max:
            self._album_dir_cache.clear()
        self._album_dir_cache[key] = signature, matched
        return self._simplify_recognized_music_mapping(matched)

    def recognize_music_by_path(
            self,
            path: Union[str, Path],
            media_source: Optional[MediaSource] = None,
    ) -> Tuple[MetaMusic, MusicInfo]:
        """按指纹、文件标签、文件名三级顺序识别本地音乐。"""
        meta, tag_meta, filename_meta = AudioMetadataHelper.read_evidence(Path(path))
        info = None
        normalized_source = normalize_media_source(media_source)
        if normalized_source in (None, MediaSource.MusicBrainz):
            recording_id = AcoustIdChain().identify_music_by_fingerprint(path)
            if recording_id:
                info = self._recognize_musicbrainz_recording(meta, recording_id)
                if self._is_remote_music_info(info):
                    logger.info("音乐识别命中 AcoustID 指纹层，已跳过标签和文件名识别")
        if not self._is_remote_music_info(info):
            info = self._recognize_music_meta_tier(
                meta=tag_meta,
                media_source=media_source,
                tier_name="文件标签",
            )
        if not self._is_remote_music_info(info):
            info = self._recognize_music_meta_tier(
                meta=filename_meta,
                media_source=media_source,
                tier_name="文件名",
            )
        result = self._merge_music_audio_quality(
            info or self._music_info_from_path_meta(meta), meta
        )
        if not result.media_source and media_source in (
                None, MediaSource.MusicBrainz
        ):
            # 单曲搜索未命中时，按所在目录做专辑级匹配兑底
            matched = self._music_album_dir_fallback(path)
            if matched:
                result = self._merge_music_audio_quality(matched, meta)
        return meta, self._simplify_recognized_music_info(result)

    async def async_recognize_music_by_path(
            self,
            path: Union[str, Path],
            media_source: Optional[MediaSource] = None,
    ) -> Tuple[MetaMusic, MusicInfo]:
        """异步按指纹、文件标签、文件名三级顺序识别本地音乐。"""
        meta, tag_meta, filename_meta = await run_in_threadpool(
            AudioMetadataHelper.read_evidence,
            Path(path),
        )
        info = None
        normalized_source = normalize_media_source(media_source)
        if normalized_source in (None, MediaSource.MusicBrainz):
            recording_id = await AcoustIdChain().async_identify_music_by_fingerprint(path)
            if recording_id:
                info = await self._async_recognize_musicbrainz_recording(
                    meta,
                    recording_id,
                )
                if self._is_remote_music_info(info):
                    logger.info("音乐识别命中 AcoustID 指纹层，已跳过标签和文件名识别")
        if not self._is_remote_music_info(info):
            info = await self._async_recognize_music_meta_tier(
                meta=tag_meta,
                media_source=media_source,
                tier_name="文件标签",
            )
        if not self._is_remote_music_info(info):
            info = await self._async_recognize_music_meta_tier(
                meta=filename_meta,
                media_source=media_source,
                tier_name="文件名",
            )
        result = self._merge_music_audio_quality(
            info or self._music_info_from_path_meta(meta), meta
        )
        if not result.media_source and media_source in (
                None, MediaSource.MusicBrainz
        ):
            # 单曲搜索未命中时，按所在目录做专辑级匹配兑底
            matched = await self._async_music_album_dir_fallback(path)
            if matched:
                result = self._merge_music_audio_quality(matched, meta)
        return meta, self._simplify_recognized_music_info(result)

    def _is_music_path_request(self, path: str, media_source: Optional[MediaSource]) -> bool:
        """路径识别请求是否属于音乐：音频后缀文件或显式指定音乐数据源。"""
        return self.is_audio_path(path) or is_music_media_source(media_source)

    def recognize_by_path(
            self,
            path: str,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            obtain_images: bool = False,
    ) -> Optional[Context]:
        """
        根据文件路径识别媒体信息，影视与音乐统一入口

        :param path: 文件路径
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param obtain_images: 是否补充图片
        :return: 识别上下文
        """
        logger.info(f"开始识别媒体信息，文件：{path} ...")
        # 音频文件直接在本链完成标签读取、搜索匹配与专辑目录兜底，封面等图片由刮削环节补充
        if self._is_music_path_request(path, media_source):
            music_meta, music_info = self.recognize_music_by_path(
                path, media_source=media_source
            )
            return Context(meta_info=music_meta, media_info=music_info)
        file_path = Path(path)
        # 元数据
        file_meta = MetaInfoPath(file_path)
        mediainfo = self._recognize_with_fallback_by_meta(
            metainfo=file_meta,
            media_source=media_source,
            episode_group=episode_group,
            obtain_images=obtain_images,
        )
        if not mediainfo:
            logger.warn(f"{path} 未识别到媒体信息")
            return Context(meta_info=file_meta)
        # 返回上下文
        return Context(meta_info=file_meta, media_info=mediainfo)

    def search(
        self, title: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Tuple[Optional[MetaBase], List[MediaInfo]]:
        """
        搜索媒体/人物信息

        :param title: 搜索内容
        :param media_source: 请求级搜索数据源
        :return: 识别元数据，媒体信息列表
        """
        # 提取要素
        mtype, key_word, season_num, episode_num, year, content = (
            title_rules.parse_search_keyword(title)
        )
        # 识别
        meta = MetaInfo(content)
        if not meta.name:
            meta.cn_name = content
        # 合并信息
        if mtype:
            meta.type = mtype
        if season_num:
            meta.begin_season = season_num
        if episode_num:
            meta.begin_episode = episode_num
        if year:
            meta.year = year
        # 开始搜索
        logger.info(f"开始搜索媒体信息：{meta.name}")
        medias: Optional[List[MediaInfo]] = self.search_medias(meta=meta, media_source=media_source)
        if not medias:
            logger.warn(f"{meta.name} 没有找到对应的媒体信息！")
            return meta, []
        logger.info(f"{content} 搜索到 {len(medias)} 条相关媒体信息")
        # 识别的元数据，媒体信息列表
        return meta, medias

    def convert_media_identity(
            self,
            target_source: MediaSource,
            media_source: MediaSource,
            media_id: str,
            mtype: Optional[MediaType] = None,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        使用统一媒体身份在影视来源之间转换，返回目标来源的原始详情。

        :param target_source: 目标媒体来源
        :param media_source: 当前媒体来源
        :param media_id: 当前来源原生 ID
        :param mtype: 可选媒体类型
        :param season: 可选季号，用于电视剧年份匹配
        :return: 目标来源详情；身份无效或转换组合不受支持时返回 None
        """
        target_source = normalize_media_source(target_source)
        media_source, media_id = resolve_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        if not target_source or not media_source or not media_id:
            return None

        if target_source == MediaSource.TMDB and media_source == MediaSource.Douban:
            source_info = self.douban_info(doubanid=media_id, mtype=mtype)
            if not source_info:
                return None
            if source_info.get("original_title"):
                meta = MetaInfo(title=source_info.get("title"))
                original_meta = MetaInfo(title=source_info.get("original_title"))
            else:
                original_meta = meta = MetaInfo(title=source_info.get("title"))
            if source_info.get("year"):
                meta.year = source_info.get("year")
            meta.type = (
                source_info.get("media_type")
                if isinstance(source_info.get("media_type"), MediaType)
                else MediaType.MOVIE if source_info.get("type") == "movie" else MediaType.TV
            )
            target_info = self._match_tmdb_with_names(
                meta_names=list(dict.fromkeys(
                    name for name in (original_meta.name, meta.cn_name, meta.en_name) if name
                )),
                year=meta.year,
                mtype=mtype or meta.type,
                season=season if season is not None else meta.begin_season,
            )
            if target_info:
                target_info["season"] = season if season is not None else meta.begin_season
            return target_info

        if target_source == MediaSource.TMDB and media_source == MediaSource.Bangumi:
            if not media_id.isdigit():
                return None
            source_info = self.bangumi_info(bangumiid=int(media_id))
            if not source_info:
                return None
            if source_info.get("name_cn"):
                meta = MetaInfo(title=source_info.get("name"))
                localized_meta = MetaInfo(title=source_info.get("name_cn"))
            else:
                localized_meta = meta = MetaInfo(title=source_info.get("name"))
            return self._match_tmdb_with_names(
                meta_names=list(dict.fromkeys(
                    name for name in (localized_meta.name, meta.name) if name
                )),
                year=self._extract_year_from_bangumi(source_info),
                mtype=mtype or MediaInfo.get_bangumi_media_type(source_info),
                season=season if season is not None else meta.begin_season,
            )

        if target_source == MediaSource.Douban and media_source == MediaSource.TMDB:
            if not media_id.isdigit():
                return None
            source_info = self.tmdb_info(
                tmdbid=int(media_id),
                mtype=mtype,
            )
            if not source_info:
                return None
            return self.match_doubaninfo(
                name=source_info.get("title") or source_info.get("name"),
                year=self._extract_year_from_tmdb(source_info, season),
                mtype=mtype,
                imdbid=source_info.get("external_ids", {}).get("imdb_id"),
            )

        if target_source == MediaSource.Douban and media_source == MediaSource.Bangumi:
            if not media_id.isdigit():
                return None
            source_info = self.bangumi_info(bangumiid=int(media_id))
            if not source_info:
                return None
            meta = MetaInfo(title=source_info.get("name_cn") or source_info.get("name"))
            return self.match_doubaninfo(
                name=meta.name,
                year=self._extract_year_from_bangumi(source_info),
                mtype=mtype or MediaInfo.get_bangumi_media_type(source_info),
                season=season if season is not None else meta.begin_season,
            )
        event_data = _SchemaMediaRecognizeConvertEventData(
            media_source=media_source,
            media_id=media_id,
            target_media_source=target_source,
        )
        event = eventmanager.send_event(
            ChainEventType.MediaRecognizeConvert, event_data,
        )
        return event_data.media_dict if event and event_data.media_dict else None


    @staticmethod
    async def async_select_recognize_source(
            log_name: str, log_context: str, native_fn, plugin_fn,
            is_recognized=None,
            plugin_event: ChainEventType = ChainEventType.NameRecognize,
    ) -> Optional[MediaInfo]:
        """
        选择识别模式，插件优先或原生优先（异步版本）

        :param log_name: 用于日志“标题：...”处的名称（如 file_path.name 或 title）
        :param log_context: 用于日志“未识别到...的媒体信息”处的上下文（如 path 或 title）
        :param native_fn: 原生识别函数
        :param plugin_fn: 插件识别函数
        :param is_recognized: 判定识别结果是否有效的谓词，语义同同步版本
        :param plugin_event: 辅助识别对应的链式事件类型，音乐使用音乐名称识别事件
        """
        if is_recognized is None:
            is_recognized = lambda result: bool(result)
        mediainfo = None
        plugin_available = eventmanager.check(plugin_event)
        if get_chain_runtime_config_snapshot().recognize_plugin_first and plugin_available:
            # 插件优先
            logger.info(f"插件优先模式已开启。请求辅助识别，标题：{log_name} ...")
            helped = await plugin_fn()
            if is_recognized(helped):
                mediainfo = helped
            else:
                logger.info(
                    f"辅助识别未识别到 {log_context} 的媒体信息，尝试使用原生识别"
                )
                mediainfo = await native_fn()
                # 辅助结果不采信时保留原生兜底，避免丢失已有识别结果（音乐原生兜底恒非空）
                if helped and not mediainfo:
                    mediainfo = helped
        else:
            # 原生优先
            logger.info(f"识别标题：{log_name} ...")
            mediainfo = await native_fn()
            if not is_recognized(mediainfo) and plugin_available:
                logger.info(
                    f"原生识别未识别到 {log_context} 的媒体信息，尝试使用辅助识别"
                )
                helped = await plugin_fn()
                if is_recognized(helped):
                    mediainfo = helped
        return mediainfo

    async def async_recognize_by_meta(
            self,
            metainfo: MetaBase,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            obtain_images: bool = False,
            mtype: Optional[MediaType] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        根据主副标题识别媒体信息（异步版本）

        :param metainfo: 标题解析元数据
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param obtain_images: 是否补充图片
        :param mtype: 上游已确定的媒体类型
        :param music_type: 音乐实体类型，用于约束显式音乐身份及插件结果
        :return: 统一媒体信息
        """
        mediainfo = await self._async_recognize_with_fallback_by_meta(
            metainfo=metainfo,
            mtype=mtype,
            media_source=media_source,
            episode_group=episode_group,
            obtain_images=obtain_images,
            music_type=music_type,
        )
        if not mediainfo:
            logger.warn(f"{metainfo.title} 未识别到媒体信息")
        return mediainfo

    async def _async_recognize_with_fallback_by_meta(
            self,
            metainfo: MetaBase,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            obtain_images: bool = False,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        异步根据标题识别媒体信息，必要时回退到辅助识别。

        :param metainfo: 标题解析元数据
        :param mtype: 上游已确定的媒体类型
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param obtain_images: 是否补充图片
        :param music_type: 音乐实体类型，用于约束显式音乐身份及插件结果
        :return: 统一媒体信息
        """
        if not metainfo:
            return None
        title = metainfo.title
        share_meta = deepcopy(metainfo)
        # 音乐原生兜底结果无远端身份，需按是否取得身份判定，才会请求辅助识别
        is_music = mtype == MediaType.MUSIC or isinstance(metainfo, MetaMusic)
        is_recognized = (
            (lambda result: bool(result and result.media_source)) if is_music else None
        )

        async def native_recognize() -> Optional[MediaInfo]:
            """异步使用请求级数据源执行原生识别。"""
            return await self.async_recognize_media(
                meta=metainfo,
                mtype=mtype,
                media_source=media_source,
                share_meta=share_meta,
                episode_group=episode_group,
                music_type=music_type,
            )

        async def plugin_recognize() -> Optional[MediaInfo]:
            """异步执行辅助识别并保持请求级数据源约束。"""
            if is_music and not isinstance(metainfo, MetaMusic):
                return None
            return await self.async_recognize_help(
                title=title,
                org_meta=metainfo,
                share_meta=share_meta,
                media_source=media_source,
                episode_group=episode_group,
                music_type=music_type,
            )

        # 按 config 中设置的识别顺序识别，影视与音乐共用同一选择流程
        mediainfo = await self.async_select_recognize_source(
            log_name=title,
            log_context=title,
            native_fn=native_recognize,
            plugin_fn=plugin_recognize,
            is_recognized=is_recognized,
            plugin_event=(
                ChainEventType.MusicNameRecognize if is_music
                else ChainEventType.NameRecognize
            ),
        )
        if not mediainfo:
            return None
        logger.info(
            f"{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}"
        )
        if obtain_images:
            await self.async_obtain_images(mediainfo=mediainfo)
        return mediainfo

    async def async_recognize_help(
            self,
            title: str,
            org_meta: MetaBase,
            share_meta: MetaBase = None,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        请求辅助识别，返回媒体信息（异步版本）；影视与音乐共用同一流程

        :param title: 标题
        :param org_meta: 原始元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param music_type: 音乐实体类型，仅音乐辅助识别使用
        """
        # 音乐标题要素（曲名/艺术家/专辑/年份）与影视不同，走专用名称识别事件
        if isinstance(org_meta, MetaMusic):
            return await self._async_recognize_music_help(
                title=title,
                org_meta=org_meta,
                share_meta=share_meta,
                media_source=media_source,
                music_type=music_type,
            )
        # 发送请求事件，等待结果
        result: Event = await eventmanager.async_send_event(
            ChainEventType.NameRecognize,
            {
                "title": title,
            },
        )
        if not result:
            return None
        # 获取返回事件数据
        event_data = result.event_data or {}
        logger.info(f"获取到辅助识别结果：{event_data}")
        # 处理数据格式
        title, year, season_number, episode_number = None, None, None, None
        if event_data.get("name"):
            title = str(event_data["name"]).split("/")[0].strip().replace(".", " ")
        if event_data.get("year"):
            year = str(event_data["year"]).split("/")[0].strip()
        season_number = self._parse_recognize_event_number(event_data.get("season"))
        episode_number = self._parse_recognize_event_number(event_data.get("episode"))
        if not title:
            return None
        if title == "Unknown":
            return None
        if not str(year).isdigit():
            year = None
        # 结果赋值
        if title == org_meta.name and year == org_meta.year:
            logger.info(f"辅助识别与原始识别结果一致，无需重新识别媒体信息")
            return None
        logger.info(f"辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        org_meta.name = title
        org_meta.year = year
        org_meta.begin_season = season_number
        org_meta.begin_episode = episode_number
        if org_meta.begin_season is not None or org_meta.begin_episode is not None:
            org_meta.type = MediaType.TV
        # 重新识别
        return await self.async_recognize_media(
            meta=org_meta,
            media_source=media_source,
            share_meta=share_meta,
            episode_group=episode_group,
        )

    async def _async_recognize_music_help(
            self,
            title: str,
            org_meta: MetaMusic,
            share_meta: MetaBase = None,
            media_source: Optional[MediaSource] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        请求插件辅助识别音乐标题要素，并按修正后的要素重新匹配媒体信息（异步版本）

        :param title: 原始音乐标题
        :param org_meta: 原始音乐元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param media_source: 请求级识别数据源
        :param music_type: 音乐实体类型
        """
        # 发送音乐名称识别事件，等待插件返回标题要素
        result: Event = await eventmanager.async_send_event(
            ChainEventType.MusicNameRecognize,
            {
                "title": title,
                "artist": org_meta.artist,
                "album": org_meta.album,
                "year": org_meta.year,
                "music_type": music_type,
            },
        )
        if not result:
            return None
        event_data = result.event_data or {}
        logger.info(f"获取到音乐辅助识别结果：{event_data}")
        name, artist, album, year = self._parse_music_recognize_event(event_data)
        if not name:
            return None
        # 辅助识别要素与原始一致时无需重新匹配
        if (
                name == org_meta.title
                and (not artist or artist in org_meta.artists)
                and (not album or album == org_meta.album)
                and (not year or year == org_meta.year)
        ):
            logger.info("音乐辅助识别与原始识别结果一致，无需重新匹配媒体信息")
            return None
        logger.info("音乐辅助识别结果与原始识别结果不一致，重新匹配媒体信息 ...")
        new_meta = self._build_music_help_meta(
            org_meta=org_meta,
            name=name,
            artist=artist,
            album=album,
            year=year,
        )
        # 重新识别，仅采信取得远端身份的结果，否则由选择流程保留原生兜底
        mediainfo = await self.async_recognize_media(
            meta=new_meta,
            media_source=media_source,
            share_meta=share_meta,
            music_type=music_type,
        )
        return mediainfo if mediainfo and mediainfo.media_source else None

    async def async_recognize_by_path(
            self,
            path: str,
            media_source: Optional[MediaSource] = None,
            episode_group: Optional[str] = None,
            obtain_images: bool = False,
    ) -> Optional[Context]:
        """
        根据文件路径识别媒体信息，影视与音乐统一入口（异步版本）

        :param path: 文件路径
        :param media_source: 请求级识别数据源
        :param episode_group: 剧集组
        :param obtain_images: 是否补充图片
        :return: 识别上下文
        """
        logger.info(f"开始识别媒体信息，文件：{path} ...")
        # 音频文件直接在本链完成标签读取、搜索匹配与专辑目录兜底，封面等图片由刮削环节补充
        if self._is_music_path_request(path, media_source):
            music_meta, music_info = await self.async_recognize_music_by_path(
                path, media_source=media_source
            )
            return Context(meta_info=music_meta, media_info=music_info)
        file_path = Path(path)
        # 元数据
        file_meta = MetaInfoPath(file_path)
        mediainfo = await self._async_recognize_with_fallback_by_meta(
            metainfo=file_meta,
            media_source=media_source,
            episode_group=episode_group,
            obtain_images=obtain_images,
        )
        if not mediainfo:
            logger.warn(f"{path} 未识别到媒体信息")
            return Context(meta_info=file_meta)
        # 返回上下文
        return Context(meta_info=file_meta, media_info=mediainfo)

    async def async_search(
            self, title: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Tuple[Optional[MetaBase], List[MediaInfo]]:
        """
        搜索媒体/人物信息（异步版本）

        :param title: 搜索内容
        :param media_source: 请求级搜索数据源
        :return: 识别元数据，媒体信息列表
        """
        # 提取要素
        mtype, key_word, season_num, episode_num, year, content = (
            title_rules.parse_search_keyword(title)
        )
        # 识别
        meta = MetaInfo(content)
        if not meta.name:
            meta.cn_name = content
        # 合并信息
        if mtype:
            meta.type = mtype
        if season_num:
            meta.begin_season = season_num
        if episode_num:
            meta.begin_episode = episode_num
        if year:
            meta.year = year
        # 开始搜索
        logger.info(f"开始搜索媒体信息：{meta.name}")
        medias: Optional[List[MediaInfo]] = await self.async_search_medias(
            meta=meta, media_source=media_source
        )
        if not medias:
            logger.warn(f"{meta.name} 没有找到对应的媒体信息！")
            return meta, []
        logger.info(f"{content} 搜索到 {len(medias)} 条相关媒体信息")
        # 识别的元数据，媒体信息列表
        return meta, medias

    @staticmethod
    def _extract_year_from_bangumi(bangumiinfo: dict) -> Optional[str]:
        """
        从Bangumi信息中提取年份
        """
        release_date = bangumiinfo.get("date") or bangumiinfo.get("air_date")
        if release_date:
            return release_date[:4]
        return None

    @staticmethod
    def _extract_year_from_tmdb(
            tmdbinfo: dict, season: Optional[int] = None
    ) -> Optional[str]:
        """
        从TMDB信息中提取年份
        """
        year = None
        if tmdbinfo.get("release_date"):
            year = tmdbinfo["release_date"][:4]
        elif tmdbinfo.get("seasons") and season is not None:
            for seainfo in tmdbinfo["seasons"]:
                season_number = seainfo.get("season_number")
                if season_number is None:
                    continue
                air_date = seainfo.get("air_date")
                if air_date and season_number == season:
                    year = air_date[:4]
                    break
        return year

    def _match_tmdb_with_names(
            self,
            meta_names: list,
            year: Optional[str],
            mtype: MediaType,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        使用名称列表匹配TMDB信息
        """
        for name in meta_names:
            tmdbinfo = self.match_tmdbinfo(
                name=name, year=year, mtype=mtype, season=season
            )
            if tmdbinfo:
                return tmdbinfo
        return None

    async def _async_match_tmdb_with_names(
            self,
            meta_names: list,
            year: Optional[str],
            mtype: MediaType,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        使用名称列表匹配TMDB信息（异步版本）
        """
        for name in meta_names:
            tmdbinfo = await self.async_match_tmdbinfo(
                name=name, year=year, mtype=mtype, season=season
            )
            if tmdbinfo:
                return tmdbinfo
        return None

    async def async_convert_media_identity(
            self,
            target_source: MediaSource,
            media_source: MediaSource,
            media_id: str,
            mtype: Optional[MediaType] = None,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        异步使用统一媒体身份在影视来源之间转换，返回目标来源原始详情。

        :param target_source: 目标媒体来源
        :param media_source: 当前媒体来源
        :param media_id: 当前来源原生 ID
        :param mtype: 可选媒体类型
        :param season: 可选季号，用于电视剧年份匹配
        :return: 目标来源详情；身份无效或转换组合不受支持时返回 None
        """
        target_source = normalize_media_source(target_source)
        media_source, media_id = resolve_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        if not target_source or not media_source or not media_id:
            return None

        if target_source == MediaSource.TMDB and media_source == MediaSource.Douban:
            source_info = await self.async_douban_info(doubanid=media_id, mtype=mtype)
            if not source_info:
                return None
            if source_info.get("original_title"):
                meta = MetaInfo(title=source_info.get("title"))
                original_meta = MetaInfo(title=source_info.get("original_title"))
            else:
                original_meta = meta = MetaInfo(title=source_info.get("title"))
            if source_info.get("year"):
                meta.year = source_info.get("year")
            meta.type = (
                source_info.get("media_type")
                if isinstance(source_info.get("media_type"), MediaType)
                else MediaType.MOVIE if source_info.get("type") == "movie" else MediaType.TV
            )
            target_info = await self._async_match_tmdb_with_names(
                meta_names=list(dict.fromkeys(
                    name for name in (original_meta.name, meta.cn_name, meta.en_name) if name
                )),
                year=meta.year,
                mtype=mtype or meta.type,
                season=season if season is not None else meta.begin_season,
            )
            if target_info:
                target_info["season"] = season if season is not None else meta.begin_season
            return target_info

        if target_source == MediaSource.TMDB and media_source == MediaSource.Bangumi:
            if not media_id.isdigit():
                return None
            source_info = await self.async_bangumi_info(bangumiid=int(media_id))
            if not source_info:
                return None
            if source_info.get("name_cn"):
                meta = MetaInfo(title=source_info.get("name"))
                localized_meta = MetaInfo(title=source_info.get("name_cn"))
            else:
                localized_meta = meta = MetaInfo(title=source_info.get("name"))
            return await self._async_match_tmdb_with_names(
                meta_names=list(dict.fromkeys(
                    name for name in (localized_meta.name, meta.name) if name
                )),
                year=self._extract_year_from_bangumi(source_info),
                mtype=mtype or MediaInfo.get_bangumi_media_type(source_info),
                season=season if season is not None else meta.begin_season,
            )

        if target_source == MediaSource.Douban and media_source == MediaSource.TMDB:
            if not media_id.isdigit():
                return None
            source_info = await self.async_tmdb_info(
                tmdbid=int(media_id),
                mtype=mtype,
            )
            if not source_info:
                return None
            return await self.async_match_doubaninfo(
                name=source_info.get("title") or source_info.get("name"),
                year=self._extract_year_from_tmdb(source_info, season),
                mtype=mtype,
                imdbid=source_info.get("external_ids", {}).get("imdb_id"),
            )

        if target_source == MediaSource.Douban and media_source == MediaSource.Bangumi:
            if not media_id.isdigit():
                return None
            source_info = await self.async_bangumi_info(bangumiid=int(media_id))
            if not source_info:
                return None
            meta = MetaInfo(title=source_info.get("name_cn") or source_info.get("name"))
            return await self.async_match_doubaninfo(
                name=meta.name,
                year=self._extract_year_from_bangumi(source_info),
                mtype=mtype or MediaInfo.get_bangumi_media_type(source_info),
                season=season if season is not None else meta.begin_season,
            )
        event_data = _SchemaMediaRecognizeConvertEventData(
            media_source=media_source,
            media_id=media_id,
            target_media_source=target_source,
        )
        event = await eventmanager.async_send_event(
            ChainEventType.MediaRecognizeConvert, event_data,
        )
        return event_data.media_dict if event and event_data.media_dict else None
