"""
组合站点搜索爬虫和用户资料解析器，提供索引模块的同步、异步搜索入口。
"""

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, List, Mapping, Optional, Protocol, Tuple, Union, cast
from urllib.parse import urljoin

from app.application.site.health import get_configured_site_health_service
from app.application.site.observation import report_site_search_outcome, report_site_search_page
from app.application.site.query import get_configured_site_query_service

# 站点目录由外部资源在运行时提供，静态检查环境不包含其实体模块。
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.domain import site as site_rules
from app.domain import torrent as torrent_rules
from app.domain.context import Context, SubtitleInfo, TorrentInfo
from app.foundation import text as text_tools
from app.foundation.reflection import ModuleHelper
from app.modules import _ModuleBase
from app.modules.indexer.parser import SiteParserBase, SiteSchema
from app.modules.indexer.spider import SiteSpider
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.modules.indexer.spider.milkie import MilkieSpider
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.modules.indexer.spider.rousi import RousiSpider
from app.modules.indexer.spider.sunnypt import SunnyPTSpider
from app.modules.indexer.spider.tnode import TNodeSpider
from app.modules.indexer.spider.torrentleech import TorrentLeech
from app.modules.indexer.spider.yema import YemaSpider
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.media import resolve_media_identity
from app.schemas.site import SiteUserData
from app.schemas.types import MediaSource, MediaType, ModuleType, OtherModulesType


class _PagedSpider(Protocol):
    """
    专站爬虫的构造与分页容量查询合同。

    具体搜索参数仍由各站点适配器独立声明。
    """

    def __init__(self, indexer: dict[str, object]) -> None:
        """
        根据站点配置构造专站爬虫。

        :param indexer: 站点索引配置
        :return: 无返回值
        """

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        返回当前关键字下可用的固定分页容量。

        :param keyword: 搜索关键字，站点可按此决定分页能力
        :return: 单页数量；不支持可靠分页时返回 None
        """


# 站点资源声明的解析器名称到宿主专站爬虫的精确映射。
SPIDER_PARSER_CLASSES: dict[str, type[_PagedSpider]] = {
    "TNodeSpider": TNodeSpider,
    "TorrentLeech": TorrentLeech,
    "mTorrent": MTorrentSpider,
    "Yema": YemaSpider,
    "Haidan": HaiDanSpider,
    "HDDolby": HddolbySpider,
    "RousiPro": RousiSpider,
    "SunnyPT": SunnyPTSpider,
    "Milkie": MilkieSpider,
}

# 仅传递各专站搜索入口支持的参数，避免同步与异步调用契约分叉。
_SPECIALIZED_SEARCH_ARGUMENTS = {
    "TNodeSpider": ("keyword", "page"),
    "TorrentLeech": ("keyword", "mtype", "page"),
    "mTorrent": ("keyword", "mtype", "cat", "page"),
    "SunnyPT": ("keyword", "mtype", "cat", "page"),
    "Yema": ("keyword", "mtype", "cat", "page"),
    "Haidan": ("keyword", "mtype"),
    "HDDolby": ("keyword", "mtype", "cat", "page"),
    "RousiPro": ("keyword", "mtype", "cat", "page"),
    "Milkie": ("keyword", "mtype", "page"),
}

# 专站字幕入口支持的参数顺序，其余站点使用通用页面解析。
_SPECIALIZED_SUBTITLE_SEARCH_ARGUMENTS = {
    "mTorrent": ("keyword", "page", "search_type"),
}

_UNKNOWN_SEARCH_FAILURE_MESSAGE = "站点请求或页面解析失败"

# NexusPHP、Gazelle 和 Unit3d 覆盖大多数站点，其余模型多为专站适配，
# 不参与自动探测，避免对同一站点重复请求专用接口。
_COMMON_SITE_USERDATA_FALLBACK_SCHEMAS = frozenset({
    SiteSchema.Gazelle.value,
    SiteSchema.NexusPhp.value,
    SiteSchema.Unit3d.value,
})


@dataclass(frozen=True)
class _IndexerSearchRequest:
    """
    一次站点搜索的解析器选择与调用参数。

    :param parser_class: 专站爬虫构造器；为 None 时使用配置驱动的通用爬虫
    :param arguments: 已通过准入检查的搜索参数，由请求构建流程封装为只读映射
    """

    parser_class: Optional[Callable[[dict[str, Any]], Any]]
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class _IndexerSearchOutcome:
    """
    一次站点搜索完成后的结果与失败信息。

    :param error_flag: 爬虫或请求是否判定本次搜索失败
    :param result: 原始种子或字幕记录列表，未获得结果时为空
    :param seconds: 搜索耗时的整数秒部分，用于统计和日志
    :param error: 执行时捕获的异常；没有异常时为 None
    :param failure_detail: 爬虫提供的状态码或页面摘要；默认 None，仅用于展示，不参与冷却分类
    """

    error_flag: bool
    result: List[dict[str, Any]]
    seconds: int
    error: Optional[Exception] = None
    failure_detail: Optional[str] = None


def _classify_search_failure(error: Optional[Exception]) -> str:
    """
    把索引器异常归一为站点预算可执行的冷却类别。
    """
    if error is None:
        return "error"
    status_code = getattr(error, "status_code", None)
    text = f"{type(error).__name__}: {error}".lower()
    if status_code == 429 or "429" in text or "rate limit" in text:
        return "rate_limited"
    if status_code == 403 or "403" in text or "forbidden" in text:
        return "forbidden"
    if any(token in text for token in ("未登录", "登录失效", "login", "cookie")):
        return "login_invalid"
    if isinstance(error, TimeoutError) or "timeout" in text or "timed out" in text or "超时" in text:
        return "timeout"
    return "error"


def _search_failure_message(outcome: _IndexerSearchOutcome) -> Optional[str]:
    """
    返回供站点预算持久化和任务聚合展示的失败原因。
    """
    if outcome.error is not None:
        return str(outcome.error)
    if outcome.error_flag and outcome.failure_detail:
        return f"{_UNKNOWN_SEARCH_FAILURE_MESSAGE}（{outcome.failure_detail}）"
    if outcome.error_flag:
        return _UNKNOWN_SEARCH_FAILURE_MESSAGE
    return None


class IndexerModule(_ModuleBase):
    """
    提供站点种子、字幕搜索及用户数据刷新，按站点类型选择对应解析器。
    """

    _site_schemas: list[type[SiteParserBase]] = []

    def init_module(self) -> None:
        """
        加载站点用户数据解析器。
        """
        self._site_schemas = ModuleHelper.load(
            'app.modules.indexer.parser',
            filter_func=lambda _, obj: hasattr(obj, 'schema') and getattr(obj, 'schema') is not None)
        pass

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称。
        """
        return "站点索引"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Indexer

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型。
        """
        return OtherModulesType.Indexer

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 0

    def stop(self) -> None:
        """
        索引模块没有独立后台资源，此入口不执行额外停止操作。
        """
        pass

    def test(self) -> Tuple[bool, str]:
        """
        检查是否存在已配置并通过用户认证的索引站点，不发起网络探测。

        :return: 是否可用及失败原因；可用时原因为空字符串
        """
        sites = SitesHelper().get_indexers()
        if not sites:
            return False, "未配置站点或未通过用户认证"
        return True, ""

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        索引模块无需独立开关配置。

        :return: 无需初始化设置，返回 None
        """
        return None

    def site_subtitle_links(self, context: Context) -> Optional[List[str]]:
        """
        解析采用API访问的站点的字幕下载链接，非API站点返回None交由页面解析模块处理。

        :param context: 上下文，包括识别信息、媒体信息、种子信息
        :return: 字幕下载链接列表，不适用时返回None
        """
        torrent = context.torrent_info
        if torrent is None:
            return None
        if torrent.site is None:
            return None
        site = get_configured_site_query_service().get_sync(torrent.site)
        if not site:
            return None
        if not site.domain:
            return None
        indexer = SitesHelper().get_indexer(site.domain)
        if not indexer:
            return None
        if indexer.get("parser") == "mTorrent":
            if not torrent.page_url:
                return None
            return MTorrentSpider(indexer).get_subtitle_links(
                torrent.page_url
            )
        # 其他站点尚无专用字幕接口适配，返回 None 交给页面解析流程处理。
        return None

    @staticmethod
    def __search_check(site: dict[str, Any], search_word: Optional[str] = None) -> bool:
        """
        检查是否可以执行搜索。
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
            report_site_search_outcome(attempted=False, outcome="deferred", error=msg)
            return False

        return True

    @staticmethod
    def __clear_search_text(text: Optional[str]) -> Optional[str]:
        """
        清理搜索文本。

        :param text: 需要清理的文本
        :return: 清理后的文本
        """
        if not text:
            return text
        # 去除特殊字符和多余空格
        return text_tools.remove_punctuation(text, replacement=" ", allow_space=True)

    @staticmethod
    def __indexer_statistic(site: dict[str, Any], error_flag: bool = False, seconds: int = 0) -> None:
        """
        索引器统计。
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
    async def __async_indexer_statistic(site: dict[str, Any], error_flag: bool = False, seconds: int = 0) -> None:
        """
        异步索引器统计。
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
    def __parse_result(site: dict[str, Any], result_array: list[dict[str, Any]], seconds: int) -> List[TorrentInfo]:
        """
        解析搜索结果为 TorrentInfo 对象。

        :return: 站点搜索结果列表；没有匹配资源时为空列表
        """
        report_site_search_page(raw_count=len(result_array or []))
        if not result_array or len(result_array) == 0:
            logger.warn(f"{site.get('name')} 未搜索到数据，耗时 {seconds} 秒")
            return []
        logger.info(
            f"{site.get('name')} 搜索完成，耗时 {seconds} 秒，返回数据：{len(result_array)}")
        torrents: List[TorrentInfo] = []
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
                site_proxy=bool(site.get("proxy")),
                site_order=int(site.get("pri") or 0),
                site_downloader=site.get("downloader"),
                **result,
            ))
        return torrents

    @staticmethod
    def __parse_subtitle_result(site: dict[str, Any], result_array: list[dict[str, Any]], seconds: int) -> List[SubtitleInfo]:
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
                             site_proxy=bool(site.get("proxy")),
                             site_order=int(site.get("pri") or 0),
                             **result) for result in result_array if
                result.get("language") and result.get("enclosure") and result.get("title") and result.get("size")]

    @staticmethod
    def get_search_page_size(site: dict[str, Any], keyword: Optional[str] = None) -> Optional[int]:
        """
        获取站点搜索入口的单页容量。

        :param site: 站点索引配置，包含解析器类型和可选单页数量
        :param keyword: 当前搜索关键词，供专站爬虫判断能否分页
        :return: 单页记录数；专站入口没有可靠分页能力时返回 None
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
    def restore_search_torrent(site: dict[str, Any], record: dict[str, Any], allow_missing: bool = False) -> Optional[TorrentInfo]:
        """
        用当前站点凭据恢复候选下载信息。

        M-Team 可从详情页中的站内 ID 重建下载配方；其他无法重建的临时票据需重新查询。

        :param site: 当前站点配置及有效凭据
        :param record: 检查点保存的种子记录
        :param allow_missing: 是否允许返回仍缺少下载地址的候选，默认不允许
        :return: 已刷新凭据的候选；缺少下载地址且不允许保留时返回 None
        """
        torrent = TorrentInfo()
        torrent.from_dict(record)
        # M-Team 下载链接含 API Key 不入检查点，按详情页链接中的站内 ID 用当前凭据重建。
        identity = torrent_rules.resource_identity(torrent)
        if site.get("parser") == "mTorrent" and identity and identity[1].startswith("id="):
            torrent.enclosure = MTorrentSpider(site).get_download_url(identity[1][3:])
        if not torrent.enclosure and not allow_missing:
            return None
        torrent.site_cookie = site.get("cookie") or ""
        torrent.site_ua = site.get("ua") or ""
        torrent.site_proxy = bool(site.get("proxy"))
        torrent.site_downloader = site.get("downloader") or ""
        return torrent

    @staticmethod
    def __create_search_request(
        site: dict[str, Any],
        keyword: Optional[str],
        mtype: Optional[MediaType] = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
        search_type: str = "torrents",
    ) -> Optional[_IndexerSearchRequest]:
        """
        校验搜索条件并生成同步、异步共用的解析器调用请求。
        """
        if search_type == "subtitles" and not site.get("subtitles"):
            return None
        if not IndexerModule.__search_check(site, keyword):
            return None

        search_word = IndexerModule.__clear_search_text(keyword)
        parser_name = str(site.get("parser") or "")
        parser_class = SPIDER_PARSER_CLASSES.get(parser_name)
        if search_type == "subtitles":
            subtitle_argument_names = _SPECIALIZED_SUBTITLE_SEARCH_ARGUMENTS.get(parser_name)
            if parser_class and subtitle_argument_names:
                subtitle_available_arguments: dict[str, Any] = {
                    "keyword": search_word,
                    "page": page,
                    "search_type": search_type,
                }
                return _IndexerSearchRequest(
                    parser_class=parser_class,
                    arguments=MappingProxyType({
                        name: subtitle_available_arguments[name]
                        for name in subtitle_argument_names
                    }),
                )
            return _IndexerSearchRequest(
                parser_class=None,
                arguments=MappingProxyType({
                    "search_word": search_word,
                    "indexer": site,
                    "page": page,
                    "search_type": search_type,
                }),
            )

        torrent_argument_names = _SPECIALIZED_SEARCH_ARGUMENTS.get(parser_name)
        if parser_class and torrent_argument_names:
            torrent_available_arguments: dict[str, Any] = {
                "keyword": search_word,
                "mtype": mtype,
                "cat": cat,
                "page": page,
            }
            return _IndexerSearchRequest(
                parser_class=parser_class,
                arguments=MappingProxyType({
                    name: torrent_available_arguments[name]
                    for name in torrent_argument_names
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
    ) -> Tuple[bool, List[dict[str, Any]], Optional[str]]:
        """
        通过同步解析器执行已冻结的搜索请求，返回错误标志、结果与失败原因。
        """
        if request.parser_class:
            spider = request.parser_class(site)
            error_flag, result = cast(
                Tuple[bool, List[dict[str, Any]]],
                spider.search(**request.arguments),
            )
            return error_flag, result, getattr(spider, "error_detail", None)
        return IndexerModule.__spider_search(**request.arguments)

    @staticmethod
    async def __async_execute_search(
        site: dict[str, Any],
        request: _IndexerSearchRequest,
    ) -> Tuple[bool, List[dict[str, Any]], Optional[str]]:
        """
        通过异步解析器执行已冻结的搜索请求，返回错误标志、结果与失败原因。
        """
        if request.parser_class:
            spider = request.parser_class(site)
            error_flag, result = cast(
                Tuple[bool, List[dict[str, Any]]],
                await spider.async_search(**request.arguments),
            )
            return error_flag, result, getattr(spider, "error_detail", None)
        return await IndexerModule.__async_spider_search(**request.arguments)

    @staticmethod
    def __create_search_outcome(
        start_time: datetime,
        error_flag: bool,
        result: List[dict[str, Any]],
        error: Optional[Exception] = None,
        failure_detail: Optional[str] = None,
    ) -> _IndexerSearchOutcome:
        """
        把同步、异步 I/O 结果整理为共用的搜索完成状态。
        """
        return _IndexerSearchOutcome(
            error_flag=error_flag,
            result=result,
            seconds=(datetime.now() - start_time).seconds,
            failure_detail=failure_detail,
            error=error,
        )

    @staticmethod
    def __log_search_error(site: dict[str, Any], search_type: str, error: Exception) -> None:
        """
        按搜索类型记录同步、异步共用的操作失败信息。
        """
        resource_name = "字幕" if search_type == "subtitles" else ""
        logger.error(f"{site.get('name')} {resource_name}搜索出错：{str(error)}")

    def search_torrents(self, site: dict[str, Any],
                        keyword: Optional[str] = None,
                        mtype: Optional[MediaType] = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        搜索一个站点。

        :param site: 站点
        :param keyword: 搜索关键词
        :param mtype: 媒体类型
        :param cat: 分类
        :param page: 页码
        :return: 资源列表
        """

        result: List[dict[str, object]] = []
        start_time = datetime.now()
        error_flag = False

        request = self.__create_search_request(
            site=site,
            keyword=keyword,
            mtype=mtype,
            cat=cat,
            page=page,
        )
        if not request:
            report_site_search_outcome(attempted=False, outcome="skipped")
            return []

        error: Optional[Exception] = None
        failure_detail: Optional[str] = None
        try:
            error_flag, result, failure_detail = self.__execute_search(site, request)
        except Exception as err:
            error = err
            self.__log_search_error(site, "torrents", err)

        outcome = self.__create_search_outcome(start_time, error_flag, result, error, failure_detail)

        self.__indexer_statistic(
            site=site,
            error_flag=outcome.error_flag,
            seconds=outcome.seconds,
        )
        report_site_search_outcome(
            attempted=True,
            outcome=(
                _classify_search_failure(outcome.error)
                if outcome.error_flag or outcome.error is not None
                else "success"
            ),
            error=_search_failure_message(outcome),
        )

        return self.__parse_result(
            site=site,
            result_array=outcome.result,
            seconds=outcome.seconds,
        )

    def search_subtitles(self, site: dict[str, Any],
                         keyword: Optional[str] = None,
                         page: Optional[int] = 0) -> List[SubtitleInfo]:
        """
        搜索一个站点的字幕资源。

        :param site: 站点
        :param keyword: 搜索关键词
        :param page: 页码
        :return: 字幕列表
        """

        result: List[dict[str, object]] = []
        start_time = datetime.now()
        error_flag = False
        error: Optional[Exception] = None
        failure_detail: Optional[str] = None

        request = self.__create_search_request(
            site=site,
            keyword=keyword,
            page=page,
            search_type="subtitles",
        )
        if not request:
            return []

        try:
            error_flag, result, failure_detail = self.__execute_search(site, request)
        except Exception as err:
            error = err
            self.__log_search_error(site, "subtitles", err)

        outcome = self.__create_search_outcome(
            start_time,
            error_flag,
            result,
            error=error,
            failure_detail=failure_detail,
        )
        self.__indexer_statistic(
            site=site,
            error_flag=outcome.error_flag,
            seconds=outcome.seconds,
        )
        report_site_search_outcome(
            attempted=True,
            outcome=(
                _classify_search_failure(outcome.error)
                if outcome.error_flag or outcome.error is not None
                else "success"
            ),
            error=_search_failure_message(outcome),
        )
        return self.__parse_subtitle_result(
            site=site,
            result_array=outcome.result,
            seconds=outcome.seconds,
        )

    async def async_search_torrents(self, site: dict[str, Any],
                                    keyword: Optional[str] = None,
                                    mtype: Optional[MediaType] = None,
                                    cat: Optional[str] = None,
                                    page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        异步搜索一个站点。

        :param site: 站点
        :param keyword: 搜索关键词
        :param mtype: 媒体类型
        :param cat: 分类
        :param page: 页码
        :return: 资源列表
        """

        result: List[dict[str, object]] = []
        start_time = datetime.now()
        error_flag = False

        request = self.__create_search_request(
            site=site,
            keyword=keyword,
            mtype=mtype,
            cat=cat,
            page=page,
        )
        if not request:
            report_site_search_outcome(attempted=False, outcome="skipped")
            return []

        error: Optional[Exception] = None
        failure_detail: Optional[str] = None
        try:
            error_flag, result, failure_detail = await self.__async_execute_search(site, request)
        except Exception as err:
            error = err
            self.__log_search_error(site, "torrents", err)

        outcome = self.__create_search_outcome(start_time, error_flag, result, error, failure_detail)

        await self.__async_indexer_statistic(
            site=site,
            error_flag=outcome.error_flag,
            seconds=outcome.seconds,
        )
        report_site_search_outcome(
            attempted=True,
            outcome=(
                _classify_search_failure(outcome.error)
                if outcome.error_flag or outcome.error is not None
                else "success"
            ),
            error=_search_failure_message(outcome),
        )

        return self.__parse_result(
            site=site,
            result_array=outcome.result,
            seconds=outcome.seconds,
        )

    async def async_search_subtitles(self, site: dict[str, Any],
                                     keyword: Optional[str] = None,
                                     page: Optional[int] = 0) -> List[SubtitleInfo]:
        """
        异步搜索一个站点的字幕资源。

        :param site: 站点
        :param keyword: 搜索关键词
        :param page: 页码
        :return: 字幕列表
        """

        result: List[dict[str, object]] = []
        start_time = datetime.now()
        error_flag = False
        error: Optional[Exception] = None
        failure_detail: Optional[str] = None

        request = self.__create_search_request(
            site=site,
            keyword=keyword,
            page=page,
            search_type="subtitles",
        )
        if not request:
            return []

        try:
            error_flag, result, failure_detail = await self.__async_execute_search(site, request)
        except Exception as err:
            error = err
            self.__log_search_error(site, "subtitles", err)

        outcome = self.__create_search_outcome(
            start_time,
            error_flag,
            result,
            error=error,
            failure_detail=failure_detail,
        )
        await self.__async_indexer_statistic(
            site=site,
            error_flag=outcome.error_flag,
            seconds=outcome.seconds,
        )
        report_site_search_outcome(
            attempted=True,
            outcome=(
                _classify_search_failure(outcome.error)
                if outcome.error_flag or outcome.error is not None
                else "success"
            ),
            error=_search_failure_message(outcome),
        )
        return self.__parse_subtitle_result(
            site=site,
            result_array=outcome.result,
            seconds=outcome.seconds,
        )

    @staticmethod
    def __spider_search(indexer: dict[str, Any],
                        search_word: Optional[str] = None,
                        mtype: Optional[MediaType] = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0,
                        search_type: Optional[str] = "torrents") -> Tuple[bool, List[dict[str, Any]], Optional[str]]:
        """
        根据关键字搜索单个站点。

        :param indexer: 站点配置
        :param search_word: 关键字
        :param cat: 分类
        :param page: 页码
        :param mtype: 媒体类型
        :return: 是否发生错误, 种子列表, 失败原因
        """
        _spider = SiteSpider(indexer=indexer,
                             keyword=search_word,
                             mtype=mtype,
                             cat=cat,
                             page=page,
                             search_type=search_type)

        try:
            result = _spider.get_torrents()
            return _spider.is_error, result, _spider.error_detail
        finally:
            del _spider

    @staticmethod
    async def __async_spider_search(indexer: dict[str, Any],
                                    search_word: Optional[str] = None,
                                    mtype: Optional[MediaType] = None,
                                    cat: Optional[str] = None,
                                    page: Optional[int] = 0,
                                    search_type: Optional[str] = "torrents") -> Tuple[bool, List[dict[str, Any]], Optional[str]]:
        """
        异步根据关键字搜索单个站点。

        :param indexer: 站点配置
        :param search_word: 关键字
        :param cat: 分类
        :param page: 页码
        :param mtype: 媒体类型
        :return: 是否发生错误, 种子列表, 失败原因
        """
        _spider = SiteSpider(indexer=indexer,
                             keyword=search_word,
                             mtype=mtype,
                             cat=cat,
                             page=page,
                             search_type=search_type)

        try:
            result = await _spider.async_get_torrents()
            return _spider.is_error, result, _spider.error_detail
        finally:
            del _spider

    def refresh_torrents(self, site: dict[str, Any],
                         keyword: Optional[str] = None,
                         cat: Optional[str] = None,
                         page: Optional[int] = 0,
                         mtype: Optional[MediaType] = None) -> Optional[List[TorrentInfo]]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理。

        :param site: 站点
        :param keyword: 关键字
        :param cat: 分类
        :param page: 页码
        :param mtype: 媒体类型
        :return: 种子资源列表
        """
        return self.search_torrents(
            site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
        )

    async def async_refresh_torrents(self, site: dict[str, Any],
                                     keyword: Optional[str] = None,
                                     cat: Optional[str] = None,
                                     page: Optional[int] = 0,
                                     mtype: Optional[MediaType] = None) -> Optional[List[TorrentInfo]]:
        """
        异步获取站点最新一页的种子，多个站点需要多线程处理。

        :param site: 站点
        :param keyword: 关键字
        :param cat: 分类
        :param page: 页码
        :param mtype: 媒体类型
        :return: 种子资源列表
        """
        return await self.async_search_torrents(
            site=site, keyword=keyword, cat=cat, page=page, mtype=mtype
        )

    def refresh_userdata(self, site: dict[str, Any]) -> Optional[SiteUserData]:
        """
        刷新站点的用户数据。

        :param site: 站点
        :return: 用户数据
        """

        def __get_site_obj(schema_value: Optional[str] = None) -> Optional[SiteParserBase]:
            """
            获取站点解析器。

            :param schema_value: 指定 schema, 默认取站点声明的 schema
            """
            schema_value = schema_value or site.get("schema")
            for site_schema in self._site_schemas:
                if site_schema.schema and site_schema.schema.value == schema_value:
                    site_url = cast(str, site.get("url"))
                    userdata_path = str(site.get("userdata_path") or "").strip()
                    if userdata_path and site_url:
                        if userdata_path.startswith(("http://", "https://")):
                            site_url = userdata_path
                        else:
                            site_url = urljoin(
                                f"{str(site_url).rstrip('/')}/",
                                userdata_path.lstrip("/"),
                            )
                    return site_schema(
                        site_name=site.get("name"),
                        url=site_url,
                        site_cookie=site.get("cookie"),
                        apikey=site.get("apikey"),
                        token=site.get("token"),
                        ua=site.get("ua") or get_runtime_setting("USER_AGENT"),
                        proxy=site.get("proxy"),
                        api_url=site.get("api_url"))
            return None

        # 按站点声明的 schema 获取解析器
        site_obj = __get_site_obj()
        if not site_obj:
            if not site.get("public"):
                logger.warn(f"站点  {site.get('name')} 未找到站点解析器，schema：{site.get('schema')}")
            return None

        try:
            logger.info(f"站点 {site.get('name')} 开始以 {site.get('schema')} 模型解析数据...")
            site_obj.parse()
            logger.debug(f"站点 {site.get('name')} 数据解析完成")
            # 站点声明的 schema 解析失败(userid 为空)时, 自动尝试其他解析器,
            # 兼容资源文件 schema 标注错误/变种站点的场景
            if not site_obj.userid and not site.get("public"):
                tried = {site.get("schema")}
                for site_schema in self._site_schemas:
                    if (
                        not site_schema.schema
                        or site_schema.schema.value in tried
                        or site_schema.schema.value not in _COMMON_SITE_USERDATA_FALLBACK_SCHEMAS
                    ):
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
            if not site_obj.userid and not site.get("public"):
                site_obj.err_msg = site_obj.err_msg or "未获取到站点用户信息，请检查 Cookie 是否有效"
            # 解析器保留站点原值，由模型统一执行既有字段校验与列表投影。
            return SiteUserData.model_validate({
                "domain": site_rules.extract_domain(site.get("url")),
                "userid": site_obj.userid,
                "username": site_obj.username,
                "user_level": site_obj.user_level,
                "join_at": site_obj.join_at,
                "upload": site_obj.upload,
                "download": site_obj.download,
                "ratio": site_obj.ratio,
                "bonus": site_obj.bonus,
                "seeding": site_obj.seeding,
                "seeding_size": site_obj.seeding_size,
                "seeding_info": site_obj.seeding_info.copy() if site_obj.seeding_info else [],
                "leeching": site_obj.leeching,
                "leeching_size": site_obj.leeching_size,
                "message_unread": site_obj.message_unread,
                "message_unread_contents": site_obj.message_unread_contents.copy()
                if site_obj.message_unread_contents else [],
                "updated_day": datetime.now().strftime('%Y-%m-%d'),
                "err_msg": site_obj.err_msg,
            })
        finally:
            site_obj.clear()
