from __future__ import annotations

import pickle
import traceback
from abc import ABCMeta
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple, Union, cast

from app.application.chain.context import ChainRuntimeContext, get_chain_runtime_context
from app.application.configuration import (
    ChainRuntimeConfig,
    get_chain_runtime_config_snapshot,
)
from app.chain._messaging import MessageProcessingMixin, NotificationMixin
from app.chain._recognition import RecognitionMixin
from app.domain.context import Context, MediaInfo, MusicInfo, SubtitleInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.context import MediaPerson
from app.schemas.exception import RateLimitExceededException
from app.schemas.mediaserver import ExistMediaInfo, WebhookEventInfo
from app.schemas.message import IncomingMessage
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import DownloaderFile, DownloaderTorrent, TransferInfo
from app.schemas.types import (
    EventType,
    MediaImageType,
    MediaSourceSelection,
    MediaType,
    TorrentStatus,
)
from app.schemas.workflow import FileItem

if TYPE_CHECKING:
    from app.application.transfer.workflow import TransferPlanCheckpoint, TransferPlanningInput


class ChainBase(RecognitionMixin, MessageProcessingMixin, NotificationMixin, metaclass=ABCMeta):
    """
    处理链基类
    """

    def __init__(self, runtime_context: Optional[ChainRuntimeContext] = None):
        """
        公共初始化；未显式传入上下文时继续使用兼容运行时 provider。
        """
        context = runtime_context or get_chain_runtime_context()
        self.modulemanager = context.module_manager
        self.eventmanager = context.event_manager
        self.messageoper = context.message_oper
        self.messagehelper = context.message_helper
        self.pluginmanager = context.plugin_manager
        self.filecache = context.file_cache
        self.async_filecache = context.async_file_cache
        self.site_repository = context.site_repository
        self.subscription_repository = context.subscription_repository
        self.subscription_search_repository = context.subscription_search_repository
        self.subscription_mutation_scope = context.subscription_mutation_scope
        self.sync_subscription_mutation_scope = context.sync_subscription_mutation_scope
        self.subscription_delete_scope = context.subscription_delete_scope
        self.sync_subscription_delete_scope = context.sync_subscription_delete_scope
        self.subscription_completion_scope = context.subscription_completion_scope
        self.rule_group_mutation_scope = context.rule_group_mutation_scope
        self.site_reference_mutation_scope = context.site_reference_mutation_scope
        self.download_history_repository = context.download_history_repository
        self.transfer_history_repository = context.transfer_history_repository
        self.transfer_admission_repository = context.transfer_admission_repository
        self.transfer_execution_repository = context.transfer_execution_repository
        self.media_server_repository = context.media_server_repository
        self.download_failure_repository = context.download_failure_repository
        self.user_repository = context.user_repository
        self.classification_service = context.classification_service
        self.runtime_config = context.configuration
        self.stop_state = context.stop_state
        self.durable_event_writer = context.durable_event_writer
        self._module_dispatcher = context.module_dispatcher_factory(
            module_catalog=self.modulemanager,
            plugin_catalog=self.pluginmanager,
            plugin_error_handler=self.__handle_plugin_error,
            system_error_handler=self.__handle_system_error,
            rate_limit_handler=self.__handle_rate_limit_error,
        )
        self._legacy_transfer_command = context.legacy_transfer_command
        self.messagequeue = context.message_queue.bind(self.run_module)

    @property
    def runtime_config(self) -> ChainRuntimeConfig:
        """返回实例快照；兼容绕过构造器的旧调用并按需取得当前快照。"""
        configuration = getattr(self, "_runtime_config", None)
        if configuration is None:
            return get_chain_runtime_config_snapshot()
        return configuration

    @runtime_config.setter
    def runtime_config(self, configuration: ChainRuntimeConfig) -> None:
        """保存显式注入的 Chain 配置快照。"""
        self._runtime_config = configuration

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

    async def async_remove_cache(self, filename: str) -> None:
        """
        异步删除缓存，同时删除Redis和本地缓存
        """
        await self.async_filecache.delete(filename)

    def __handle_plugin_error(self, err: Exception, plugin_id: str, plugin_name: str, method: str, **kwargs):
        """
        处理插件模块执行错误
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.error(f"运行插件 {plugin_id} 模块 {method} 出错：{str(err)}\n{traceback.format_exc()}")
        self.messagehelper.put(title=f"{plugin_name} 发生了错误", message=str(err), role="plugin")
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

    def __handle_system_error(self, err: Exception, module_id: str, module_name: str, method: str, **kwargs):
        """
        处理系统模块执行错误
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.error(f"运行模块 {module_id}.{method} 出错：{str(err)}\n{traceback.format_exc()}")
        self.messagehelper.put(title=f"{module_name}发生了错误", message=str(err), role="system")
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
        err: RateLimitExceededException, source_type: str, source_id: str, method: str, **kwargs
    ) -> None:
        """
        处理本地限流跳过，避免预期的限流状态进入系统错误告警。
        """
        if kwargs.get("raise_exception"):
            raise err
        logger.info(f"{source_type} {source_id}.{method} 已限流，跳过执行：{str(err)}")

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
        return self._module_dispatcher.dispatch(method, *args, **kwargs)

    def run_module_strict(
        self,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """运行模块并传播 provider 异常，供必须区分空结果与查询失败的能力使用。"""
        return self._module_dispatcher.dispatch_strict(method, *args, **kwargs)

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
        return await self._module_dispatcher.async_dispatch(
            method,
            *args,
            **kwargs,
        )

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
        return self.run_module("match_tmdbinfo", name=name, mtype=mtype, year=year, season=season)

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
        return await self.async_run_module("async_match_tmdbinfo", name=name, mtype=mtype, year=year, season=season)

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

    def tmdb_info(self, tmdbid: int, mtype: MediaType, season: Optional[int] = None) -> Optional[dict]:
        """
        获取TMDB信息
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季
        :return: TVDB信息
        """
        return self.run_module("tmdb_info", tmdbid=tmdbid, mtype=mtype, season=season)

    async def async_tmdb_info(self, tmdbid: int, mtype: MediaType, season: Optional[int] = None) -> Optional[dict]:
        """
        获取TMDB信息（异步版本）
        :param tmdbid: int
        :param mtype:  媒体类型
        :param season: 季
        :return: TVDB信息
        """
        return await self.async_run_module("async_tmdb_info", tmdbid=tmdbid, mtype=mtype, season=season)

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

    def message_parser(self, source: str, body: Any, form: Any, args: Any) -> Optional[IncomingMessage]:
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
        return self.run_module("message_parser", source=source, body=body, form=form, args=args)

    def webhook_parser(self, body: Any, form: Any, args: Any) -> Optional[WebhookEventInfo]:
        """
        解析Webhook报文体
        :param body:  请求体
        :param form:  请求表单
        :param args:  请求参数
        :return: 字典，解析为消息时需要包含：title、text、image
        """
        return self.run_module("webhook_parser", body=body, form=form, args=args)

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        return self.run_module("search_medias", meta=meta, media_source=media_source)

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        return await self.async_run_module("async_search_medias", meta=meta, media_source=media_source)

    def search_persons(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息
        :param name:  人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        return self.run_module("search_persons", name=name, media_source=media_source)

    async def async_search_persons(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息（异步版本）
        :param name:  人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        return await self.async_run_module("async_search_persons", name=name, media_source=media_source)

    def search_collections(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息
        :param name:  集合名称
        :param media_source: 请求级搜索数据源
        :return: 合集信息列表
        """
        return self.run_module("search_collections", name=name, media_source=media_source)

    async def async_search_collections(
        self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息（异步版本）
        :param name:  集合名称
        :param media_source: 请求级搜索数据源
        :return: 合集信息列表
        """
        return await self.async_run_module("async_search_collections", name=name, media_source=media_source)

    def get_search_page_size(
        self,
        site: dict,
        keyword: Optional[str] = None,
    ) -> Optional[int]:
        """
        获取站点搜索单页容量；返回 None 表示当前搜索入口不支持可靠翻页。
        """
        return self.run_module("get_search_page_size", site=site, keyword=keyword)

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
        return self.run_module("search_torrents", site=site, keyword=keyword, mtype=mtype, page=page)

    def search_plugin_torrents(
        self,
        keyword: str,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """仅搜索插件提供的资源源，避免依赖或重复绑定站点索引器。"""
        return (
            self._module_dispatcher.execute_plugin_modules(
                "search_torrents", None, site={}, keyword=keyword, mtype=mtype, page=page
            )
            or []
        )

    def search_site_torrents(
        self,
        site: dict,
        keyword: str,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """仅搜索指定站点索引器；插件资源源由搜索链统一调用一次。"""
        return (
            self._module_dispatcher.execute_system_modules(
                "search_torrents", None, site=site, keyword=keyword, mtype=mtype, page=page
            )
            or []
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
        return self.run_module("search_subtitles", site=site, keyword=keyword, page=page)

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
        return await self.async_run_module("async_search_torrents", site=site, keyword=keyword, mtype=mtype, page=page)

    async def async_search_plugin_torrents(
        self,
        keyword: str,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """异步搜索插件提供的资源源。"""
        return (
            await self._module_dispatcher.async_execute_plugin_modules(
                "async_search_torrents", None, site={}, keyword=keyword, mtype=mtype, page=page
            )
            or []
        )

    async def async_search_site_torrents(
        self,
        site: dict,
        keyword: str,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """异步搜索指定站点索引器。"""
        return (
            await self._module_dispatcher.async_execute_system_modules(
                "async_search_torrents", None, site=site, keyword=keyword, mtype=mtype, page=page
            )
            or []
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
        return await self.async_run_module("async_search_subtitles", site=site, keyword=keyword, page=page)

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
        return self.run_module("refresh_torrents", site=site, keyword=keyword, cat=cat, page=page, mtype=mtype)

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
        添加下载任务成功后的模块附加处理分发，站点字幕下载由 DownloadChain 另行编排
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param download_dir:  下载目录
        :param torrent_content: 种子内容，如果有则直接使用该内容，否则从 context 中获取种子文件路径
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
        mediainfo: Union[MediaInfo, MusicInfo],
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
        经启动组合根注入的 canonical durable command 执行旧整理 ABI。
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
        if self._legacy_transfer_command is None:
            raise RuntimeError("旧整理兼容命令尚未由启动组合根配置")
        return self._legacy_transfer_command(
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

    def plan_transfer(
        self,
        fileitem: FileItem,
        meta: MetaBase,
        mediainfo: Union[MediaInfo, MusicInfo],
        target_directory: Optional[TransferDirectoryConf] = None,
        target_storage: Optional[str] = None,
        target_path: Optional[Path] = None,
        transfer_type: Optional[str] = None,
        scrape: Optional[bool] = None,
        library_type_folder: Optional[bool] = None,
        library_category_folder: Optional[bool] = None,
        episodes_info: Optional[List[TmdbEpisode]] = None,
        source_oper: Any = None,
        preview: bool = False,
        planning_input: Optional[TransferPlanningInput] = None,
    ) -> Optional[TransferPlanCheckpoint]:
        """调用文件管理模块生成无文件写入的冻结整理计划。"""
        return cast(
            "Optional[TransferPlanCheckpoint]",
            self.run_module_strict(
                "plan_transfer",
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
                preview=preview,
                planning_input=planning_input,
            ),
        )

    def execute_transfer_plan(
        self,
        checkpoint: TransferPlanCheckpoint,
        *,
        meta: MetaBase,
        mediainfo: Union[MediaInfo, MusicInfo],
        source_oper: Any = None,
        target_oper: Any = None,
        cleanup_media_file: Optional[Callable[[FileItem], bool]] = None,
    ) -> Optional[TransferInfo]:
        """调用文件管理模块执行已冻结计划，并注入统一安全删除能力。"""
        return cast(
            Optional[TransferInfo],
            self.run_module(
                "execute_transfer_plan",
                checkpoint=checkpoint,
                meta=meta,
                mediainfo=mediainfo,
                source_oper=source_oper,
                target_oper=target_oper,
                cleanup_media_file=cleanup_media_file,
            ),
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

    def start_torrents(self, hashs: Union[list, str], downloader: Optional[str] = None) -> bool:
        """
        开始下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("start_torrents", hashs=hashs, downloader=downloader)

    def stop_torrents(self, hashs: Union[list, str], downloader: Optional[str] = None) -> bool:
        """
        停止下载
        :param hashs:  种子Hash
        :param downloader:  下载器
        :return: bool
        """
        return self.run_module("stop_torrents", hashs=hashs, downloader=downloader)

    def set_torrents_tag(self, hashs: Union[list, str], tags: list, downloader: Optional[str] = None) -> bool:
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

    def torrent_files(self, tid: str, downloader: Optional[str] = None) -> Optional[List[DownloaderFile]]:
        """
        获取种子文件
        :param tid:  种子Hash
        :param downloader:  下载器
        :return: 与下载器 SDK 解耦的统一文件项列表
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
        return self.run_module("media_exists", mediainfo=mediainfo, itemid=itemid, server=server)

    def media_files(self, mediainfo: MediaInfo) -> Optional[List[FileItem]]:
        """
        获取媒体文件清单
        :param mediainfo:  识别的媒体信息
        :return: 媒体文件列表
        """
        return self.run_module("media_files", mediainfo=mediainfo)

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
        return self.run_module("metadata_img", mediainfo=mediainfo, season=season, episode=episode)

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
