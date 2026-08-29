from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, List, Mapping, Optional, Tuple, Union, cast

from app.application.site.health import get_configured_site_health_service
from app.application.site.query import get_configured_site_query_service
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.domain import site as site_rules
from app.domain.context import Context, SubtitleInfo, TorrentInfo
from app.foundation import text as text_tools
from app.foundation.reflection import ModuleHelper
from app.modules import _ModuleBase
from app.modules.indexer.parser import SiteParserBase
from app.modules.indexer.spider import SiteSpider
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.modules.indexer.spider.rousi import RousiSpider
from app.modules.indexer.spider.sunnypt import SunnyPTSpider
from app.modules.indexer.spider.tnode import TNodeSpider
from app.modules.indexer.spider.torrentleech import TorrentLeech
from app.modules.indexer.spider.yema import YemaSpider
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.site import SiteUserData
from app.schemas.types import MediaSource, MediaType, ModuleType, OtherModulesType

SPIDER_PARSER_CLASSES = {
    "TNodeSpider": TNodeSpider,
    "TorrentLeech": TorrentLeech,
    "mTorrent": MTorrentSpider,
    "Yema": YemaSpider,
    "Haidan": HaiDanSpider,
    "HDDolby": HddolbySpider,
    "RousiPro": RousiSpider,
    "SunnyPT": SunnyPTSpider,
}

_SPECIALIZED_SEARCH_ARGUMENTS = {
    "TNodeSpider": ("keyword", "page"),
    "TorrentLeech": ("keyword", "mtype", "page"),
    "mTorrent": ("keyword", "mtype", "page"),
    "SunnyPT": ("keyword", "mtype", "cat", "page"),
    "Yema": ("keyword", "mtype", "page"),
    "Haidan": ("keyword", "mtype"),
    "HDDolby": ("keyword", "mtype", "page"),
    "RousiPro": ("keyword", "mtype", "cat", "page"),
}


@dataclass(frozen=True)
class _IndexerSearchRequest:
    """一次站点搜索的解析器选择与不可变调用参数"""

    parser_class: Optional[Callable[[dict[str, Any]], Any]]
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class _IndexerSearchOutcome:
    """一次站点搜索完成后的错误状态、原始结果与耗时"""

    error_flag: bool
    result: List[dict[str, Any]]
    seconds: int


class IndexerModule(_ModuleBase):
    """
    索引模块
    """

    _site_schemas = []

    def init_module(self) -> None:
        """加载站点用户数据解析器"""
        # 加载模块
        self._site_schemas = ModuleHelper.load(
            'app.modules.indexer.parser',
            filter_func=lambda _, obj: hasattr(obj, 'schema') and getattr(obj, 'schema') is not None)
        pass

    @staticmethod
    def get_name() -> str:
        """获取模块名称"""
        return "站点索引"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Indexer

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型
        """
        return OtherModulesType.Indexer

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def stop(self):
        """停止索引模块"""
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        sites = SitesHelper().get_indexers()
        if not sites:
            return False, "未配置站点或未通过用户认证"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """索引模块无需独立开关配置"""
        pass

    def site_subtitle_links(self, context: Context) -> Optional[List[str]]:
        """
        解析采用API访问的站点的字幕下载链接，非API站点返回None交由页面解析模块处理
        :param context: 上下文，包括识别信息、媒体信息、种子信息
        :return: 字幕下载链接列表，不适用时返回None
        """
        torrent = context.torrent_info
        if torrent.site is None:
            return None
        site = get_configured_site_query_service().get_sync(torrent.site)
        if not site:
            return None
        indexer = SitesHelper().get_indexer(site.domain)
        if not indexer:
            return None
        if indexer.get("parser") == "mTorrent":
            return MTorrentSpider(indexer).get_subtitle_links(
                torrent.page_url
            )
        # TODO 其它采用API访问的站点
        return None

    @staticmethod
    def __search_check(site: dict, search_word: Optional[str] = None) -> bool:
        """
        检查是否可以执行搜索
        """
        # 可能为关键字或ttxxxx
        if search_word \
                and site.get('language') == "en" \
                and text_tools.contains_chinese(search_word):
            # 不支持中文
            logger.warn(f"{site.get('name')} 不支持中文搜索")
            return False

        # 站点流控
        state, msg = SitesHelper().check(site_rules.extract_domain(site.get("domain")))
        if state:
            logger.warn(msg)
            return False

        return True

    @staticmethod
    def __clear_search_text(text: Optional[str]) -> Optional[str]:
        """
        清理搜索文本
        :param text: 需要清理的文本
        :return: 清理后的文本
        """
        if not text:
            return text
        # 去除特殊字符和多余空格
        return text_tools.remove_punctuation(text, replacement=" ", allow_space=True)

    @staticmethod
    def __indexer_statistic(site: dict, error_flag: bool = False, seconds: int = 0) -> None:
        """
        索引器统计
        """
        domain = site_rules.extract_domain(site.get("domain"))
        if error_flag:
            get_configured_site_health_service().fail(domain)
        else:
            get_configured_site_health_service().success(
                domain=domain,
                seconds=seconds,
            )

    @staticmethod
    async def __async_indexer_statistic(site: dict, error_flag: bool = False, seconds: int = 0) -> None:
        """
        异步索引器统计
        """
        domain = site_rules.extract_domain(site.get("domain"))
        if error_flag:
            await get_configured_site_health_service().async_fail(domain)
        else:
            await get_configured_site_health_service().async_success(
                domain=domain,
                seconds=seconds,
            )

    @staticmethod
    def __parse_result(site: dict, result_array: list, seconds: int) -> TorrentInfo:
        """
        解析搜索结果为 TorrentInfo 对象
        """
        if not result_array or len(result_array) == 0:
            logger.warn(f"{site.get('name')} 未搜索到数据，耗时 {seconds} 秒")
            return []
        logger.info(
            f"{site.get('name')} 搜索完成，耗时 {seconds} 秒，返回数据：{len(result_array)}")
        torrents = []
        for result in result_array:
            result = dict(result)
            legacy_imdb_id = result.pop("imdbid", None)
            media_source, media_id = resolve_media_identity(media=result)
            if not media_source and legacy_imdb_id:
                media_source, media_id = MediaSource.IMDb, str(legacy_imdb_id)
            result["media_source"] = media_source
            result["media_id"] = media_id
            torrents.append(TorrentInfo(
                site=site.get("id"),
                site_name=site.get("name"),
                site_cookie=site.get("cookie"),
                site_ua=site.get("ua"),
                site_proxy=site.get("proxy"),
                site_order=site.get("pri"),
                site_downloader=site.get("downloader"),
                **result,
            ))
        return torrents

    @staticmethod
    def __parse_subtitle_result(site: dict, result_array: list, seconds: int) -> List[SubtitleInfo]:
        """
        解析字幕搜索结果为 SubtitleInfo 对象。
        """
        if not result_array or len(result_array) == 0:
            logger.warn(f"{site.get('name')} 未搜索到字幕，耗时 {seconds} 秒")
            return []
        logger.info(
            f"{site.get('name')} 字幕搜索完成，耗时 {seconds} 秒，返回数据：{len(result_array)}")
        return [SubtitleInfo(site=site.get("id"),
                             site_name=site.get("name"),
                             site_cookie=site.get("cookie"),
                             site_ua=site.get("ua"),
                             site_proxy=site.get("proxy"),
                             site_order=site.get("pri"),
                             **result) for result in result_array if
                result.get("language") and result.get("enclosure") and result.get("title") and result.get("size")]

    @staticmethod
    def get_search_page_size(site: dict, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取站点搜索单页容量；None 表示当前搜索入口不支持可靠翻页。
        """
        site = site or {}
        site_parser = site.get("parser")
        if site_parser in SPIDER_PARSER_CLASSES:
            return SPIDER_PARSER_CLASSES[site_parser].get_search_page_size(keyword=keyword)
        try:
            page_size = int(site.get("result_num") or SiteSpider.default_result_num())
        except (TypeError, ValueError):
            page_size = SiteSpider.default_result_num()
        return page_size if page_size > 0 else SiteSpider.default_result_num()

    @staticmethod
    def __create_search_request(
        site: dict[str, Any],
        keyword: Optional[str],
        mtype: Optional[MediaType] = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
        search_type: str = "torrents",
    ) -> Optional[_IndexerSearchRequest]:
        """校验搜索条件并生成同步、异步共用的解析器调用请求"""
        if search_type == "subtitles" and not site.get("subtitles"):
            return None
        if not IndexerModule.__search_check(site, keyword):
            return None

        search_word = IndexerModule.__clear_search_text(keyword)
        if search_type == "subtitles":
            return _IndexerSearchRequest(
                parser_class=None,
                arguments=MappingProxyType({
                    "search_word": search_word,
                    "indexer": site,
                    "page": page,
                    "search_type": search_type,
                }),
            )

        parser_name = str(site.get("parser") or "")
        parser_class = SPIDER_PARSER_CLASSES.get(parser_name)
        argument_names = _SPECIALIZED_SEARCH_ARGUMENTS.get(parser_name)
        if parser_class and argument_names:
            available_arguments = {
                "keyword": search_word,
                "mtype": mtype,
                "cat": cat,
                "page": page,
            }
            return _IndexerSearchRequest(
                parser_class=parser_class,
                arguments=MappingProxyType({
                    name: available_arguments[name]
                    for name in argument_names
                }),
            )

        return _IndexerSearchRequest(
            parser_class=None,
            arguments=MappingProxyType({
                "search_word": search_word,
                "indexer": site,
                "mtype": mtype,
                "cat": cat,
                "page": page,
            }),
        )

    @staticmethod
    def __execute_search(
        site: dict[str, Any],
        request: _IndexerSearchRequest,
    ) -> Tuple[bool, List[dict[str, Any]]]:
        """通过同步解析器执行已冻结的搜索请求"""
        if request.parser_class:
            return cast(
                Tuple[bool, List[dict[str, Any]]],
                request.parser_class(site).search(**request.arguments),
            )
        return IndexerModule.__spider_search(**request.arguments)

    @staticmethod
    async def __async_execute_search(
        site: dict[str, Any],
        request: _IndexerSearchRequest,
    ) -> Tuple[bool, List[dict[str, Any]]]:
        """通过异步解析器执行已冻结的搜索请求"""
        if request.parser_class:
            return cast(
                Tuple[bool, List[dict[str, Any]]],
                await request.parser_class(site).async_search(**request.arguments),
            )
        return await IndexerModule.__async_spider_search(**request.arguments)

    @staticmethod
    def __create_search_outcome(
        start_time: datetime,
        error_flag: bool,
        result: List[dict[str, Any]],
    ) -> _IndexerSearchOutcome:
        """把同步、异步 I/O 结果整理为共用的搜索完成状态"""
        return _IndexerSearchOutcome(
            error_flag=error_flag,
            result=result,
            seconds=(datetime.now() - start_time).seconds,
        )

    @staticmethod
    def __log_search_error(site: dict[str, Any], search_type: str, error: Exception) -> None:
        """按搜索类型记录同步、异步共用的操作失败信息"""
        resource_name = "字幕" if search_type == "subtitles" else ""
        logger.error(f"{site.get('name')} {resource_name}搜索出错：{str(error)}")

    def search_torrents(self, site: dict,
                        keyword: str = None,
                        mtype: MediaType = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        搜索一个站点
        :param site:  站点
        :param keyword:  搜索关键词
        :param mtype:  媒体类型
        :param cat:  分类
        :param page:  页码
        :return: 资源列表
        """

        # 索引结果
        result = []
        # 开始计时
        start_time = datetime.now()
        # 错误标志
        error_flag = False

        request = self.__create_search_request(
            site=site,
            keyword=keyword,
            mtype=mtype,
            cat=cat,
            page=page,
        )
        if not request:
            return []

        # 开始搜索
        try:
            error_flag, result = self.__execute_search(site, request)
        except Exception as err:
            self.__log_search_error(site, "torrents", err)

        outcome = self.__create_search_outcome(start_time, error_flag, result)

        # 统计索引情况
        self.__indexer_statistic(
            site=site,
            error_flag=outcome.error_flag,
            seconds=outcome.seconds,
        )

        # 返回结果
        return self.__parse_result(
            site=site,
            result_array=outcome.result,
            seconds=outcome.seconds,
        )

    def search_subtitles(self, site: dict,
                         keyword: str = None,
                         page: Optional[int] = 0) -> List[SubtitleInfo]:
        """
        搜索一个站点的字幕资源。
        :param site: 站点
        :param keyword: 搜索关键词
        :param page: 页码
        :return: 字幕列表
        """

        result = []
        start_time = datetime.now()
        error_flag = False

        request = self.__create_search_request(
            site=site,
            keyword=keyword,
            page=page,
            search_type="subtitles",
        )
        if not request:
            return []

        try:
            error_flag, result = self.__execute_search(site, request)
        except Exception as err:
            self.__log_search_error(site, "subtitles", err)

        outcome = self.__create_search_outcome(start_time, error_flag, result)
        self.__indexer_statistic(
            site=site,
            error_flag=outcome.error_flag,
            seconds=outcome.seconds,
        )
        return self.__parse_subtitle_result(
            site=site,
            result_array=outcome.result,
            seconds=outcome.seconds,
        )

    async def async_search_torrents(self, site: dict,
                                    keyword: str = None,
                                    mtype: MediaType = None,
                                    cat: Optional[str] = None,
                                    page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        异步搜索一个站点
        :param site:  站点
        :param keyword:  搜索关键词
        :param mtype:  媒体类型
        :param cat:  分类
        :param page:  页码
        :return: 资源列表
        """

        # 索引结果
        result = []
        # 开始计时
        start_time = datetime.now()
        # 错误标志
        error_flag = False

        request = self.__create_search_request(
            site=site,
            keyword=keyword,
            mtype=mtype,
            cat=cat,
            page=page,
        )
        if not request:
            return []

        # 开始搜索
        try:
            error_flag, result = await self.__async_execute_search(site, request)
        except Exception as err:
            self.__log_search_error(site, "torrents", err)

        outcome = self.__create_search_outcome(start_time, error_flag, result)

        # 统计索引情况
        await self.__async_indexer_statistic(
            site=site,
            error_flag=outcome.error_flag,
            seconds=outcome.seconds,
        )

        # 返回结果
        return self.__parse_result(
            site=site,
            result_array=outcome.result,
            seconds=outcome.seconds,
        )

    async def async_search_subtitles(self, site: dict,
                                     keyword: str = None,
                                     page: Optional[int] = 0) -> List[SubtitleInfo]:
        """
        异步搜索一个站点的字幕资源。
        :param site: 站点
        :param keyword: 搜索关键词
        :param page: 页码
        :return: 字幕列表
        """

        result = []
        start_time = datetime.now()
        error_flag = False

        request = self.__create_search_request(
            site=site,
            keyword=keyword,
            page=page,
            search_type="subtitles",
        )
        if not request:
            return []

        try:
            error_flag, result = await self.__async_execute_search(site, request)
        except Exception as err:
            self.__log_search_error(site, "subtitles", err)

        outcome = self.__create_search_outcome(start_time, error_flag, result)
        await self.__async_indexer_statistic(
            site=site,
            error_flag=outcome.error_flag,
            seconds=outcome.seconds,
        )
        return self.__parse_subtitle_result(
            site=site,
            result_array=outcome.result,
            seconds=outcome.seconds,
        )

    @staticmethod
    def __spider_search(indexer: dict,
                        search_word: Optional[str] = None,
                        mtype: MediaType = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0,
                        search_type: Optional[str] = "torrents") -> Tuple[bool, List[dict]]:
        """
        根据关键字搜索单个站点
        :param: indexer: 站点配置
        :param: search_word: 关键字
        :param: cat: 分类
        :param: page: 页码
        :param: mtype: 媒体类型
        :param: timeout: 超时时间
        :return: 是否发生错误, 种子列表
        """
        _spider = SiteSpider(indexer=indexer,
                             keyword=search_word,
                             mtype=mtype,
                             cat=cat,
                             page=page,
                             search_type=search_type)

        try:
            result = _spider.get_torrents()
            return _spider.is_error, result
        finally:
            del _spider

    @staticmethod
    async def __async_spider_search(indexer: dict,
                                    search_word: Optional[str] = None,
                                    mtype: MediaType = None,
                                    cat: Optional[str] = None,
                                    page: Optional[int] = 0,
                                    search_type: Optional[str] = "torrents") -> Tuple[bool, List[dict]]:
        """
        异步根据关键字搜索单个站点
        :param: indexer: 站点配置
        :param: search_word: 关键字
        :param: cat: 分类
        :param: page: 页码
        :param: mtype: 媒体类型
        :param: timeout: 超时时间
        :return: 是否发生错误, 种子列表
        """
        _spider = SiteSpider(indexer=indexer,
                             keyword=search_word,
                             mtype=mtype,
                             cat=cat,
                             page=page,
                             search_type=search_type)

        try:
            result = await _spider.async_get_torrents()
            return _spider.is_error, result
        finally:
            del _spider

    def refresh_torrents(self, site: dict,
                         keyword: Optional[str] = None,
                         cat: Optional[str] = None,
                         page: Optional[int] = 0,
                         mtype: Optional[MediaType] = None) -> Optional[List[TorrentInfo]]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :param keyword:  关键字
        :param cat:  分类
        :param page:  页码
        :param mtype: 媒体类型
        :reutrn: 种子资源列表
        """
        return self.search_torrents(
            site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
        )

    async def async_refresh_torrents(self, site: dict,
                                     keyword: Optional[str] = None,
                                     cat: Optional[str] = None,
                                     page: Optional[int] = 0,
                                     mtype: Optional[MediaType] = None) -> Optional[List[TorrentInfo]]:
        """
        异步获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :param keyword:  关键字
        :param cat:  分类
        :param page:  页码
        :param mtype: 媒体类型
        :reutrn: 种子资源列表
        """
        return await self.async_search_torrents(
            site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
        )

    def refresh_userdata(self, site: dict) -> Optional[SiteUserData]:
        """
        刷新站点的用户数据
        :param site:  站点
        :return: 用户数据
        """

        def __get_site_obj(schema_value: Optional[str] = None) -> Optional[SiteParserBase]:
            """
            获取站点解析器
            :param schema_value: 指定 schema, 默认取站点声明的 schema
            """
            schema_value = schema_value or site.get("schema")
            for site_schema in self._site_schemas:
                if site_schema.schema and site_schema.schema.value == schema_value:
                    return site_schema(
                        site_name=site.get("name"),
                        url=site.get("url"),
                        site_cookie=site.get("cookie"),
                        apikey=site.get("apikey"),
                        token=site.get("token"),
                        ua=site.get("ua"),
                        proxy=site.get("proxy"),
                        api_url=site.get("api_url"))
            return None

        # 按站点声明的 schema 获取解析器
        site_obj = __get_site_obj()
        if not site_obj:
            if not site.get("public"):
                logger.warn(f"站点  {site.get('name')} 未找到站点解析器，schema：{site.get('schema')}")
            return None

        # 获取用户数据
        try:
            logger.info(f"站点 {site.get('name')} 开始以 {site.get('schema')} 模型解析数据...")
            site_obj.parse()
            logger.debug(f"站点 {site.get('name')} 数据解析完成")
            # 站点声明的 schema 解析失败(userid 为空)时, 自动尝试其他解析器,
            # 兼容资源文件 schema 标注错误/变种站点的场景
            if not site_obj.userid and not site.get("public"):
                tried = {site.get("schema")}
                for site_schema in self._site_schemas:
                    if not site_schema.schema or site_schema.schema.value in tried:
                        continue
                    tried.add(site_schema.schema.value)
                    logger.info(f"站点 {site.get('name')} schema {site.get('schema')} 解析失败, "
                                f"尝试 {site_schema.schema.value} 模型...")
                    alt_obj = __get_site_obj(site_schema.schema.value)
                    if not alt_obj:
                        continue
                    try:
                        alt_obj.parse()
                    except Exception as e:
                        logger.error(f"站点 {site.get('name')} 以 {site_schema.schema.value} 解析失败: {str(e)}")
                        continue
                    if alt_obj.userid:
                        site_obj = alt_obj
                        logger.info(f"站点 {site.get('name')} 改用 {site_schema.schema.value} 模型解析成功")
                        break
            return SiteUserData(
                domain=site_rules.extract_domain(site.get("url")),
                userid=site_obj.userid,
                username=site_obj.username,
                user_level=site_obj.user_level,
                join_at=site_obj.join_at,
                upload=site_obj.upload,
                download=site_obj.download,
                ratio=site_obj.ratio,
                bonus=site_obj.bonus,
                seeding=site_obj.seeding,
                seeding_size=site_obj.seeding_size,
                seeding_info=site_obj.seeding_info.copy() if site_obj.seeding_info else [],
                leeching=site_obj.leeching,
                leeching_size=site_obj.leeching_size,
                message_unread=site_obj.message_unread,
                message_unread_contents=site_obj.message_unread_contents.copy() if site_obj.message_unread_contents else [],
                updated_day=datetime.now().strftime('%Y-%m-%d'),
                err_msg=site_obj.err_msg
            )
        finally:
            site_obj.clear()
