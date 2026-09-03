"""媒体识别管线 mixin。

从 ChainBase 拆出的识别域：原生模块识别路由、识别缓存回填、共享识别、
插件补充识别。方法经 MRO 解析，依赖 ChainBase 实例的 run_module/eventmanager
等协作对象。
"""
import copy
import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Generator, Optional, Protocol, TypeVar, cast

from app.application.configuration import get_configured_system_config
from app.chain._contracts import ChainRuntimeMixinHost
from app.domain.context import (
    MediaInfo,
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
)
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.runtime.cache import async_fresh, fresh
from app.runtime.events import Event
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.category import ClassificationSelection
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.schemas.types import ChainEventType, MediaSource, MediaType, SystemConfigKey

_ClassificationSubjectT = TypeVar(
    "_ClassificationSubjectT",
    MediaInfo,
    MusicInfo,
    MusicAlbumInfo,
    MusicArtistInfo,
)


class RecognitionSharePort(Protocol):
    """媒体识别链访问共享识别服务所需的最小端口。"""

    def report_recognize_share(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo | MusicInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """同步上报共享识别结果。"""
        ...

    async def async_report_recognize_share(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo | MusicInfo],
            keyword_meta: Optional[MetaBase] = None,
    ) -> bool:
        """异步上报共享识别结果。"""
        ...

    def query_recognize_share(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """同步查询共享识别结果。"""
        ...

    async def async_query_recognize_share(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType] = None,
            keyword_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """异步查询共享识别结果。"""
        ...

    def to_recognize_params(
            self,
            item: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """把共享结果转换为本地识别参数。"""
        ...


class _SystemConfigCounter(Protocol):
    """共享识别命中统计所需的最小原子计数端口。"""

    def increment(self, key: SystemConfigKey, step: int = 1) -> int:
        """原子递增指定系统配置计数。"""
        ...


_recognition_share_lock = threading.RLock()
_recognition_share_port: Optional[RecognitionSharePort] = None


def configure_recognition_share_port(
        port: RecognitionSharePort,
) -> Optional[RecognitionSharePort]:
    """装配共享识别端口，并返回旧实现供隔离环境恢复。"""
    global _recognition_share_port
    with _recognition_share_lock:
        previous = _recognition_share_port
        _recognition_share_port = port
    return previous


def reset_recognition_share_port(
        port: Optional[RecognitionSharePort] = None,
) -> None:
    """恢复指定共享识别端口；省略参数时回到未装配状态。"""
    global _recognition_share_port
    with _recognition_share_lock:
        _recognition_share_port = port


def _recognition_share_snapshot() -> RecognitionSharePort:
    """获取当前共享识别端口，未装配时稳定失败。"""
    with _recognition_share_lock:
        port = _recognition_share_port
    if port is None:
        raise RuntimeError("共享识别端口尚未由启动组合根装配")
    return port


@dataclass(frozen=True, slots=True)
class _RecognitionPlan:
    """冻结一次媒体识别请求的规范身份、模块参数和共享查询上下文。"""

    meta: Optional[MetaBase]
    mtype: Optional[MediaType]
    media_source: Optional[MediaSource | str]
    media_id: Optional[str]
    episode_group: Optional[str]
    cache: bool
    share_meta: Optional[MetaBase]
    music_type: Optional[str]
    identity_valid: bool

    def module_kwargs(self) -> dict[str, Any]:
        """生成原生模块识别所需的稳定参数。"""
        kwargs: dict[str, Any] = {
            "meta": self.meta,
            "mtype": self.mtype,
            "media_source": self.media_source,
            "media_id": self.media_id,
            "episode_group": self.episode_group,
            "cache": self.cache,
        }
        if self.music_type is not None:
            kwargs["music_type"] = self.music_type
        return kwargs

    def share_query_kwargs(self) -> dict[str, Any]:
        """生成共享识别查询参数，保持原始关键字与规范媒体类型。"""
        kwargs: dict[str, Any] = {
            "meta": self.meta,
            "mtype": self.mtype,
            "keyword_meta": self.share_meta,
        }
        if self.music_type is not None:
            kwargs["music_type"] = self.music_type
        return kwargs

    def shared_module_kwargs(
            self,
            shared_params: dict[str, Any],
    ) -> dict[str, Any]:
        """把共享识别身份投影为第二次原生模块识别参数。"""
        kwargs: dict[str, Any] = {
            "meta": self.meta,
            "mtype": shared_params.get("mtype") or self.mtype,
            "media_source": shared_params.get("media_source"),
            "media_id": shared_params.get("media_id"),
            "episode_group": self.episode_group,
            "cache": self.cache,
        }
        shared_music_type = shared_params.get("music_type") or self.music_type
        if shared_music_type is not None:
            kwargs["music_type"] = shared_music_type
        return kwargs


@dataclass(frozen=True, slots=True)
class _RecognitionOutcome:
    """表达候选结果是否终结识别，以及应保留的无身份回退结果。"""

    candidate: Optional[MediaInfo | MusicInfo]
    fallback: Optional[MediaInfo | MusicInfo]
    has_identity: bool

    @classmethod
    def decide(
            cls,
            candidate: Optional[MediaInfo | MusicInfo],
            fallback: Optional[MediaInfo | MusicInfo] = None,
    ) -> "_RecognitionOutcome":
        """按规范媒体身份决定终态，并稳定保留最早的有效回退结果。"""
        media_source, media_id = resolve_media_identity(media=candidate)
        has_identity = bool(media_source and media_id)
        return cls(
            candidate=candidate,
            fallback=fallback or (candidate if candidate and not has_identity else None),
            has_identity=has_identity,
        )

    @property
    def result(self) -> Optional[MediaInfo | MusicInfo]:
        """返回已识别终态，未命中远端身份时返回保留的回退结果。"""
        return self.candidate if self.has_identity else self.fallback

    @property
    def should_report(self) -> bool:
        """判断原生或插件终态是否需要上报共享识别。"""
        return bool(
            self.has_identity
            and self.candidate
            and not getattr(self.candidate, "recognize_cache_hit", False)
        )


class _RecognitionAction(Enum):
    """标识媒体识别纯状态机请求同步或异步外壳执行的 I/O 动作。"""

    NATIVE = auto()
    SUPPLEMENT = auto()
    REPORT = auto()
    QUERY_SHARE = auto()
    UPDATE_CACHE = auto()
    RECORD_SHARE_HIT = auto()


@dataclass(frozen=True, slots=True)
class _RecognitionStep:
    """描述媒体识别状态机的一次 I/O 请求及其稳定调用参数。"""

    action: _RecognitionAction
    kwargs: dict[str, Any]
    cache: bool = True


@dataclass(frozen=True, slots=True)
class _PluginRecognitionPlan:
    """冻结插件补充识别的事件类型、载荷与结果解析上下文。"""

    payload: dict[str, Any]
    fallback: Optional[MediaInfo | MusicInfo]
    is_music: bool
    mtype: Optional[MediaType]
    music_type: Optional[str]


class _RecognitionFinalizationOwner:
    """隔离完整识别结果的同步与异步分类收口。"""

    def _finalize_recognition_result(
        self,
        mediainfo: Optional[_ClassificationSubjectT],
        *,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> Optional[_ClassificationSubjectT]:
        """在唯一应用服务中复制并分类完整识别结果。"""
        if mediainfo is None:
            return None
        service = getattr(cast(ChainRuntimeMixinHost, self), "classification_service", None)
        if service is None:
            return mediainfo
        return cast(
            _ClassificationSubjectT,
            service.finalize(
                mediainfo,
                effective_override=effective_override,
                refresh=refresh,
            ),
        )

    async def _async_finalize_recognition_result(
        self,
        mediainfo: Optional[_ClassificationSubjectT],
        *,
        effective_override: ClassificationSelection | None = None,
        refresh: bool = False,
    ) -> Optional[_ClassificationSubjectT]:
        """通过异步应用服务复制、补充并分类完整识别结果。"""
        if mediainfo is None:
            return None
        service = getattr(cast(ChainRuntimeMixinHost, self), "classification_service", None)
        if service is None:
            return mediainfo
        return cast(
            _ClassificationSubjectT,
            await service.async_finalize(
                mediainfo,
                effective_override=effective_override,
                refresh=refresh,
            ),
        )


class RecognitionMixin:
    """为媒体 Chain 提供本地识别、共享识别和插件补充识别流程。"""

    __mixin_host_protocol__ = ChainRuntimeMixinHost
    eventmanager: Any
    _finalize_recognition_result = cast(Any, _RecognitionFinalizationOwner._finalize_recognition_result)
    _async_finalize_recognition_result = cast(Any, _RecognitionFinalizationOwner._async_finalize_recognition_result)

    def _runtime_host(self) -> ChainRuntimeMixinHost:
        """把 MRO 注入的宿主能力收窄为识别 mixin 的静态协议。"""
        return cast(ChainRuntimeMixinHost, self)

    @staticmethod
    def _build_recognition_plan(
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource | str],
            media_id: Optional[str],
            episode_group: Optional[str],
            cache: bool,
            share_meta: Optional[MetaBase],
            music_type: Optional[str],
    ) -> _RecognitionPlan:
        """规范请求级来源和显式身份，并生成同步、异步共用的识别计划。"""
        explicit_identity = media_id is not None
        requested_source = normalize_media_source(media_source) or media_source
        resolved_source, resolved_id = resolve_media_identity(
            media=meta,
            media_source=media_source,
            media_id=media_id,
        )
        identity_valid = not explicit_identity or bool(resolved_source and resolved_id)
        planned_source: Optional[MediaSource | str] = resolved_source
        if not resolved_id and requested_source is not None:
            planned_source = requested_source
            meta_source, meta_id = resolve_media_identity(media=meta)
            if meta_id and meta_source == requested_source:
                planned_source, resolved_id = meta_source, meta_id
        planned_episode_group = episode_group
        if not planned_episode_group and meta is not None:
            planned_episode_group = getattr(meta, "episode_group", None)
        planned_type = mtype
        if (
                not planned_type
                and not (planned_source and resolved_id)
                and meta
                and meta.type in (MediaType.TV, MediaType.MOVIE, MediaType.MUSIC)
        ):
            planned_type = meta.type
        return _RecognitionPlan(
            meta=meta,
            mtype=planned_type,
            media_source=planned_source,
            media_id=resolved_id,
            episode_group=planned_episode_group,
            cache=cache,
            share_meta=share_meta or meta,
            music_type=music_type,
            identity_valid=identity_valid,
        )

    def _can_use_media_recognize_share(
            self,
            meta: Optional[MetaBase],
            media_source: Optional[MediaSource | str],
            media_id: Optional[str],
    ) -> bool:
        """
        仅在名称识别场景下使用共享识别，显式ID识别不再重复回查
        """
        return bool(
            self._runtime_host().runtime_config.media_recognize_share
            and meta
            and not media_source
            and not media_id
        )

    @staticmethod
    def _snapshot_recognize_cache_meta(meta: Optional[MetaBase]) -> Optional[MetaBase]:
        """
        保存共享识别前的本地缓存关键元数据，用于共享成功后回填正缓存覆盖负缓存。
        """
        if not meta:
            return None
        return copy.deepcopy(meta)

    def _update_local_recognize_cache(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo | MusicInfo],
    ) -> None:
        """
        共享识别成功后回填本地识别缓存，避免名称负缓存导致后续重复回查共享。
        """
        if not meta or not mediainfo:
            return
        self._runtime_host().run_module(
            "update_recognize_cache",
            meta=meta,
            mediainfo=mediainfo,
        )

    async def _async_update_local_recognize_cache(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo | MusicInfo],
    ) -> None:
        """
        异步回填本地识别缓存。
        """
        if not meta or not mediainfo:
            return
        await self._runtime_host().async_run_module(
            "async_update_recognize_cache",
            meta=meta,
            mediainfo=mediainfo,
        )

    @staticmethod
    def _record_media_recognize_share_hit() -> None:
        """记录一次共享媒体识别成功命中，统计失败不影响识别结果。"""
        try:
            counter = cast(_SystemConfigCounter, get_configured_system_config())
            counter.increment(SystemConfigKey.MediaRecognizeShareCount)
        except Exception as err:
            logger.error(f"记录共享媒体识别命中次数失败：{str(err)}")

    def _run_native_media_recognize(
            self,
            module_kwargs: dict[str, Any],
            cache: bool,
    ) -> Optional[MediaInfo]:
        """执行同步原生媒体模块识别，具体媒体领域可覆写该路由钩子。"""
        with fresh(not cache):
            return cast(
                Optional[MediaInfo],
                self._runtime_host().run_module("recognize_media", **module_kwargs),
            )

    async def _async_run_native_media_recognize(
            self,
            module_kwargs: dict[str, Any],
            cache: bool,
    ) -> Optional[MediaInfo]:
        """执行异步原生媒体模块识别，具体媒体领域可覆写该路由钩子。"""
        async with async_fresh(not cache):
            return cast(
                Optional[MediaInfo],
                await self._runtime_host().async_run_module(
                    "async_recognize_media", **module_kwargs
                ),
            )

    @classmethod
    def _recognition_steps(
            cls,
            plan: _RecognitionPlan,
            use_share: bool,
    ) -> Generator[
        _RecognitionStep, Any, Optional[MediaInfo | MusicInfo]
    ]:
        """生成双 ABI 共用的识别状态机，仅把实际 I/O 留给入口外壳。"""
        mediainfo = yield _RecognitionStep(
            action=_RecognitionAction.NATIVE,
            kwargs=plan.module_kwargs(),
            cache=plan.cache,
        )
        mediainfo = yield _RecognitionStep(
            action=_RecognitionAction.SUPPLEMENT,
            kwargs={
                "meta": plan.meta,
                "mtype": plan.mtype,
                "media_source": plan.media_source,
                "media_id": plan.media_id,
                "mediainfo": mediainfo,
                "music_type": plan.music_type,
            },
        )
        outcome = _RecognitionOutcome.decide(mediainfo)
        if outcome.has_identity:
            if outcome.should_report:
                yield _RecognitionStep(
                    action=_RecognitionAction.REPORT,
                    kwargs={
                        "meta": plan.meta,
                        "mediainfo": outcome.candidate,
                        "keyword_meta": plan.share_meta,
                    },
                )
            return outcome.result

        if use_share:
            shared_cache_meta = cls._snapshot_recognize_cache_meta(plan.meta)
            shared_params = yield _RecognitionStep(
                action=_RecognitionAction.QUERY_SHARE,
                kwargs=plan.share_query_kwargs(),
            )
            if shared_params:
                mediainfo = yield _RecognitionStep(
                    action=_RecognitionAction.NATIVE,
                    kwargs=plan.shared_module_kwargs(shared_params),
                    cache=plan.cache,
                )
                outcome = _RecognitionOutcome.decide(mediainfo, outcome.fallback)
                if outcome.has_identity:
                    yield _RecognitionStep(
                        action=_RecognitionAction.UPDATE_CACHE,
                        kwargs={
                            "meta": shared_cache_meta,
                            "mediainfo": outcome.candidate,
                        },
                    )
                    yield _RecognitionStep(
                        action=_RecognitionAction.RECORD_SHARE_HIT,
                        kwargs={},
                    )
        return outcome.result

    def _run_recognition_steps(self, plan: _RecognitionPlan) -> Optional[MediaInfo]:
        """用同步端口驱动共享识别状态机。"""
        steps = self._recognition_steps(
            plan,
            self._can_use_media_recognize_share(
                plan.share_meta, plan.media_source, plan.media_id
            ),
        )
        result: Any = None
        while True:
            try:
                step = steps.send(result)
            except StopIteration as completed:
                return cast(Optional[MediaInfo], completed.value)
            if step.action == _RecognitionAction.NATIVE:
                result = self._run_native_media_recognize(step.kwargs, step.cache)
            elif step.action == _RecognitionAction.SUPPLEMENT:
                result = self._supplement_media_recognize(**step.kwargs)
            elif step.action == _RecognitionAction.REPORT:
                result = _recognition_share_snapshot().report_recognize_share(
                    **step.kwargs
                )
            elif step.action == _RecognitionAction.QUERY_SHARE:
                share_port = _recognition_share_snapshot()
                shared_item = share_port.query_recognize_share(**step.kwargs)
                result = share_port.to_recognize_params(shared_item)
            elif step.action == _RecognitionAction.UPDATE_CACHE:
                self._update_local_recognize_cache(**step.kwargs)
                result = None
            else:
                result = self._record_media_recognize_share_hit()

    async def _async_run_recognition_steps(
            self, plan: _RecognitionPlan
    ) -> Optional[MediaInfo]:
        """用异步端口驱动共享识别状态机，不在线程间复用模块对象。"""
        steps = self._recognition_steps(
            plan,
            self._can_use_media_recognize_share(
                plan.share_meta, plan.media_source, plan.media_id
            ),
        )
        result: Any = None
        while True:
            try:
                step = steps.send(result)
            except StopIteration as completed:
                return cast(Optional[MediaInfo], completed.value)
            if step.action == _RecognitionAction.NATIVE:
                result = await self._async_run_native_media_recognize(
                    step.kwargs, step.cache
                )
            elif step.action == _RecognitionAction.SUPPLEMENT:
                result = await self._async_supplement_media_recognize(**step.kwargs)
            elif step.action == _RecognitionAction.REPORT:
                result = await (
                    _recognition_share_snapshot().async_report_recognize_share(
                        **step.kwargs
                    )
                )
            elif step.action == _RecognitionAction.QUERY_SHARE:
                share_port = _recognition_share_snapshot()
                shared_item = await share_port.async_query_recognize_share(**step.kwargs)
                result = share_port.to_recognize_params(shared_item)
            elif step.action == _RecognitionAction.UPDATE_CACHE:
                await self._async_update_local_recognize_cache(**step.kwargs)
                result = None
            else:
                result = await run_in_threadpool(
                    self._record_media_recognize_share_hit
                )

    def recognize_media(
            self,
            meta: Optional[MetaBase] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            episode_group: Optional[str] = None,
            cache: bool = True,
            share_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息，不含Fanart图片
        :param meta:     识别的元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param mtype:    识别的媒体类型
        :param media_source: 请求级识别数据源
        :param media_id: 数据源原生ID，必须与media_source成对提供
        :param episode_group: 剧集组
        :param cache:    是否使用缓存
        :param music_type: 音乐实体类型，显式音乐 ID 必须据此区分单曲与专辑
        :return: 识别的媒体信息，包括剧集信息
        """
        plan = self._build_recognition_plan(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            episode_group=episode_group,
            cache=cache,
            share_meta=share_meta,
            music_type=music_type,
        )
        if not plan.identity_valid:
            logger.warning("媒体识别需要同时提供有效的 media_source 和 media_id")
            return None
        return cast(Optional[MediaInfo], self._finalize_recognition_result(self._run_recognition_steps(plan)))

    async def async_recognize_media(
            self,
            meta: Optional[MetaBase] = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            episode_group: Optional[str] = None,
            cache: bool = True,
            share_meta: Optional[MetaBase] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息，不含Fanart图片（异步版本）
        :param meta:     识别的元数据
        :param share_meta: 共享识别查询/上报使用的原始元数据
        :param mtype:    识别的媒体类型
        :param media_source: 请求级识别数据源
        :param media_id: 数据源原生ID，必须与media_source成对提供
        :param episode_group: 剧集组
        :param cache:    是否使用缓存
        :param music_type: 音乐实体类型，显式音乐 ID 必须据此区分单曲与专辑
        :return: 识别的媒体信息，包括剧集信息
        """
        plan = self._build_recognition_plan(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            episode_group=episode_group,
            cache=cache,
            share_meta=share_meta,
            music_type=music_type,
        )
        if not plan.identity_valid:
            logger.warning("媒体识别需要同时提供有效的 media_source 和 media_id")
            return None
        return cast(Optional[MediaInfo], await self._async_finalize_recognition_result(await self._async_run_recognition_steps(plan)))

    @staticmethod
    def _media_recognize_plugin_payload(
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource | str],
            media_id: Optional[str],
            is_music: bool,
            music_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        构造媒体识别链式事件的已知要素载荷，供插件匹配媒体信息；影视与音乐统一协议，
        仅要素字段随媒体类型不同
        """
        if is_music:
            return {
                "title": getattr(meta, "title", None),
                "artists": list(getattr(meta, "artists", None) or []),
                "album": getattr(meta, "album", None),
                "year": getattr(meta, "year", None),
                "isrc": getattr(meta, "isrc", None),
                "media_source": media_source,
                "media_id": media_id,
                "music_type": music_type,
            }
        return {
            "title": getattr(meta, "title", None) or getattr(meta, "name", None),
            "year": getattr(meta, "year", None),
            "season": getattr(meta, "begin_season", None),
            "type": mtype.value if isinstance(mtype, MediaType) else None,
            "media_source": media_source,
            "media_id": media_id,
        }

    @classmethod
    def _media_info_from_plugin(
            cls,
            event_data: dict[str, Any],
            is_music: bool,
            mtype: Optional[MediaType] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo | MusicInfo]:
        """
        解析插件返回的媒体信息，缺少数据源或身份字段的结果不采信；
        音乐构造 MusicInfo，影视构造 MediaInfo
        """
        if not isinstance(event_data, dict):
            return None
        plugin_info = event_data.get("mediainfo")
        if not isinstance(plugin_info, dict):
            return None
        if not plugin_info.get("media_source"):
            logger.warn("插件返回的媒体信息缺少数据源，忽略 ...")
            return None
        try:
            if is_music:
                if not plugin_info.get("media_id"):
                    logger.warn("插件返回的音乐媒体信息缺少媒体ID，忽略 ...")
                    return None
                music_info = MusicInfo.from_dict(plugin_info)
                if not music_info.media_source or not music_info.media_id:
                    return None
                if music_type and music_info.music_type != music_type:
                    logger.warn(
                        f"插件返回的音乐实体类型为 {music_info.music_type}，"
                        f"与请求的 {music_type} 不一致，忽略 ..."
                    )
                    return None
                return music_info
            # 影视：插件未提供类型时使用请求推断的类型
            if not plugin_info.get("type") and mtype:
                plugin_info = {**plugin_info, "type": mtype}
            media_info = MediaInfo()
            media_info.from_dict(plugin_info)
        except Exception as err:
            logger.warn(f"插件返回的媒体信息格式错误：{err}")
            return None
        # 影视与音乐统一要求远端身份，无身份的结果不采信，避免未验证结果进入识别管线
        if not media_info.media_source or not cls._media_info_has_identity(media_info):
            logger.warn("插件返回的媒体信息缺少远端身份，忽略 ...")
            return None
        return media_info

    @staticmethod
    def _media_info_has_identity(
            mediainfo: Optional[MediaInfo | MusicInfo],
    ) -> bool:
        """判断媒体信息是否具备完整的规范媒体身份。"""
        return _RecognitionOutcome.decide(mediainfo).has_identity

    def _build_plugin_recognition_plan(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource | str],
            media_id: Optional[str],
            mediainfo: Optional[MediaInfo | MusicInfo],
            music_type: Optional[str],
    ) -> Optional[_PluginRecognitionPlan]:
        """为缺少规范身份的候选结果生成插件补充识别计划。"""
        if _RecognitionOutcome.decide(mediainfo).has_identity:
            return None
        is_music = (
            isinstance(meta, MetaMusic)
            or mtype == MediaType.MUSIC
            or isinstance(mediainfo, MusicInfo)
        )
        return _PluginRecognitionPlan(
            payload=self._media_recognize_plugin_payload(
                meta, mtype, media_source, media_id, is_music, music_type
            ),
            fallback=mediainfo,
            is_music=is_music,
            mtype=mtype,
            music_type=music_type,
        )

    def _apply_plugin_recognition_result(
            self,
            plan: _PluginRecognitionPlan,
            result: Optional[Event],
    ) -> Optional[MediaInfo | MusicInfo]:
        """解析插件事件结果；无响应或非法身份时稳定保留原候选。"""
        if not result:
            return plan.fallback
        plugin_info = self._media_info_from_plugin(
            result.event_data or {},
            plan.is_music,
            plan.mtype,
            plan.music_type,
        )
        if not plugin_info:
            return plan.fallback
        logger.info(
            f"插件补充媒体识别成功：{plugin_info.title}"
            f"（{plugin_info.media_source}:{plugin_info.media_id}）"
        )
        return plugin_info

    def _supplement_media_recognize(
            self, meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource | str],
            media_id: Optional[str],
            mediainfo: Optional[MediaInfo | MusicInfo],
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo | MusicInfo]:
        """
        媒体识别插件补充（影视与音乐统一）：原生模块未给出带远端身份的结果时，
        广播媒体识别链式事件，允许插件（如第三方媒体源）按已知要素匹配并返回标准信息
        """
        plan = self._build_plugin_recognition_plan(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            mediainfo=mediainfo,
            music_type=music_type,
        )
        if plan is None:
            return mediainfo
        event_type = (
            ChainEventType.MusicMediaRecognize
            if plan.is_music
            else ChainEventType.MediaRecognize
        )
        if not self.eventmanager.check(event_type):
            return mediainfo
        result: Event = self.eventmanager.send_event(
            event_type,
            plan.payload,
        )
        return self._apply_plugin_recognition_result(plan, result)

    async def _async_supplement_media_recognize(
            self, meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource | str],
            media_id: Optional[str],
            mediainfo: Optional[MediaInfo | MusicInfo],
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo | MusicInfo]:
        """媒体识别插件补充的异步版本，影视与音乐统一流程"""
        plan = self._build_plugin_recognition_plan(
            meta=meta,
            mtype=mtype,
            media_source=media_source,
            media_id=media_id,
            mediainfo=mediainfo,
            music_type=music_type,
        )
        if plan is None:
            return mediainfo
        event_type = (
            ChainEventType.MusicMediaRecognize
            if plan.is_music
            else ChainEventType.MediaRecognize
        )
        if not self.eventmanager.check(event_type):
            return mediainfo
        result: Event = await self.eventmanager.async_send_event(
            event_type,
            plan.payload,
        )
        return self._apply_plugin_recognition_result(plan, result)
