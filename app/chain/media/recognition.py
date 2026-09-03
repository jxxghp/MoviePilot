"""媒体原生路由与同步异步识别回退 owner。"""

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, cast

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


class _NativeRecognitionAction(Enum):
    """标识媒体识别入口下一步需要执行的真实 I/O 动作。"""

    MUSIC = "music"
    VIDEO = "video"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class _NativeRecognitionPlan:
    """冻结同步与异步原生识别入口共用的路由和调用参数。"""

    action: _NativeRecognitionAction
    kwargs: Mapping[str, Any]
    refresh_cache: bool = False


@dataclass(frozen=True, slots=True)
class _MetaRecognitionRequest:
    """冻结一次按标题识别所需的元数据、副本和来源决策。"""

    title: str
    metainfo: MetaBase
    share_meta: MetaBase
    mtype: Optional[MediaType]
    media_source: Optional[MediaSource]
    episode_group: Optional[str]
    music_type: Optional[str]
    is_music: bool

    @property
    def plugin_event(self) -> ChainEventType:
        """返回与媒体类型匹配的辅助识别事件。"""
        return (
            ChainEventType.MusicNameRecognize
            if self.is_music
            else ChainEventType.NameRecognize
        )


class MediaRecognitionOwner(_MediaOwnerBase):
    """媒体原生路由与同步异步识别回退 owner。"""

    def _native_recognition_plan(
        self,
        module_kwargs: Mapping[str, Any],
        cache: bool,
    ) -> _NativeRecognitionPlan:
        """把媒体类型、显式来源和默认来源归并为唯一原生识别计划。"""
        meta = module_kwargs.get("meta")
        mtype = module_kwargs.get("mtype")
        media_source = module_kwargs.get("media_source")
        if (
            isinstance(meta, MetaMusic)
            or mtype == MediaType.MUSIC
            or is_music_media_source(media_source)
        ):
            if media_source:
                recognize_kwargs: dict[str, Any] = {
                    "media_source": media_source,
                    "meta": meta if isinstance(meta, MetaMusic) else None,
                    "media_id": module_kwargs.get("media_id"),
                    "cache": cache,
                }
                if "music_type" in module_kwargs:
                    recognize_kwargs["music_type"] = module_kwargs["music_type"]
                return _NativeRecognitionPlan(
                    action=_NativeRecognitionAction.MUSIC,
                    kwargs=recognize_kwargs,
                    refresh_cache=not cache,
                )
            if isinstance(meta, MetaMusic):
                return _NativeRecognitionPlan(
                    action=_NativeRecognitionAction.MUSIC,
                    kwargs={
                        "media_source": self._music_primary_source,
                        "meta": meta,
                        "cache": cache,
                        "music_type": MUSIC_ENTITY_RECORDING,
                    },
                )
            return _NativeRecognitionPlan(
                action=_NativeRecognitionAction.NONE,
                kwargs={},
            )
        video_kwargs = dict(module_kwargs)
        if not media_source and isinstance(meta, MetaBase):
            video_kwargs["media_source"] = self._video_primary_source
        return _NativeRecognitionPlan(
            action=_NativeRecognitionAction.VIDEO,
            kwargs=video_kwargs,
        )

    def _run_native_media_recognize(
        self,
        module_kwargs: dict[str, Any],
        cache: bool,
    ) -> Optional[MediaInfo]:
        """统一同步媒体识别路由，未指定来源时影视和音乐只使用各自主数据源。"""
        plan = MediaRecognitionOwner._native_recognition_plan(
            self, module_kwargs, cache
        )
        if plan.action is _NativeRecognitionAction.NONE:
            return None
        if plan.action is _NativeRecognitionAction.MUSIC:
            with fresh(plan.refresh_cache):
                return cast(
                    Optional[MediaInfo],
                    self.recognize_music_from_source(**plan.kwargs),
                )
        return ChainBase._run_native_media_recognize(
            cast(ChainBase, self), dict(plan.kwargs), cache
        )

    async def _async_run_native_media_recognize(
        self,
        module_kwargs: dict[str, Any],
        cache: bool,
    ) -> Optional[MediaInfo]:
        """统一异步媒体识别路由，未指定来源时影视和音乐只使用各自主数据源。"""
        plan = MediaRecognitionOwner._native_recognition_plan(
            self, module_kwargs, cache
        )
        if plan.action is _NativeRecognitionAction.NONE:
            return None
        if plan.action is _NativeRecognitionAction.MUSIC:
            async with async_fresh(plan.refresh_cache):
                return cast(
                    Optional[MediaInfo],
                    await self.async_recognize_music_from_source(**plan.kwargs),
                )
        return await ChainBase._async_run_native_media_recognize(
            cast(ChainBase, self), dict(plan.kwargs), cache
        )

    @staticmethod
    def _meta_recognition_request(
        metainfo: MetaBase,
        *,
        mtype: Optional[MediaType],
        media_source: Optional[MediaSource],
        episode_group: Optional[str],
        music_type: Optional[str],
    ) -> _MetaRecognitionRequest:
        """为同步与异步标题识别冻结相同的元数据副本和来源语义。"""
        return _MetaRecognitionRequest(
            title=metainfo.title,
            metainfo=metainfo,
            share_meta=deepcopy(metainfo),
            mtype=mtype,
            media_source=media_source,
            episode_group=episode_group,
            music_type=music_type,
            is_music=mtype == MediaType.MUSIC or isinstance(metainfo, MetaMusic),
        )

    @staticmethod
    def _has_remote_identity(result: Optional[MediaInfo]) -> bool:
        """音乐识别仅在取得远端来源身份后视为完整命中。"""
        return bool(result and result.media_source)

    @staticmethod
    def _accepted_recognition(
        request: _MetaRecognitionRequest,
        mediainfo: Optional[MediaInfo],
    ) -> Optional[MediaInfo]:
        """统一识别结果的空值判定与成功日志，图片补充留给各 I/O 外壳。"""
        if not mediainfo:
            return None
        logger.info(
            f"{request.title} 识别到媒体信息："
            f"{mediainfo.type.value} {mediainfo.title_year}"
        )
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
        request = MediaRecognitionOwner._meta_recognition_request(
            metainfo,
            mtype=mtype,
            media_source=media_source,
            episode_group=episode_group,
            music_type=music_type,
        )

        def native_recognize() -> Optional[MediaInfo]:
            """使用请求级数据源执行原生识别。"""
            return self.recognize_media(
                meta=request.metainfo,
                mtype=request.mtype,
                media_source=request.media_source,
                share_meta=request.share_meta,
                episode_group=request.episode_group,
                music_type=request.music_type,
            )

        def plugin_recognize() -> Optional[MediaInfo]:
            """执行辅助识别并保持请求级数据源约束。"""
            if request.is_music and not isinstance(request.metainfo, MetaMusic):
                return None
            return self.recognize_help(
                title=request.title,
                org_meta=request.metainfo,
                share_meta=request.share_meta,
                media_source=request.media_source,
                episode_group=request.episode_group,
                music_type=request.music_type,
            )

        # 按 config 中设置的识别顺序识别，影视与音乐共用同一选择流程
        mediainfo = self.select_recognize_source(
            log_name=request.title,
            log_context=request.title,
            native_fn=native_recognize,
            plugin_fn=plugin_recognize,
            is_recognized=(
                MediaRecognitionOwner._has_remote_identity
                if request.is_music
                else None
            ),
            plugin_event=request.plugin_event,
        )
        mediainfo = MediaRecognitionOwner._accepted_recognition(request, mediainfo)
        if mediainfo is None:
            return None
        if obtain_images:
            self.obtain_images(mediainfo=mediainfo)
        return cast(
            Optional[MediaInfo],
            self._finalize_recognition_result(mediainfo),
        )

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
        request = MediaRecognitionOwner._meta_recognition_request(
            metainfo,
            mtype=mtype,
            media_source=media_source,
            episode_group=episode_group,
            music_type=music_type,
        )

        async def native_recognize() -> Optional[MediaInfo]:
            """异步使用请求级数据源执行原生识别。"""
            return await self.async_recognize_media(
                meta=request.metainfo,
                mtype=request.mtype,
                media_source=request.media_source,
                share_meta=request.share_meta,
                episode_group=request.episode_group,
                music_type=request.music_type,
            )

        async def plugin_recognize() -> Optional[MediaInfo]:
            """异步执行辅助识别并保持请求级数据源约束。"""
            if request.is_music and not isinstance(request.metainfo, MetaMusic):
                return None
            return await self.async_recognize_help(
                title=request.title,
                org_meta=request.metainfo,
                share_meta=request.share_meta,
                media_source=request.media_source,
                episode_group=request.episode_group,
                music_type=request.music_type,
            )

        # 按 config 中设置的识别顺序识别，影视与音乐共用同一选择流程
        mediainfo = await self.async_select_recognize_source(
            log_name=request.title,
            log_context=request.title,
            native_fn=native_recognize,
            plugin_fn=plugin_recognize,
            is_recognized=(
                MediaRecognitionOwner._has_remote_identity
                if request.is_music
                else None
            ),
            plugin_event=request.plugin_event,
        )
        mediainfo = MediaRecognitionOwner._accepted_recognition(request, mediainfo)
        if mediainfo is None:
            return None
        if obtain_images:
            await self.async_obtain_images(mediainfo=mediainfo)
        return cast(
            Optional[MediaInfo],
            await self._async_finalize_recognition_result(mediainfo),
        )
