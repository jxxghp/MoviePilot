"""媒体原生路由与同步异步识别回退 owner。"""

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, cast

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.music.observation import music_recognition_needs_confirmation
from app.application.music.recognition import async_recognize_music_sources, recognize_music_sources
from app.chain.base import ChainBase
from app.chain.media.contract import _MediaOwnerBase
from app.domain.context import (
    MediaInfo,
    MusicInfo,
)
from app.domain.media import is_music_media_source, music_recognition_sources
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
    sources: tuple[MediaSource, ...] = ()


def _music_fallback_info(info: Optional[MusicInfo], meta: MetaMusic, diagnostic: dict[str, Any]) -> MusicInfo:
    """附加来源尝试记录而不污染来源缓存对象；未命中只保留本地元数据。"""
    result = deepcopy(info) if info is not None else MusicInfo.from_meta(meta)
    result.raw_data = {**(result.raw_data or {}), "recognition": diagnostic}
    return result


def _native_music_candidate(info: Optional[MusicInfo], source: MediaSource, kind: Optional[str]) -> bool:
    """回退只能采用同一来源及所请求实体类型的有效主身份。"""
    return bool(isinstance(info, MusicInfo) and info.media_id and info.media_source == source
                and (not kind or info.music_type == kind))


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
                if "music_type" in module_kwargs or (isinstance(meta, MetaMusic) and meta.music_type):
                    recognize_kwargs["music_type"] = module_kwargs.get("music_type") or getattr(meta, "music_type", None)
                return _NativeRecognitionPlan(
                    action=_NativeRecognitionAction.MUSIC,
                    kwargs=recognize_kwargs,
                    refresh_cache=not cache,
                )
            if isinstance(meta, MetaMusic):
                sources = music_recognition_sources(get_chain_runtime_config_snapshot().search_source, self._music_primary_source)
                if any((meta.musicbrainz_release_id, meta.musicbrainz_release_group_id, meta.musicbrainz_release_track_id)):
                    sources = (MediaSource.MusicBrainz,)
                return _NativeRecognitionPlan(
                    action=_NativeRecognitionAction.MUSIC,
                    kwargs={
                        "media_source": sources[0],
                        "meta": meta,
                        "cache": cache,
                        "music_type": module_kwargs.get("music_type") or meta.music_type or MUSIC_ENTITY_RECORDING,
                    },
                    sources=sources if sources != (self._music_primary_source,) else (),
                    refresh_cache=not cache,
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
        """影视使用原主来源；未绑定身份的音乐按已配置内置来源有序回退。"""
        plan = MediaRecognitionOwner._native_recognition_plan(
            self, module_kwargs, cache
        )
        if plan.action is _NativeRecognitionAction.NONE:
            return None
        if plan.action is _NativeRecognitionAction.MUSIC:
            with fresh(plan.refresh_cache):
                if plan.sources:
                    info, diagnostic = recognize_music_sources(
                        plan.sources,
                        lambda source: self.recognize_music_from_source(**{**plan.kwargs, "media_source": source}),
                        lambda item, source: _native_music_candidate(item, source, plan.kwargs.get("music_type")),
                    )
                    return cast(MediaInfo, _music_fallback_info(info, plan.kwargs["meta"], diagnostic))
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
        """异步识别使用与同步入口相同的来源计划，保留显式来源与主身份约束。"""
        plan = MediaRecognitionOwner._native_recognition_plan(
            self, module_kwargs, cache
        )
        if plan.action is _NativeRecognitionAction.NONE:
            return None
        if plan.action is _NativeRecognitionAction.MUSIC:
            async with async_fresh(plan.refresh_cache):
                if plan.sources:
                    info, diagnostic = await async_recognize_music_sources(
                        plan.sources,
                        lambda source: self.async_recognize_music_from_source(**{**plan.kwargs, "media_source": source}),
                        lambda item, source: _native_music_candidate(item, source, plan.kwargs.get("music_type")),
                    )
                    return cast(MediaInfo, _music_fallback_info(info, plan.kwargs["meta"], diagnostic))
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
        return bool(result and result.media_source and result.media_id)

    @staticmethod
    def _music_recognition_is_terminal(result: Optional[MediaInfo]) -> bool:
        """远端命中或已知歧义都结束自动层级选择，后者继续作为待确认结果返回。"""
        return MediaRecognitionOwner._has_remote_identity(result) or music_recognition_needs_confirmation(result)

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
                MediaRecognitionOwner._music_recognition_is_terminal
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
                MediaRecognitionOwner._music_recognition_is_terminal
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
