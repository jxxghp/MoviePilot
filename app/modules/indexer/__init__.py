from datetime import datetime
from typing import List, Optional, Tuple, Union

from app.core.config import settings
from app.core.context import TorrentInfo
from app.db.site_oper import SiteOper
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper, SiteSpider
from app.log import logger
from app.modules import _ModuleBase
from app.modules.indexer.parser import SiteParserBase
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.modules.indexer.spider.tnode import TNodeSpider
from app.modules.indexer.spider.torrentleech import TorrentLeech
from app.modules.indexer.spider.yema import YemaSpider
from app.schemas import SiteUserData
from app.schemas.types import MediaType, ModuleType, OtherModulesType
from app.utils.string import StringUtils


class IndexerModule(_ModuleBase):
    """
    索引模块
    """

    _site_schemas = []

    def init_module(self) -> None:
        # 加载模块
        self._site_schemas = ModuleHelper.load(
            'app.modules.indexer.parser',
            filter_func=lambda _, obj: hasattr(obj, 'schema') and getattr(obj, 'schema') is not None)
        pass

    @staticmethod
    def get_name() -> str:
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
        pass

    def search_torrents(self, site: dict,
                        keywords: List[str] = None,
                        mtype: MediaType = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        搜索一个站点
        :param site:  站点
        :param keywords:  搜索关键词列表
        :param mtype:  媒体类型
        :param cat:  分类
        :param page:  页码
        :return: 资源列表
        """

        def __remove_duplicate(_torrents: List[TorrentInfo]) -> List[TorrentInfo]:
            """
            去除重复的种子
            :param _torrents: 种子列表
            :return: 去重后的种子列表
            """
            if not settings.SEARCH_MULTIPLE_NAME:
                return _torrents
            # 通过encosure去重
            return list({f"{t.title}_{t.description}": t for t in _torrents}.values())

        # 确认搜索的名字
        if not keywords:
            # 浏览种子页
            keywords = ['']

        # 开始索引
        result_array = []

        # 开始计时
        start_time = datetime.now()

        # 搜索多个关键字
        error_flag = False
        for search_word in keywords:
            # 可能为关键字或ttxxxx
            if search_word \
                    and site.get('language') == "en" \
                    and StringUtils.is_chinese(search_word):
                # 不支持中文
                logger.warn(f"{site.get('name')} 不支持中文搜索")
                continue

            # 站点流控
            state, msg = SitesHelper().check(StringUtils.get_url_domain(site.get("domain")))
            if state:
                logger.warn(msg)
                continue

            # 去除搜索关键字中的特殊字符
            if search_word:
                search_word = StringUtils.clear(search_word, replace_word=" ", allow_space=True)

            try:
                if site.get('parser') == "TNodeSpider":
                    error_flag, result = TNodeSpider(site).search(
                        keyword=search_word,
                        page=page
                    )
                elif site.get('parser') == "TorrentLeech":
                    error_flag, result = TorrentLeech(site).search(
                        keyword=search_word,
                        page=page
                    )
                elif site.get('parser') == "mTorrent":
                    error_flag, result = MTorrentSpider(site).search(
                        keyword=search_word,
                        mtype=mtype,
                        page=page
                    )
                elif site.get('parser') == "Yema":
                    error_flag, result = YemaSpider(site).search(
                        keyword=search_word,
                        mtype=mtype,
                        page=page
                    )
                elif site.get('parser') == "Haidan":
                    error_flag, result = HaiDanSpider(site).search(
                        keyword=search_word,
                        mtype=mtype
                    )
                elif site.get('parser') == "HDDolby":
                    error_flag, result = HddolbySpider(site).search(
                        keyword=search_word,
                        mtype=mtype,
                        page=page
                    )
                else:
                    error_flag, result = self.__spider_search(
                        search_word=search_word,
                        indexer=site,
                        mtype=mtype,
                        cat=cat,
                        page=page
                    )
                if error_flag:
                    break
                if not result:
                    continue
                if settings.SEARCH_MULTIPLE_NAME:
                    # 合并多个结果
                    result_array.extend(result)
                else:
                    # 有结果就停止
                    result_array = result
                    break
            except Exception as err:
                logger.error(f"{site.get('name')} 搜索出错：{str(err)}")

        # 索引花费的时间
        seconds = (datetime.now() - start_time).seconds

        # 统计索引情况
        domain = StringUtils.get_url_domain(site.get("domain"))
        if error_flag:
            SiteOper().fail(domain)
        else:
            SiteOper().success(domain=domain, seconds=seconds)

        # 返回结果
        if not result_array or len(result_array) == 0:
            logger.warn(f"{site.get('name')} 未搜索到数据，耗时 {seconds} 秒")
            return []
        else:
            logger.info(f"{site.get('name')} 搜索完成，耗时 {seconds} 秒，返回数据：{len(result_array)}")
            # TorrentInfo
            torrents = [TorrentInfo(site=site.get("id"),
                                    site_name=site.get("name"),
                                    site_cookie=site.get("cookie"),
                                    site_ua=site.get("ua"),
                                    site_proxy=site.get("proxy"),
                                    site_order=site.get("pri"),
                                    site_downloader=site.get("downloader"),
                                    **result) for result in result_array]
            # 去重
            return __remove_duplicate(torrents)

    @staticmethod
    def __spider_search(indexer: dict,
                        search_word: Optional[str] = None,
                        mtype: MediaType = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
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
                             page=page)

        try:
            return _spider.is_error, _spider.get_torrents()
        finally:
            # 显式清理SiteSpider对象
            del _spider

    def refresh_torrents(self, site: dict,
                         keyword: Optional[str] = None, cat: Optional[str] = None, page: Optional[int] = 0) -> Optional[List[TorrentInfo]]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :param keyword:  关键字
        :param cat:  分类
        :param page:  页码
        :reutrn: 种子资源列表
        """
        return self.search_torrents(site=site, keywords=[keyword], cat=cat, page=page)

    def refresh_userdata(self, site: dict) -> Optional[SiteUserData]:
        """
        刷新站点的用户数据
        :param site:  站点
        :return: 用户数据
        """

        def __get_site_obj() -> Optional[SiteParserBase]:
            """
            获取站点解析器
            """
            for site_schema in self._site_schemas:
                if site_schema.schema.value == site.get("schema"):
                    return site_schema(
                        site_name=site.get("name"),
                        url=site.get("url"),
                        site_cookie=site.get("cookie"),
                        apikey=site.get("apikey"),
                        token=site.get("token"),
                        ua=site.get("ua"),
                        proxy=site.get("proxy"))
            return None

        site_obj = __get_site_obj()
        if not site_obj:
            if not site.get("public"):
                logger.warn(f"站点  {site.get('name')} 未找到站点解析器，schema：{site.get('schema')}")
            return None

        # 获取用户数据
        logger.info(f"站点 {site.get('name')} 开始以 {site.get('schema')} 模型解析数据...")
        site_obj.parse()
        logger.debug(f"站点 {site.get('name')} 数据解析完成")
        return SiteUserData(
            domain=StringUtils.get_url_domain(site.get("url")),
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
            seeding_info=site_obj.seeding_info or [],
            leeching=site_obj.leeching,
            leeching_size=site_obj.leeching_size,
            message_unread=site_obj.message_unread,
            message_unread_contents=site_obj.message_unread_contents or [],
            updated_day=datetime.now().strftime('%Y-%m-%d'),
            err_msg=site_obj.err_msg
        )
