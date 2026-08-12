import copy
import inspect
import pickle
import traceback
from abc import ABCMeta
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Tuple, List, Set, Union, Dict

from fastapi.concurrency import run_in_threadpool
from qbittorrentapi import TorrentFilesList
from transmission_rpc import File

from app.core.cache import FileCache, AsyncFileCache, fresh, async_fresh
from app.core.config import settings
from app.core.context import Context, MediaInfo, MusicInfo, SubtitleInfo, TorrentInfo
from app.core.event import Event, EventManager
from app.core.meta import MetaBase, MetaMusic
from app.core.module import ModuleManager
from app.core.plugin import PluginManager
from app.db.message_oper import MessageOper
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import UserOper
from app.helper.message import MessageHelper, MessageQueueManager, MessageTemplateHelper
from app.helper.server import MoviePilotServerHelper
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.schemas import (
    RateLimitExceededException,
    TransferInfo,
    ExistMediaInfo,
    DownloaderTorrent,
    CommingMessage,
    Notification,
    WebhookEventInfo,
    TmdbEpisode,
    MediaPerson,
    FileItem,
    TransferDirectoryConf,
    MessageResponse,
)
from app.utils.identity import normalize_internal_user_id
from app.utils.media import resolve_media_identity
from app.schemas.message import ChannelCapability, ChannelCapabilityManager
from app.schemas.category import CategoryConfig
from app.schemas.types import (
    TorrentStatus,
    MediaType,
    MediaImageType,
    EventType,
    ChainEventType,
    MessageChannel,
    MediaSource,
    SystemConfigKey,
)
from app.utils.object import ObjectUtils


class ChainBase(metaclass=ABCMeta):
    """
    处理链基类
    """

    def __init__(self):
        """
        公共初始化
        """
        self.modulemanager = ModuleManager()
        self.eventmanager = EventManager()
        self.messageoper = MessageOper()
        self.messagehelper = MessageHelper()
        self.messagequeue = MessageQueueManager(send_callback=self.run_module)
        self.pluginmanager = PluginManager()
        self.filecache = FileCache()
        self.async_filecache = AsyncFileCache()

    def load_cache(self, filename: str) -> Any:
        """
        加载缓存
        """
        content = self.filecache.get(filename)
        if not content:
            return None
        try:
            return pickle.loads(content)
        except Exception as err:
            logger.error(f"加载缓存 {filename} 出错：{str(err)}")
            return None

    async def async_load_cache(self, filename: str) -> Any:
        """
        异步加载缓存
        """
        content = await self.async_filecache.get(filename)
        if not content:
            return None
        try:
            return pickle.loads(content)
        except Exception as err:
            logger.error(f"异步加载缓存 {filename} 出错：{str(err)}")
            return None

    async def async_save_cache(self, cache: Any, filename: str) -> None:
        """
        异步保存缓存
        """
        try:
            await self.async_filecache.set(filename, pickle.dumps(cache))
        except Exception as err:
            logger.error(f"异步保存缓存 {filename} 出错：{str(err)}")
            return

    def save_cache(self, cache: Any, filename: str) -> None:
        """
        保存缓存
        """
        try:
            self.filecache.set(filename, pickle.dumps(cache))
        except Exception as err:
            logger.error(f"保存缓存 {filename} 出错：{str(err)}")
            return

    def remove_cache(self, filename: str) -> None:
        """
        删除缓存，同时删除Redis和本地缓存
        """
        self.filecache.delete(filename)

    def start_message_processing_status(
            self,
            channel: MessageChannel,
            source: Optional[str],
            userid: Optional[Union[str, int]] = None,
            message_id: Optional[Union[str, int]] = None,
            chat_id: Optional[Union[str, int]] = None,
            text: Optional[str] = None,
    ) -> Optional[dict]:
        """
        启动渠道侧消息输入/处理状态。
        具体表现由消息模块实现，例如 typing 保活或消息 reaction。
        """
        if not channel or not ChannelCapabilityManager.supports_capability(
                channel, ChannelCapability.PROCESSING_STATUS
        ):
            return None
        try:
            status = self.run_module(
                "mark_message_processing_started",
                channel=channel,
                source=source,
                userid=userid,
                message_id=message_id,
                chat_id=chat_id,
                text=text,
            )
        except Exception as err:
            logger.debug(f"启动消息处理状态失败: {err}")
            return None
        return status if isinstance(status, dict) else None

    def finish_message_processing_status(
            self,
            status: Optional[dict] = None,
            channel: Optional[MessageChannel] = None,
            source: Optional[str] = None,
            userid: Optional[Union[str, int]] = None,
            message_id: Optional[Union[str, int]] = None,
            chat_id: Optional[Union[str, int]] = None,
    ) -> None:
        """
        结束渠道侧消息输入/处理状态。
        优先使用 start 返回的 status，缺失时使用显式渠道和消息定位参数。
        """
        target_channel = channel
        if status:
            try:
                target_channel = MessageChannel(status.get("channel"))
            except Exception:
                target_channel = channel
        if not target_channel or not ChannelCapabilityManager.supports_capability(
                target_channel, ChannelCapability.PROCESSING_STATUS
        ):
            return
        try:
            self.run_module(
                "mark_message_processing_finished",
                channel=target_channel,
                source=(status or {}).get("source") or source,
                userid=(status or {}).get("userid") or userid,
                message_id=(status or {}).get("message_id") or message_id,
                chat_id=(status or {}).get("chat_id") or chat_id,
                status=status,
            )
        except Exception as err:
            logger.debug(f"结束消息处理状态失败: {err}")

    @staticmethod
    def _normalize_notification_for_dispatch(
            message: Notification
    ) -> Notification:
        """
        规范化待发送的通知消息。
        后台任务会复用内部占位用户ID作为会话身份，这里在真正发送前清空，
        让消息重新走默认通知路由或基于 targets 的目标解析。
        """
        dispatch_message = copy.deepcopy(message)
        dispatch_message.userid = normalize_internal_user_id(
            dispatch_message.userid
        )
        return dispatch_message

    @staticmethod
    def _build_notice_message_data(message: Notification) -> dict:
        """
        构造消息通知事件数据。
        """
        return {**message.model_dump(exclude={"save_history"}), "type": message.mtype}

    async def async_remove_cache(self, filename: str) -> None:
        """
        异步删除缓存，同时删除Redis和本地缓存
        """
        await self.async_filecache.delete(filename)

    @staticmethod
    def __is_valid_empty(ret):
        """
        判断结果是否为空
        """
        if isinstance(ret, tuple):
            return all(value is None for value in ret)
        else:
            return ret is None

    def __handle_plugin_error(
            self, err: Exception, plugin_id: str, plugin_name: str, method: str, **kwargs
    ):
        """
        处理插件模块执行错误
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.error(
            f"运行插件 {plugin_id} 模块 {method} 出错：{str(err)}\n{traceback.format_exc()}"
        )
        self.messagehelper.put(
            title=f"{plugin_name} 发生了错误", message=str(err), role="plugin"
        )
        self.eventmanager.send_event(
            EventType.SystemError,
            {
                "type": "plugin",
                "plugin_id": plugin_id,
                "plugin_name": plugin_name,
                "plugin_method": method,
                "error": str(err),
                "traceback": traceback.format_exc(),
            },
        )

    def __handle_system_error(
            self, err: Exception, module_id: str, module_name: str, method: str, **kwargs
    ):
        """
        处理系统模块执行错误
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.error(
            f"运行模块 {module_id}.{method} 出错：{str(err)}\n{traceback.format_exc()}"
        )
        self.messagehelper.put(
            title=f"{module_name}发生了错误", message=str(err), role="system"
        )
        self.eventmanager.send_event(
            EventType.SystemError,
            {
                "type": "module",
                "module_id": module_id,
                "module_name": module_name,
                "module_method": method,
                "error": str(err),
                "traceback": traceback.format_exc(),
            },
        )

    @staticmethod
    def __handle_rate_limit_error(
            err: RateLimitExceededException, source_type: str, source_id: str,
            method: str, **kwargs
    ) -> None:
        """
        处理本地限流跳过，避免预期的限流状态进入系统错误告警。
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.info(f"{source_type} {source_id}.{method} 已限流，跳过执行：{str(err)}")

    def __execute_plugin_modules(
            self, method: str, result: Any, *args, **kwargs
    ) -> Any:
        """
        执行插件模块
        """
        for plugin, module_dict in self.pluginmanager.get_plugin_modules().items():
            plugin_id, plugin_name = plugin
            if method in module_dict:
                func = module_dict[method]
                if func:
                    try:
                        logger.info(f"请求插件 {plugin_name} 执行：{method} ...")
                        if self.__is_valid_empty(result):
                            # 返回None，第一次执行或者需继续执行下一模块
                            result = func(*args, **kwargs)
                        elif isinstance(result, list):
                            # 返回为列表，有多个模块运行结果时进行合并
                            temp = func(*args, **kwargs)
                            if isinstance(temp, list):
                                result.extend(temp)
                        else:
                            break
                    except RateLimitExceededException as err:
                        self.__handle_rate_limit_error(
                            err, "插件", plugin_id, method, **kwargs
                        )
                    except Exception as err:
                        self.__handle_plugin_error(
                            err, plugin_id, plugin_name, method, **kwargs
                        )
        return result

    async def __async_execute_plugin_modules(
            self, method: str, result: Any, *args, **kwargs
    ) -> Any:
        """
        异步执行插件模块
        """
        for plugin, module_dict in self.pluginmanager.get_plugin_modules().items():
            plugin_id, plugin_name = plugin
            if method in module_dict:
                func = module_dict[method]
                if func:
                    try:
                        logger.info(f"请求插件 {plugin_name} 执行：{method} ...")
                        if self.__is_valid_empty(result):
                            # 返回None，第一次执行或者需继续执行下一模块
                            if inspect.iscoroutinefunction(func):
                                result = await func(*args, **kwargs)
                            else:
                                # 插件同步函数在异步环境中运行，避免阻塞
                                result = await run_in_threadpool(func, *args, **kwargs)
                        elif isinstance(result, list):
                            # 返回为列表，有多个模块运行结果时进行合并
                            if inspect.iscoroutinefunction(func):
                                temp = await func(*args, **kwargs)
                            else:
                                # 插件同步函数在异步环境中运行，避免阻塞
                                temp = await run_in_threadpool(func, *args, **kwargs)
                            if isinstance(temp, list):
                                result.extend(temp)
                        else:
                            break
                    except RateLimitExceededException as err:
                        self.__handle_rate_limit_error(
                            err, "插件", plugin_id, method, **kwargs
                        )
                    except Exception as err:
                        self.__handle_plugin_error(
                            err, plugin_id, plugin_name, method, **kwargs
                        )
        return result

    def __execute_system_modules(
            self, method: str, result: Any, *args, **kwargs
    ) -> Any:
        """
        执行系统模块
        """
        logger.debug(f"请求系统模块执行：{method} ...")
        for module in sorted(
                self.modulemanager.get_running_modules(method),
                key=lambda x: x.get_priority(),
        ):
            module_id = module.__class__.__name__
            try:
                module_name = module.get_name()
            except Exception as err:
                logger.debug(f"获取模块名称出错：{str(err)}")
                module_name = module_id
            try:
                func = getattr(module, method)
                if self.__is_valid_empty(result):
                    # 返回None，第一次执行或者需继续执行下一模块
                    result = func(*args, **kwargs)
                elif ObjectUtils.check_signature(func, result):
                    # 返回结果与方法签名一致，将结果传入
                    result = func(result)
                elif isinstance(result, list):
                    # 返回为列表，有多个模块运行结果时进行合并
                    temp = func(*args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    # 中止继续执行
                    break
            except RateLimitExceededException as err:
                self.__handle_rate_limit_error(
                    err, "模块", module_id, method, **kwargs
                )
            except Exception as err:
                logger.error(traceback.format_exc())
                self.__handle_system_error(
                    err, module_id, module_name, method, **kwargs
                )
        return result

    async def __async_execute_system_modules(
            self, method: str, result: Any, *args, **kwargs
    ) -> Any:
        """
        异步执行系统模块
        """
        logger.debug(f"请求系统模块执行：{method} ...")
        for module in sorted(
                self.modulemanager.get_running_modules(method),
                key=lambda x: x.get_priority(),
        ):
            module_id = module.__class__.__name__
            try:
                module_name = module.get_name()
            except Exception as err:
                logger.debug(f"获取模块名称出错：{str(err)}")
                module_name = module_id
            try:
                func = getattr(module, method)
                if self.__is_valid_empty(result):
                    # 返回None，第一次执行或者需继续执行下一模块
                    if inspect.iscoroutinefunction(func):
                        result = await func(*args, **kwargs)
                    else:
                        # 系统同步模块在异步路径里也必须切到线程池，避免阻塞共享事件循环。
                        result = await run_in_threadpool(func, *args, **kwargs)
                elif ObjectUtils.check_signature(func, result):
                    # 返回结果与方法签名一致，将结果传入
                    if inspect.iscoroutinefunction(func):
                        result = await func(result)
                    else:
                        result = await run_in_threadpool(func, result)
                elif isinstance(result, list):
                    # 返回为列表，有多个模块运行结果时进行合并
                    if inspect.iscoroutinefunction(func):
                        temp = await func(*args, **kwargs)
                    else:
                        temp = await run_in_threadpool(func, *args, **kwargs)
                    if isinstance(temp, list):
                        result.extend(temp)
                else:
                    # 中止继续执行
                    break
            except RateLimitExceededException as err:
                self.__handle_rate_limit_error(
                    err, "模块", module_id, method, **kwargs
                )
            except Exception as err:
                logger.error(traceback.format_exc())
                self.__handle_system_error(
                    err, module_id, module_name, method, **kwargs
                )
        return result

    def run_module(
            self,
            method: str,
            *args,
            **kwargs,
    ) -> Any:
        """
        运行包含该方法的所有模块，然后返回结果
        当kwargs包含命名参数raise_exception时，如模块方法抛出异常且raise_exception为True，则同步抛出异常

        :param method: 模块方法名称
        """
        # 执行插件模块
        result = self.__execute_plugin_modules(method, None, *args, **kwargs)

        if not self.__is_valid_empty(result) and not isinstance(result, list):
            # 插件模块返回结果不为空且不是列表，直接返回
            return result

        # 执行系统模块
        return self.__execute_system_modules(method, result, *args, **kwargs)

    async def async_run_module(
            self,
            method: str,
            *args,
            **kwargs,
    ) -> Any:
        """
        异步运行包含该方法的所有模块，然后返回结果
        当kwargs包含命名参数raise_exception时，如模块方法抛出异常且raise_exception为True，则同步抛出异常
        支持异步和同步方法的混合调用

        :param method: 模块方法名称
        """
        # 执行插件模块
        result = await self.__async_execute_plugin_modules(
            method, None, *args, **kwargs
        )

        if not self.__is_valid_empty(result) and not isinstance(result, list):
            # 插件模块返回结果不为空且不是列表，直接返回
            return result

        # 执行系统模块
        return await self.__async_execute_system_modules(
            method, result, *args, **kwargs
        )

    @staticmethod
    def _can_use_media_recognize_share(
            meta: Optional[MetaBase],
            media_source: Optional[MediaSource],
            media_id: Optional[str],
    ) -> bool:
        """
        仅在名称识别场景下使用共享识别，显式ID识别不再重复回查
        """
        return bool(
            settings.MEDIA_RECOGNIZE_SHARE
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
        self.run_module(
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
        await self.async_run_module(
            "async_update_recognize_cache",
            meta=meta,
            mediainfo=mediainfo,
        )

    @staticmethod
    def _record_media_recognize_share_hit() -> None:
        """记录一次共享媒体识别成功命中，统计失败不影响识别结果。"""
        try:
            SystemConfigOper().increment(SystemConfigKey.MediaRecognizeShareCount)
        except Exception as err:
            logger.error(f"记录共享媒体识别命中次数失败：{str(err)}")

    def _run_native_media_recognize(
            self,
            module_kwargs: dict,
            cache: bool,
    ) -> Optional[MediaInfo]:
        """执行同步原生媒体模块识别，具体媒体领域可覆写该路由钩子。"""
        with fresh(not cache):
            return self.run_module("recognize_media", **module_kwargs)

    async def _async_run_native_media_recognize(
            self,
            module_kwargs: dict,
            cache: bool,
    ) -> Optional[MediaInfo]:
        """执行异步原生媒体模块识别，具体媒体领域可覆写该路由钩子。"""
        async with async_fresh(not cache):
            return await self.async_run_module(
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
        explicit_identity = media_source is not None or media_id is not None
        media_source, media_id = resolve_media_identity(
            media=meta,
            media_source=media_source,
            media_id=media_id,
        )
        if explicit_identity and (not media_source or not media_id):
            logger.warning("媒体识别需要同时提供有效的 media_source 和 media_id")
            return None
        if not episode_group and hasattr(meta, "episode_group"):
            episode_group = meta.episode_group
        if not mtype and meta and meta.type in [
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
        explicit_identity = media_source is not None or media_id is not None
        media_source, media_id = resolve_media_identity(
            media=meta,
            media_source=media_source,
            media_id=media_id,
        )
        if explicit_identity and (not media_source or not media_id):
            logger.warning("媒体识别需要同时提供有效的 media_source 和 media_id")
            return None
        if not episode_group and hasattr(meta, "episode_group"):
            episode_group = meta.episode_group
        if not mtype and meta and meta.type in [
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

    def match_doubaninfo(
            self,
            name: str,
            imdbid: Optional[str] = None,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
            raise_exception: bool = False,
    ) -> Optional[dict]:
        """
        搜索和匹配豆瓣信息
        :param name: 标题
        :param imdbid: imdbid
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return self.run_module(
            "match_doubaninfo",
            name=name,
            imdbid=imdbid,
            mtype=mtype,
            year=year,
            season=season,
            raise_exception=raise_exception,
        )

    async def async_match_doubaninfo(
            self,
            name: str,
            imdbid: Optional[str] = None,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
            raise_exception: bool = False,
    ) -> Optional[dict]:
        """
        搜索和匹配豆瓣信息（异步版本）
        :param name: 标题
        :param imdbid: imdbid
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return await self.async_run_module(
            "async_match_doubaninfo",
            name=name,
            imdbid=imdbid,
            mtype=mtype,
            year=year,
            season=season,
            raise_exception=raise_exception,
        )

    def match_tmdbinfo(
            self,
            name: str,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        搜索和匹配TMDB信息
        :param name: 标题
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        """
        return self.run_module(
            "match_tmdbinfo", name=name, mtype=mtype, year=year, season=season
        )

    async def async_match_tmdbinfo(
            self,
            name: str,
            mtype: Optional[MediaType] = None,
            year: Optional[str] = None,
            season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        搜索和匹配TMDB信息（异步版本）
        :param name: 标题
        :param mtype: 类型
        :param year: 年份
        :param season: 季
        """
        return await self.async_run_module(
            "async_match_tmdbinfo", name=name, mtype=mtype, year=year, season=season
        )

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if mediainfo and mediainfo.type == MediaType.MUSIC:
            return mediainfo
        return self.run_module("obtain_images", mediainfo=mediainfo)

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片（异步版本）
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if mediainfo and mediainfo.type == MediaType.MUSIC:
            return mediainfo
        return await self.async_run_module("async_obtain_images", mediainfo=mediainfo)

    def obtain_specific_image(
            self,
            mediaid: Union[str, int],
            mtype: MediaType,
            image_type: MediaImageType,
            image_prefix: Optional[str] = None,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[str]:
        """
        获取指定媒体信息图片，返回图片地址
        :param mediaid:     媒体ID
        :param mtype:       媒体类型
        :param image_type:  图片类型
        :param image_prefix: 图片前缀
        :param season:      季
        :param episode:     集
        """
        return self.run_module(
            "obtain_specific_image",
            mediaid=mediaid,
            mtype=mtype,
            image_prefix=image_prefix,
            image_type=image_type,
            season=season,
            episode=episode,
        )

    def douban_info(
            self,
            doubanid: str,
            mtype: Optional[MediaType] = None,
            raise_exception: bool = False,
    ) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :return: 豆瓣信息
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return self.run_module(
            "douban_info",
            doubanid=doubanid,
            mtype=mtype,
            raise_exception=raise_exception,
        )

    async def async_douban_info(
            self,
            doubanid: str,
            mtype: Optional[MediaType] = None,
            raise_exception: bool = False,
    ) -> Optional[dict]:
        """
        获取豆瓣信息（异步版本）
        :param doubanid: 豆瓣ID
        :param mtype: 媒体类型
        :return: 豆瓣信息
        :param raise_exception: 触发速率限制时是否抛出异常
        """
        return await self.async_run_module(
            "async_douban_info",
            doubanid=doubanid,
            mtype=mtype,
            raise_exception=raise_exception,
        )

    def tvdb_info(self, tvdbid: int) -> Optional[dict]:
        """
        获取TVDB信息
        :param tvdbid: int
        :return: TVDB信息
        """
        return self.run_module("tvdb_info", tvdbid=tvdbid)

    def tvdb_slug(self, tvdbid: int) -> Optional[str]:
        """
        获取TVDB剧集 slug（别名），用于构建 TheTvDb 直达链接。
        :param tvdbid: int
        :return: slug 字符串
        """
        return self.run_module("tvdb_slug", tvdbid=tvdbid)

    def tmdb_info(
            self, tmdbid: int, mtype: MediaType, season: Optional[int] = None
    ) -> Optional[dict]:
        """
        获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季
        :return: TVDB信息
        """
        return self.run_module("tmdb_info", tmdbid=tmdbid, mtype=mtype, season=season)

    async def async_tmdb_info(
            self, tmdbid: int, mtype: MediaType, season: Optional[int] = None
    ) -> Optional[dict]:
        """
        获取TMDB信息（异步版本）
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季
        :return: TVDB信息
        """
        return await self.async_run_module(
            "async_tmdb_info", tmdbid=tmdbid, mtype=mtype, season=season
        )

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: int
        :return: Bangumi信息
        """
        return self.run_module("bangumi_info", bangumiid=bangumiid)

    async def async_bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息（异步版本）
        :param bangumiid: int
        :return: Bangumi信息
        """
        return await self.async_run_module("async_bangumi_info", bangumiid=bangumiid)

    def message_parser(
            self, source: str, body: Any, form: Any, args: Any
    ) -> Optional[CommingMessage]:
        """
        解析消息内容，返回字典，注意以下约定值：
        userid: 用户ID
        username: 用户名
        text: 内容
        :param source: 消息来源（渠道配置名称）
        :param body: 请求体
        :param form: 表单
        :param args: 参数
        :return: 消息渠道、消息内容
        """
        return self.run_module(
            "message_parser", source=source, body=body, form=form, args=args
        )

    def webhook_parser(
            self, body: Any, form: Any, args: Any
    ) -> Optional[WebhookEventInfo]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self.run_module("webhook_parser", body=body, form=form, args=args)

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSource] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        return self.run_module(
            "search_medias", meta=meta, media_source=media_source
        )

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSource] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        return await self.async_run_module(
            "async_search_medias", meta=meta, media_source=media_source
        )

    def search_persons(
        self, name: str, media_source: Optional[MediaSource] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息
        :param name:  人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        return self.run_module(
            "search_persons", name=name, media_source=media_source
        )

    async def async_search_persons(
        self, name: str, media_source: Optional[MediaSource] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息（异步版本）
        :param name:  人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        return await self.async_run_module(
            "async_search_persons", name=name, media_source=media_source
        )

    def search_collections(
        self, name: str, media_source: Optional[MediaSource] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息
        :param name:  集合名称
        :param media_source: 请求级搜索数据源
        :return: 合集信息列表
        """
        return self.run_module(
            "search_collections", name=name, media_source=media_source
        )

    async def async_search_collections(
        self, name: str, media_source: Optional[MediaSource] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息（异步版本）
        :param name:  集合名称
        :param media_source: 请求级搜索数据源
        :return: 合集信息列表
        """
        return await self.async_run_module(
            "async_search_collections", name=name, media_source=media_source
        )

    def get_search_page_size(
            self,
            site: dict,
            keyword: Optional[str] = None,
    ) -> Optional[int]:
        """
        获取站点搜索单页容量；返回 None 表示当前搜索入口不支持可靠翻页。
        """
        return self.run_module(
            "get_search_page_size", site=site, keyword=keyword
        )

    def search_torrents(
            self,
            site: dict,
            keyword: str,
            mtype: Optional[MediaType] = None,
            page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """
        搜索一个站点的种子资源
        :param site:  站点
        :param keyword:  搜索关键词
        :param mtype:  媒体类型
        :param page:  页码
        :reutrn: 资源列表
        """
        return self.run_module(
            "search_torrents", site=site, keyword=keyword, mtype=mtype, page=page
        )

    def search_subtitles(
            self,
            site: dict,
            keyword: str,
            page: Optional[int] = 0,
    ) -> List[SubtitleInfo]:
        """
        搜索一个站点的字幕资源。
        :param site: 站点
        :param keyword: 搜索关键词
        :param page: 页码
        :return: 字幕列表
        """
        return self.run_module(
            "search_subtitles", site=site, keyword=keyword, page=page
        )

    async def async_search_torrents(
            self,
            site: dict,
            keyword: str,
            mtype: Optional[MediaType] = None,
            page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """
        异步搜索一个站点的种子资源
        :param site:  站点
        :param keyword:  搜索关键词
        :param mtype:  媒体类型
        :param page:  页码
        :reutrn: 资源列表
        """
        return await self.async_run_module(
            "async_search_torrents", site=site, keyword=keyword, mtype=mtype, page=page
        )

    async def async_search_subtitles(
            self,
            site: dict,
            keyword: str,
            page: Optional[int] = 0,
    ) -> List[SubtitleInfo]:
        """
        异步搜索一个站点的字幕资源。
        :param site: 站点
        :param keyword: 搜索关键词
        :param page: 页码
        :return: 字幕列表
        """
        return await self.async_run_module(
            "async_search_subtitles", site=site, keyword=keyword, page=page
        )

    def refresh_torrents(
            self,
            site: dict,
            keyword: Optional[str] = None,
            cat: Optional[str] = None,
            page: Optional[int] = 0,
            mtype: Optional[MediaType] = None,
    ) -> List[TorrentInfo]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :param keyword:  标题
        :param cat:  分类
        :param page:  页码
        :param mtype: 媒体类型
        :reutrn: 种子资源列表
        """
        return self.run_module(
            "refresh_torrents", site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
        )

    async def async_refresh_torrents(
            self,
            site: dict,
            keyword: Optional[str] = None,
            cat: Optional[str] = None,
            page: Optional[int] = 0,
            mtype: Optional[MediaType] = None,
    ) -> List[TorrentInfo]:
        """
        异步获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :param keyword:  标题
        :param cat:  分类
        :param page:  页码
        :param mtype: 媒体类型
        :reutrn: 种子资源列表
        """
        return await self.async_run_module(
            "async_refresh_torrents", site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
        )

    def filter_torrents(
            self,
            rule_groups: List[str],
            torrent_list: List[TorrentInfo],
            mediainfo: MediaInfo = None,
    ) -> List[TorrentInfo]:
        """
        过滤种子资源
        :param rule_groups:  过滤规则组名称列表
        :param torrent_list:  资源列表
        :param mediainfo:  识别的媒体信息
        :return: 过滤后的资源列表，添加资源优先级
        """
        return self.run_module(
            "filter_torrents",
            rule_groups=rule_groups,
            torrent_list=torrent_list,
            mediainfo=mediainfo,
        )

    def download(
            self,
            content: Union[Path, str, bytes],
            download_dir: Path,
            cookie: str,
            episodes: Set[int] = None,
            category: Optional[str] = None,
            label: Optional[str] = None,
            downloader: Optional[str] = None,
    ) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        根据种子文件，选择并添加下载任务
        :param content:  种子文件地址或者磁力链接或者种子内容
        :param download_dir:  下载目录
        :param cookie:  cookie
        :param episodes:  需要下载的集数
        :param category:  种子分类
        :param label:  标签
        :param downloader:  下载器
        :return: 下载器名称、种子Hash、种子文件布局、错误原因
        """
        return self.run_module(
            "download",
            content=content,
            download_dir=download_dir,
            cookie=cookie,
            episodes=episodes,
            category=category,
            label=label,
            downloader=downloader,
        )

    def download_added(
            self,
            context: Context,
            download_dir: Path,
            torrent_content: Union[str, bytes] = None,
    ) -> None:
        """
        添加下载任务成功后，从站点下载字幕，保存到下载目录
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param download_dir:  下载目录
        :param torrent_content:  种子内容，如果有则直接使用该内容，否则从context中获取种子文件路径
        :return: None，该方法可被多个模块同时处理
        """
        return self.run_module(
            "download_added",
            context=context,
            torrent_content=torrent_content,
            download_dir=download_dir,
        )

    def list_torrents(
            self,
            status: TorrentStatus = None,
            hashs: Union[list, str] = None,
            downloader: Optional[str] = None,
            include_all_tags: bool = False,
    ) -> Optional[List[DownloaderTorrent]]:
        """
        获取下载器种子列表
        :param status:  种子状态
        :param hashs:  种子Hash
        :param downloader:  下载器
        :param include_all_tags:  是否包含未打内置标签的下载任务
        :return: 下载器中符合状态的种子列表
        """
        return self.run_module(
            "list_torrents",
            status=status,
            hashs=hashs,
            downloader=downloader,
            include_all_tags=include_all_tags,
        )

    def transfer(
            self,
            fileitem: FileItem,
            meta: MetaBase,
            mediainfo: MediaInfo,
            target_directory: TransferDirectoryConf = None,
            target_storage: Optional[str] = None,
            target_path: Path = None,
            transfer_type: Optional[str] = None,
            scrape: bool = None,
            library_type_folder: bool = None,
            library_category_folder: bool = None,
            episodes_info: List[TmdbEpisode] = None,
            source_oper: Callable = None,
            target_oper: Callable = None,
            preview: bool = False,
    ) -> Optional[TransferInfo]:
        """
        文件转移
        :param fileitem:  文件信息
        :param meta: 预识别的元数据
        :param mediainfo:  识别的媒体信息
        :param target_directory:  目标目录配置
        :param target_storage:  目标存储
        :param target_path:  目标路径
        :param transfer_type:  转移模式
        :param scrape: 是否刮削元数据
        :param library_type_folder: 是否按类型创建目录
        :param library_category_folder: 是否按类别创建目录
        :param episodes_info: 当前季的全部集信息
        :param source_oper:  源存储操作类
        :param target_oper:  目标存储操作类
        :param preview: 是否仅预览，不执行实际转移
        :return: {path, target_path, message}
        """
        return self.run_module(
            "transfer",
            fileitem=fileitem,
            meta=meta,
            mediainfo=mediainfo,
            target_directory=target_directory,
            target_path=target_path,
            target_storage=target_storage,
            transfer_type=transfer_type,
            scrape=scrape,
            library_type_folder=library_type_folder,
            library_category_folder=library_category_folder,
            episodes_info=episodes_info,
            source_oper=source_oper,
            target_oper=target_oper,
            preview=preview,
        )

    def transfer_completed(self, hashs: str, downloader: Optional[str] = None) -> None:
        """
        下载器转移完成后的处理
        :param hashs:  种子Hash
        :param downloader:  下载器
        """
        return self.run_module("transfer_completed", hashs=hashs, downloader=downloader)

    def remove_torrents(
            self,
            hashs: Union[str, list],
            delete_file: bool = True,
            downloader: Optional[str] = None,
    ) -> bool:
        """
        删除下载器种子
        :param hashs:  种子Hash
        :param delete_file: 是否删除文件
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module(
            "remove_torrents",
            hashs=hashs,
            delete_file=delete_file,
            downloader=downloader,
        )

    def start_torrents(
            self, hashs: Union[list, str], downloader: Optional[str] = None
    ) -> bool:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("start_torrents", hashs=hashs, downloader=downloader)

    def stop_torrents(
            self, hashs: Union[list, str], downloader: Optional[str] = None
    ) -> bool:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("stop_torrents", hashs=hashs, downloader=downloader)

    def set_torrents_tag(
            self, hashs: Union[list, str], tags: list, downloader: Optional[str] = None
    ) -> bool:
        """
        设置种子标签
        :param hashs:  种子Hash
        :param tags:  标签列表
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("set_torrents_tag", hashs=hashs, tags=tags, downloader=downloader)

    def update_torrent(
            self,
            hash_string: str,
            downloader: Optional[str] = None,
            download_limit: Optional[float] = None,
            upload_limit: Optional[float] = None,
            tracker_list: Optional[list] = None,
            save_path: Optional[str] = None,
            category: Optional[str] = None,
            ratio_limit: Optional[float] = None,
            seeding_time_limit: Optional[int] = None,
    ) -> Optional[Dict[str, bool]]:
        """
        修改下载任务属性。
        :param hash_string: 种子Hash
        :param downloader: 下载器
        :param download_limit: 下载限速，单位 KB/s
        :param upload_limit: 上传限速，单位 KB/s
        :param tracker_list: Tracker URL列表
        :param save_path: 保存目录
        :param category: 分类
        :param ratio_limit: 分享率限制
        :param seeding_time_limit: 做种时间限制，单位分钟
        :return: 各项修改结果
        """
        return self.run_module(
            "update_torrent",
            hash_string=hash_string,
            downloader=downloader,
            download_limit=download_limit,
            upload_limit=upload_limit,
            tracker_list=tracker_list,
            save_path=save_path,
            category=category,
            ratio_limit=ratio_limit,
            seeding_time_limit=seeding_time_limit,
        )

    def get_torrent_trackers(
            self,
            hash_string: str,
            downloader: Optional[str] = None,
    ) -> Optional[Dict[str, List[str]]]:
        """
        查询下载任务Tracker列表。
        :param hash_string: 种子Hash
        :param downloader: 下载器
        :return: 下载器名称到Tracker列表的映射
        """
        return self.run_module(
            "get_torrent_trackers",
            hash_string=hash_string,
            downloader=downloader,
        )

    def torrent_files(
            self, tid: str, downloader: Optional[str] = None
    ) -> Optional[Union[TorrentFilesList, List[File]]]:
        """
        获取种子文件
        :param tid:  种子Hash
        :param downloader:  下载器
        :return: 种子文件
        """
        return self.run_module("torrent_files", tid=tid, downloader=downloader)

    def media_exists(
            self,
            mediainfo: MediaInfo,
            itemid: Optional[str] = None,
            server: Optional[str] = None,
    ) -> Optional[ExistMediaInfo]:
        """
        判断媒体文件是否存在
        :param mediainfo:  识别的媒体信息
        :param itemid:  媒体服务器ItemID
        :param server:  媒体服务器
        :return: 如不存在返回None，存在时返回信息，包括每季已存在所有集{type: movie/tv, seasons: {season: [episodes]}}
        """
        return self.run_module(
            "media_exists", mediainfo=mediainfo, itemid=itemid, server=server
        )

    def media_files(self, mediainfo: MediaInfo) -> Optional[List[FileItem]]:
        """
        获取媒体文件清单
        :param mediainfo:  识别的媒体信息
        :return: 媒体文件列表
        """
        return self.run_module("media_files", mediainfo=mediainfo)

    def post_message(
            self,
            message: Optional[Notification] = None,
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[MediaInfo] = None,
            torrentinfo: Optional[TorrentInfo] = None,
            transferinfo: Optional[TransferInfo] = None,
            **kwargs,
    ) -> None:
        """
        发送消息
        :param message:  Notification实例
        :param meta:  元数据
        :param mediainfo:  媒体信息
        :param torrentinfo:  种子信息
        :param transferinfo:  文件整理信息
        :param kwargs:  其他参数(覆盖业务对象属性值)
        :return: 成功或失败
        """
        # 添加格式化的时间参数
        kwargs.setdefault("current_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # 渲染消息
        message = MessageTemplateHelper.render(
            message=message,
            meta=meta,
            mediainfo=mediainfo,
            torrentinfo=torrentinfo,
            transferinfo=transferinfo,
            **kwargs,
        )
        # 检查消息是否有效
        if not message:
            logger.warning("消息为空，跳过发送")
            return
        if message.save_history:
            self.messageoper.add(**message.model_dump())
        dispatch_message = self._normalize_notification_for_dispatch(message)
        # 发送消息按设置隔离
        if not dispatch_message.userid and dispatch_message.mtype:
            # 消息隔离设置
            notify_action = ServiceConfigHelper.get_notification_switch(
                dispatch_message.mtype
            )
            if notify_action:
                # 'admin' 'user,admin' 'user' 'all'
                actions = notify_action.split(",")
                # 是否已发送管理员标志
                admin_sended = False
                send_orignal = False
                useroper = UserOper()
                for action in actions:
                    send_message = copy.deepcopy(dispatch_message)
                    if action == "admin" and not admin_sended:
                        # 仅发送管理员
                        logger.info(f"{send_message.mtype} 的消息已设置发送给管理员")
                        # 读取管理员消息IDS
                        send_message.targets = useroper.get_settings(settings.SUPERUSER)
                        admin_sended = True
                    elif action == "user" and send_message.username:
                        # 发送对应用户
                        logger.info(
                            f"{send_message.mtype} 的消息已设置发送给用户 {send_message.username}"
                        )
                        # 读取用户消息IDS
                        send_message.targets = useroper.get_settings(
                            send_message.username
                        )
                        if send_message.targets is None:
                            # 没有找到用户
                            if not admin_sended:
                                # 回滚发送管理员
                                logger.info(
                                    f"用户 {send_message.username} 不存在，消息将发送给管理员"
                                )
                                # 读取管理员消息IDS
                                send_message.targets = useroper.get_settings(
                                    settings.SUPERUSER
                                )
                                admin_sended = True
                            else:
                                # 管理员发过了，此消息不发了
                                logger.info(
                                    f"用户 {send_message.username} 不存在，消息无法发送到对应用户"
                                )
                                continue
                        elif send_message.username == settings.SUPERUSER:
                            # 管理员同名已发送
                            admin_sended = True
                    else:
                        # 按原消息发送全体
                        if not admin_sended:
                            send_orignal = True
                        break
                    # 按设定发送
                    self.eventmanager.send_event(
                        etype=EventType.NoticeMessage,
                        data=self._build_notice_message_data(send_message),
                    )
                    self.messagequeue.send_message(
                        "post_message", message=send_message, **kwargs
                    )
                if not send_orignal:
                    return
        # 发送消息事件
        self.eventmanager.send_event(
            etype=EventType.NoticeMessage,
            data=self._build_notice_message_data(dispatch_message),
        )
        # 按原消息发送
        self.messagequeue.send_message(
            "post_message",
            message=dispatch_message,
            immediately=True if dispatch_message.userid else False,
            **kwargs,
        )

    async def async_post_message(
            self,
            message: Optional[Notification] = None,
            meta: Optional[MetaBase] = None,
            mediainfo: Optional[MediaInfo] = None,
            torrentinfo: Optional[TorrentInfo] = None,
            transferinfo: Optional[TransferInfo] = None,
            **kwargs,
    ) -> None:
        """
        异步发送消息
        :param message:  Notification实例
        :param meta:  元数据
        :param mediainfo:  媒体信息
        :param torrentinfo:  种子信息
        :param transferinfo:  文件整理信息
        :param kwargs:  其他参数(覆盖业务对象属性值)
        :return: 成功或失败
        """
        # 添加格式化的时间参数
        kwargs.setdefault("current_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # 渲染消息
        message = MessageTemplateHelper.render(
            message=message,
            meta=meta,
            mediainfo=mediainfo,
            torrentinfo=torrentinfo,
            transferinfo=transferinfo,
            **kwargs,
        )
        # 检查消息是否有效
        if not message:
            logger.warning("消息为空，跳过发送")
            return
        if message.save_history:
            await self.messageoper.async_add(**message.model_dump())
        dispatch_message = self._normalize_notification_for_dispatch(message)
        # 发送消息按设置隔离
        if not dispatch_message.userid and dispatch_message.mtype:
            # 消息隔离设置
            notify_action = ServiceConfigHelper.get_notification_switch(
                dispatch_message.mtype
            )
            if notify_action:
                # 'admin' 'user,admin' 'user' 'all'
                actions = notify_action.split(",")
                # 是否已发送管理员标志
                admin_sended = False
                send_orignal = False
                useroper = UserOper()
                for action in actions:
                    send_message = copy.deepcopy(dispatch_message)
                    if action == "admin" and not admin_sended:
                        # 仅发送管理员
                        logger.info(f"{send_message.mtype} 的消息已设置发送给管理员")
                        # 读取管理员消息IDS
                        send_message.targets = useroper.get_settings(settings.SUPERUSER)
                        admin_sended = True
                    elif action == "user" and send_message.username:
                        # 发送对应用户
                        logger.info(
                            f"{send_message.mtype} 的消息已设置发送给用户 {send_message.username}"
                        )
                        # 读取用户消息IDS
                        send_message.targets = useroper.get_settings(
                            send_message.username
                        )
                        if send_message.targets is None:
                            # 没有找到用户
                            if not admin_sended:
                                # 回滚发送管理员
                                logger.info(
                                    f"用户 {send_message.username} 不存在，消息将发送给管理员"
                                )
                                # 读取管理员消息IDS
                                send_message.targets = useroper.get_settings(
                                    settings.SUPERUSER
                                )
                                admin_sended = True
                            else:
                                # 管理员发过了，此消息不发了
                                logger.info(
                                    f"用户 {send_message.username} 不存在，消息无法发送到对应用户"
                                )
                                continue
                        elif send_message.username == settings.SUPERUSER:
                            # 管理员同名已发送
                            admin_sended = True
                    else:
                        # 按原消息发送全体
                        if not admin_sended:
                            send_orignal = True
                        break
                    # 按设定发送
                    await self.eventmanager.async_send_event(
                        etype=EventType.NoticeMessage,
                        data=self._build_notice_message_data(send_message),
                    )
                    await self.messagequeue.async_send_message(
                        "post_message", message=send_message, **kwargs
                    )
                if not send_orignal:
                    return
        # 发送消息事件
        await self.eventmanager.async_send_event(
            etype=EventType.NoticeMessage,
            data=self._build_notice_message_data(dispatch_message),
        )
        # 按原消息发送
        await self.messagequeue.async_send_message(
            "post_message",
            message=dispatch_message,
            immediately=True if dispatch_message.userid else False,
            **kwargs,
        )

    def post_medias_message(
            self, message: Notification, medias: List[MediaInfo]
    ) -> None:
        """
        发送媒体信息选择列表
        :param message:  消息体
        :param medias:  媒体列表
        :return: 成功或失败
        """
        note_list = [media.to_dict() for media in medias]
        if message.save_history:
            self.messageoper.add(**message.model_dump(), note=note_list)
        dispatch_message = self._normalize_notification_for_dispatch(message)
        return self.messagequeue.send_message(
            "post_medias_message",
            message=dispatch_message,
            medias=medias,
            immediately=True if dispatch_message.userid else False,
        )

    def post_torrents_message(
            self, message: Notification, torrents: List[Context]
    ) -> None:
        """
        发送种子信息选择列表
        :param message:  消息体
        :param torrents:  种子列表
        :return: 成功或失败
        """
        note_list = [torrent.torrent_info.to_dict() for torrent in torrents]
        if message.save_history:
            self.messageoper.add(**message.model_dump(), note=note_list)
        dispatch_message = self._normalize_notification_for_dispatch(message)
        return self.messagequeue.send_message(
            "post_torrents_message",
            message=dispatch_message,
            torrents=torrents,
            immediately=True if dispatch_message.userid else False,
        )

    def delete_message(
            self,
            channel: MessageChannel,
            source: str,
            message_id: Union[str, int],
            chat_id: Optional[Union[str, int]] = None,
    ) -> bool:
        """
        删除消息
        :param channel: 消息渠道
        :param source: 消息源（指定特定的消息模块）
        :param message_id: 消息ID
        :param chat_id: 聊天ID（如群组ID）
        :return: 删除是否成功
        """
        return self.run_module(
            "delete_message",
            channel=channel,
            source=source,
            message_id=message_id,
            chat_id=chat_id,
        )

    def edit_message(
            self,
            channel: MessageChannel,
            source: str,
            message_id: Union[str, int],
            chat_id: Union[str, int],
            text: str,
            title: Optional[str] = None,
            buttons: Optional[List[List[dict]]] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        编辑已发送的消息
        :param channel: 消息渠道
        :param source: 消息源（指定特定的消息模块）
        :param message_id: 消息ID
        :param chat_id: 聊天ID
        :param text: 新的消息内容
        :param title: 消息标题
        :param buttons: 更新后的按钮列表
        :param metadata: 其他消息元数据
        :return: 编辑是否成功
        """
        if channel == MessageChannel.WebAgent:
            try:
                from app.helper.agent import edit_web_agent_message

                return edit_web_agent_message(
                    user_id=str((metadata or {}).get("userid") or ""),
                    message_id=message_id,
                    title=title,
                    text=text,
                    buttons=buttons,
                )
            except Exception as err:
                logger.debug(f"编辑 WebAgent 消息失败: {err}")
                return False

        return self.run_module(
            "edit_message",
            channel=channel,
            source=source,
            message_id=message_id,
            chat_id=chat_id,
            text=text,
            title=title,
            buttons=buttons,
            metadata=metadata,
        )

    def send_direct_message(self, message: Notification) -> Optional[MessageResponse]:
        """
        直接发送消息并返回消息ID等信息（用于后续编辑消息的场景）
        不经过消息队列、不保存消息历史
        :param message: 消息体
        :return: 消息响应（包含message_id, chat_id等）
        """
        return self.run_module(
            "send_direct_message",
            message=self._normalize_notification_for_dispatch(message),
        )

    def finalize_message(
            self,
            response: MessageResponse,
    ) -> bool:
        """
        对已发送消息执行渠道收尾动作。
        例如关闭流式卡片状态；无特殊收尾的渠道直接返回 False。
        """
        return self.run_module("finalize_message", response=response)

    def metadata_img(
            self,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        return self.run_module(
            "metadata_img", mediainfo=mediainfo, season=season, episode=episode
        )

    def media_category(self) -> Optional[Dict[str, list]]:
        """
        获取媒体分类
        :return: 获取二级分类配置字典项，需包括电影、电视剧
        """
        return self.run_module("media_category")

    def category_config(self) -> CategoryConfig:
        """
        获取分类策略配置
        """
        return self.run_module("load_category_config")

    def save_category_config(self, config: CategoryConfig) -> bool:
        """
        保存分类策略配置
        """
        return self.run_module("save_category_config", config=config)

    def register_commands(self, commands: Dict[str, dict]) -> None:
        """
        注册菜单命令
        """
        self.run_module("register_commands", commands=commands)

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次，模块实现该接口以实现定时服务
        """
        self.run_module("scheduler_job")

    def clear_cache(self) -> None:
        """
        清理缓存，模块实现该接口响应清理缓存事件
        """
        self.run_module("clear_cache")
