"""媒体识别管线 mixin。

从 ChainBase 拆出的识别域：原生模块识别路由、识别缓存回填、共享识别、
插件补充识别。方法经 MRO 解析，依赖 ChainBase 实例的 unicast/broadcast/eventmanager
等协作对象。
"""
import copy
from typing import Optional

from app.runtime.execution import run_in_threadpool

from app.adapters.external.server import MoviePilotServerHelper
from app.application.configuration import get_configured_system_config
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.runtime.cache import fresh, async_fresh
from app.runtime.events import Event
from app.runtime.log import logger
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.schemas.types import ChainEventType, MediaSource, MediaType, SystemConfigKey


class RecognitionMixin:

    def _can_use_media_recognize_share(
            self,
            meta: Optional[MetaBase],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
    ) -> bool:
        """
        仅在名称识别场景下使用共享识别，显式ID识别不再重复回查
        """
        return bool(
            self.runtime_config.media_recognize_share
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
            mediainfo: Optional[MediaInfo],
    ) -> None:
        """
        共享识别成功后回填本地识别缓存，避免名称负缓存导致后续重复回查共享。
        """
        if not meta or not mediainfo:
            return
        self.broadcast(
            "update_recognize_cache",
            meta=meta,
            mediainfo=mediainfo,
        )

    async def _async_update_local_recognize_cache(
            self,
            meta: Optional[MetaBase],
            mediainfo: Optional[MediaInfo],
    ) -> None:
        """
        异步回填本地识别缓存。
        """
        if not meta or not mediainfo:
            return
        await self.async_broadcast(
            "async_update_recognize_cache",
            meta=meta,
            mediainfo=mediainfo,
        )

    @staticmethod
    def _record_media_recognize_share_hit() -> None:
        """记录一次共享媒体识别成功命中，统计失败不影响识别结果。"""
        try:
            get_configured_system_config().increment(SystemConfigKey.MediaRecognizeShareCount)
        except Exception as err:
            logger.error(f"记录共享媒体识别命中次数失败：{str(err)}")

    def _run_native_media_recognize(
            self,
            module_kwargs: dict,
            cache: bool,
    ) -> Optional[MediaInfo]:
        """执行同步原生媒体模块识别，具体媒体领域可覆写该路由钩子。"""
        with fresh(not cache):
            return self.unicast("recognize_media", **module_kwargs)

    async def _async_run_native_media_recognize(
            self,
            module_kwargs: dict,
            cache: bool,
    ) -> Optional[MediaInfo]:
        """执行异步原生媒体模块识别，具体媒体领域可覆写该路由钩子。"""
        async with async_fresh(not cache):
            return await self.async_unicast(
                "async_recognize_media", **module_kwargs
            )

    def recognize_media(
            self,
            meta: MetaBase = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            episode_group: Optional[str] = None,
            cache: bool = True,
            share_meta: MetaBase = None,
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
        # 仅传数据源是请求级识别源约束（按名称识别限定数据源），显式 media_id 才要求来源成对
        explicit_identity = media_id is not None
        requested_source = normalize_media_source(media_source) or media_source
        media_source, media_id = resolve_media_identity(
            media=meta,
            media_source=media_source,
            media_id=media_id,
        )
        if explicit_identity and (not media_source or not media_id):
            logger.warning("媒体识别需要同时提供有效的 media_source 和 media_id")
            return None
        if not media_id and requested_source is not None:
            media_source = requested_source
            # meta 自带同源身份（如 {tmdbid=} 标题）时直接按身份识别，避免退化为名称搜索
            meta_source, meta_id = resolve_media_identity(media=meta)
            if meta_id and meta_source == requested_source:
                media_source, media_id = meta_source, meta_id
        if not episode_group and hasattr(meta, "episode_group"):
            episode_group = meta.episode_group
        if not mtype and not (media_source and media_id) and meta and meta.type in [
            MediaType.TV, MediaType.MOVIE, MediaType.MUSIC
        ]:
            mtype = meta.type
        share_query_meta = share_meta or meta
        module_kwargs = {
            "meta": meta,
            "mtype": mtype,
            "media_source": media_source,
            "media_id": media_id,
            "episode_group": episode_group,
            "cache": cache,
        }
        if music_type is not None:
            module_kwargs["music_type"] = music_type
        mediainfo = self._run_native_media_recognize(module_kwargs, cache)
        # 原生识别未取得远端身份时，允许插件按已知要素补充匹配媒体信息（影视与音乐统一）
        mediainfo = self._supplement_media_recognize(
            meta=meta, mtype=mtype, media_source=media_source,
            media_id=media_id, mediainfo=mediainfo,
            music_type=music_type,
        )
        fallback_mediainfo = (
            mediainfo
            if mediainfo and not self._media_info_has_identity(mediainfo)
            else None
        )
        if mediainfo and self._media_info_has_identity(mediainfo):
            # 电影、电视剧、音乐统一上报；音乐的 tmdb 等字段恒为 None，身份取数据源原生 ID
            if not getattr(mediainfo, "recognize_cache_hit", False):
                MoviePilotServerHelper.report_recognize_share(
                    meta=meta,
                    mediainfo=mediainfo,
                    keyword_meta=share_query_meta,
                )
            return mediainfo

        if self._can_use_media_recognize_share(
                share_query_meta, media_source, media_id
        ):
            shared_cache_meta = self._snapshot_recognize_cache_meta(meta)
            share_query_kwargs = {
                "meta": meta,
                "mtype": mtype,
                "keyword_meta": share_query_meta,
            }
            if music_type is not None:
                share_query_kwargs["music_type"] = music_type
            shared_item = MoviePilotServerHelper.query_recognize_share(
                **share_query_kwargs,
            )
            shared_params = MoviePilotServerHelper.to_recognize_params(shared_item)
            if shared_params:
                shared_module_kwargs = {
                        "meta": meta,
                        "mtype": shared_params.get("mtype") or mtype,
                        "media_source": shared_params.get("media_source"),
                        "media_id": shared_params.get("media_id"),
                        "episode_group": episode_group,
                        "cache": cache,
                }
                shared_music_type = shared_params.get("music_type") or music_type
                if shared_music_type is not None:
                    shared_module_kwargs["music_type"] = shared_music_type
                mediainfo = self._run_native_media_recognize(
                    shared_module_kwargs,
                    cache,
                )
                if mediainfo and self._media_info_has_identity(mediainfo):
                    self._update_local_recognize_cache(shared_cache_meta, mediainfo)
                    self._record_media_recognize_share_hit()
                    return mediainfo
                if mediainfo and not fallback_mediainfo:
                    fallback_mediainfo = mediainfo
        return fallback_mediainfo

    async def async_recognize_media(
            self,
            meta: MetaBase = None,
            mtype: Optional[MediaType] = None,
            media_source: Optional[MediaSource] = None,
            media_id: Optional[str] = None,
            episode_group: Optional[str] = None,
            cache: bool = True,
            share_meta: MetaBase = None,
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
        # 仅传数据源是请求级识别源约束（按名称识别限定数据源），显式 media_id 才要求来源成对
        explicit_identity = media_id is not None
        requested_source = normalize_media_source(media_source) or media_source
        media_source, media_id = resolve_media_identity(
            media=meta,
            media_source=media_source,
            media_id=media_id,
        )
        if explicit_identity and (not media_source or not media_id):
            logger.warning("媒体识别需要同时提供有效的 media_source 和 media_id")
            return None
        if not media_id and requested_source is not None:
            media_source = requested_source
            # meta 自带同源身份（如 {tmdbid=} 标题）时直接按身份识别，避免退化为名称搜索
            meta_source, meta_id = resolve_media_identity(media=meta)
            if meta_id and meta_source == requested_source:
                media_source, media_id = meta_source, meta_id
        if not episode_group and hasattr(meta, "episode_group"):
            episode_group = meta.episode_group
        if not mtype and not (media_source and media_id) and meta and meta.type in [
            MediaType.TV, MediaType.MOVIE, MediaType.MUSIC
        ]:
            mtype = meta.type
        share_query_meta = share_meta or meta
        module_kwargs = {
            "meta": meta,
            "mtype": mtype,
            "media_source": media_source,
            "media_id": media_id,
            "episode_group": episode_group,
            "cache": cache,
        }
        if music_type is not None:
            module_kwargs["music_type"] = music_type
        mediainfo = await self._async_run_native_media_recognize(module_kwargs, cache)
        # 原生识别未取得远端身份时，允许插件按已知要素补充匹配媒体信息（影视与音乐统一）
        mediainfo = await self._async_supplement_media_recognize(
            meta=meta, mtype=mtype, media_source=media_source,
            media_id=media_id, mediainfo=mediainfo,
            music_type=music_type,
        )
        fallback_mediainfo = (
            mediainfo
            if mediainfo and not self._media_info_has_identity(mediainfo)
            else None
        )
        if mediainfo and self._media_info_has_identity(mediainfo):
            # 电影、电视剧、音乐统一上报；音乐的 tmdb 等字段恒为 None，身份取数据源原生 ID
            if not getattr(mediainfo, "recognize_cache_hit", False):
                await MoviePilotServerHelper.async_report_recognize_share(
                    meta=meta,
                    mediainfo=mediainfo,
                    keyword_meta=share_query_meta,
                )
            return mediainfo

        if self._can_use_media_recognize_share(
                share_query_meta, media_source, media_id
        ):
            shared_cache_meta = self._snapshot_recognize_cache_meta(meta)
            share_query_kwargs = {
                "meta": meta,
                "mtype": mtype,
                "keyword_meta": share_query_meta,
            }
            if music_type is not None:
                share_query_kwargs["music_type"] = music_type
            shared_item = await MoviePilotServerHelper.async_query_recognize_share(
                **share_query_kwargs,
            )
            shared_params = MoviePilotServerHelper.to_recognize_params(shared_item)
            if shared_params:
                shared_module_kwargs = {
                        "meta": meta,
                        "mtype": shared_params.get("mtype") or mtype,
                        "media_source": shared_params.get("media_source"),
                        "media_id": shared_params.get("media_id"),
                        "episode_group": episode_group,
                        "cache": cache,
                }
                shared_music_type = shared_params.get("music_type") or music_type
                if shared_music_type is not None:
                    shared_module_kwargs["music_type"] = shared_music_type
                mediainfo = await self._async_run_native_media_recognize(
                    shared_module_kwargs,
                    cache,
                )
                if mediainfo and self._media_info_has_identity(mediainfo):
                    await self._async_update_local_recognize_cache(shared_cache_meta, mediainfo)
                    await run_in_threadpool(self._record_media_recognize_share_hit)
                    return mediainfo
                if mediainfo and not fallback_mediainfo:
                    fallback_mediainfo = mediainfo
        return fallback_mediainfo

    @staticmethod
    def _media_recognize_plugin_payload(
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            is_music: bool,
            music_type: Optional[str] = None,
    ) -> dict:
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
            event_data: dict,
            is_music: bool,
            mtype: Optional[MediaType] = None,
            music_type: Optional[str] = None,
    ) -> Optional[MediaInfo]:
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
                info: MediaInfo = MusicInfo.from_dict(plugin_info)
                if not info.media_source or not info.media_id:
                    return None
                if music_type and info.music_type != music_type:
                    logger.warn(
                        f"插件返回的音乐实体类型为 {info.music_type}，"
                        f"与请求的 {music_type} 不一致，忽略 ..."
                    )
                    return None
                return info
            # 影视：插件未提供类型时使用请求推断的类型
            if not plugin_info.get("type") and mtype:
                plugin_info = {**plugin_info, "type": mtype}
            info = MediaInfo()
            info.from_dict(plugin_info)
        except Exception as err:
            logger.warn(f"插件返回的媒体信息格式错误：{err}")
            return None
        # 影视与音乐统一要求远端身份，无身份的结果不采信，避免未验证结果进入识别管线
        if not info.media_source or not cls._media_info_has_identity(info):
            logger.warn("插件返回的媒体信息缺少远端身份，忽略 ...")
            return None
        return info

    @staticmethod
    def _media_info_has_identity(mediainfo) -> bool:
        """判断媒体信息是否具备完整的规范媒体身份。"""
        media_source, media_id = resolve_media_identity(media=mediainfo)
        return bool(media_source and media_id)

    def _supplement_media_recognize(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            mediainfo,
            music_type: Optional[str] = None,
    ):
        """
        媒体识别插件补充（影视与音乐统一）：原生模块未给出带远端身份的结果时，
        广播媒体识别链式事件，允许插件（如第三方媒体源）按已知要素匹配并返回标准信息
        """
        is_music = (
                isinstance(meta, MetaMusic)
                or mtype == MediaType.MUSIC
                or isinstance(mediainfo, MusicInfo)
        )
        # 已有远端身份时无需插件介入
        if mediainfo and self._media_info_has_identity(mediainfo):
            return mediainfo
        etype = ChainEventType.MusicMediaRecognize if is_music else ChainEventType.MediaRecognize
        if not self.eventmanager.check(etype):
            return mediainfo
        result: Event = self.eventmanager.send_event(
            etype,
            self._media_recognize_plugin_payload(
                meta, mtype, media_source, media_id, is_music, music_type
            ),
        )
        if not result:
            return mediainfo
        plugin_info = self._media_info_from_plugin(
            result.event_data or {}, is_music, mtype, music_type
        )
        if not plugin_info:
            return mediainfo
        logger.info(
            f"插件补充媒体识别成功：{plugin_info.title}"
            f"（{plugin_info.media_source}:{plugin_info.media_id}）"
        )
        return plugin_info

    async def _async_supplement_media_recognize(
            self,
            meta: Optional[MetaBase],
            mtype: Optional[MediaType],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
            mediainfo,
            music_type: Optional[str] = None,
    ):
        """媒体识别插件补充的异步版本，影视与音乐统一流程"""
        is_music = (
                isinstance(meta, MetaMusic)
                or mtype == MediaType.MUSIC
                or isinstance(mediainfo, MusicInfo)
        )
        # 已有远端身份时无需插件介入
        if mediainfo and self._media_info_has_identity(mediainfo):
            return mediainfo
        etype = ChainEventType.MusicMediaRecognize if is_music else ChainEventType.MediaRecognize
        if not self.eventmanager.check(etype):
            return mediainfo
        result: Event = await self.eventmanager.async_send_event(
            etype,
            self._media_recognize_plugin_payload(
                meta, mtype, media_source, media_id, is_music, music_type
            ),
        )
        if not result:
            return mediainfo
        plugin_info = self._media_info_from_plugin(
            result.event_data or {}, is_music, mtype, music_type
        )
        if not plugin_info:
            return mediainfo
        logger.info(
            f"插件补充媒体识别成功：{plugin_info.title}"
            f"（{plugin_info.media_source}:{plugin_info.media_id}）"
        )
        return plugin_info
