"""媒体原生路由与同步异步识别回退 owner。"""

from copy import deepcopy
from typing import Any, Optional, cast

from app.chain.base import ChainBase
from app.chain.media.contract import _MediaOwnerBase
from app.domain.context import (
    MediaInfo,
)
from app.domain.media import is_music_media_source
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.runtime.cache import async_fresh, fresh
from app.runtime.log import logger
from app.schemas.types import (
    MUSIC_ENTITY_RECORDING,
    ChainEventType,
    MediaSource,
    MediaType,
)


class MediaRecognitionOwner(_MediaOwnerBase):
    """媒体原生路由与同步异步识别回退 owner。"""

    def _run_native_media_recognize(
        self,
        module_kwargs: dict[str, Any],
        cache: bool,
    ) -> Optional[MediaInfo]:
        """统一同步媒体识别路由，未指定来源时影视和音乐只使用各自主数据源。"""
        meta = module_kwargs.get("meta")
        mtype = module_kwargs.get("mtype")
        media_source = module_kwargs.get("media_source")
        if isinstance(meta, MetaMusic) or mtype == MediaType.MUSIC or is_music_media_source(media_source):
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
                    return cast(
                        Optional[MediaInfo],
                        self.recognize_music_from_source(**recognize_kwargs),
                    )
            if isinstance(meta, MetaMusic):
                return cast(
                    Optional[MediaInfo],
                    self.recognize_music_from_source(
                        media_source=self._music_primary_source,
                        meta=meta,
                        cache=cache,
                        music_type=MUSIC_ENTITY_RECORDING,
                    ),
                )
            return None
        if not media_source and isinstance(meta, MetaBase):
            module_kwargs = {
                **module_kwargs,
                "media_source": self._video_primary_source,
            }
        return ChainBase._run_native_media_recognize(
            cast(ChainBase, self), module_kwargs, cache
        )

    async def _async_run_native_media_recognize(
        self,
        module_kwargs: dict[str, Any],
        cache: bool,
    ) -> Optional[MediaInfo]:
        """统一异步媒体识别路由，未指定来源时影视和音乐只使用各自主数据源。"""
        meta = module_kwargs.get("meta")
        mtype = module_kwargs.get("mtype")
        media_source = module_kwargs.get("media_source")
        if isinstance(meta, MetaMusic) or mtype == MediaType.MUSIC or is_music_media_source(media_source):
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
                    return cast(
                        Optional[MediaInfo],
                        await self.async_recognize_music_from_source(**recognize_kwargs),
                    )
            if isinstance(meta, MetaMusic):
                return cast(
                    Optional[MediaInfo],
                    await self.async_recognize_music_from_source(
                        media_source=self._music_primary_source,
                        meta=meta,
                        cache=cache,
                        music_type=MUSIC_ENTITY_RECORDING,
                    ),
                )
            return None
        if not media_source and isinstance(meta, MetaBase):
            module_kwargs = {
                **module_kwargs,
                "media_source": self._video_primary_source,
            }
        return await ChainBase._async_run_native_media_recognize(
            cast(ChainBase, self), module_kwargs, cache
        )

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
        is_recognized = (lambda result: bool(result and result.media_source)) if is_music else None

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
                ChainEventType.MusicNameRecognize
                if is_music
                else ChainEventType.NameRecognize
            ),
        )
        if not mediainfo:
            return None
        # 识别成功
        logger.info(f"{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}")
        if obtain_images:
            self.obtain_images(mediainfo=mediainfo)
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
        is_recognized = (lambda result: bool(result and result.media_source)) if is_music else None

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
                ChainEventType.MusicNameRecognize
                if is_music
                else ChainEventType.NameRecognize
            ),
        )
        if not mediainfo:
            return None
        logger.info(f"{title} 识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year}")
        if obtain_images:
            await self.async_obtain_images(mediainfo=mediainfo)
        return mediainfo
