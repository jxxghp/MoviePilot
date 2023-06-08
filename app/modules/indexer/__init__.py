import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Optional, Tuple, Union

from ruamel.yaml import CommentedMap

from app.core import MediaInfo, TorrentInfo, settings
from app.log import logger
from app.modules import _ModuleBase
from app.modules.indexer.spider import TorrentSpider
from app.modules.indexer.tnode import TNodeSpider
from app.modules.indexer.torrentleech import TorrentLeech
from app.utils.string import StringUtils
from app.utils.types import MediaType


class IndexerModule(_ModuleBase):
    """
    索引模块
    """

    def init_module(self) -> None:
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "INDEXER", "builtin"

    def search_torrents(self, mediainfo: Optional[MediaInfo], sites: List[CommentedMap],
                        keyword: str = None) -> Optional[List[TorrentInfo]]:
        """
        搜索站点，多个站点需要多线程处理
        :param mediainfo:  识别的媒体信息
        :param sites:  站点列表
        :param keyword:  搜索关键词，如有按关键词搜索，否则按媒体信息名称搜索
        :reutrn: 资源列表
        """
        # 开始计时
        start_time = datetime.now()
        # 多线程
        executor = ThreadPoolExecutor(max_workers=len(sites))
        all_task = []
        for site in sites:
            task = executor.submit(self.__search, mediainfo=mediainfo,
                                   site=site, keyword=keyword)
            all_task.append(task)
        results = []
        finish_count = 0
        for future in as_completed(all_task):
            finish_count += 1
            result = future.result()
            if result:
                results += result
        # 计算耗时
        end_time = datetime.now()
        logger.info(f"站点搜索完成，有效资源数：{len(results)}，总耗时 {(end_time - start_time).seconds} 秒")
        # 返回
        return results

    def __search(self, mediainfo: MediaInfo, site: CommentedMap,
                 keyword: str = None) -> Optional[List[TorrentInfo]]:
        """
        搜索一个站点
        :param mediainfo:  识别的媒体信息
        :param site:  站点
        :param keyword:  搜索关键词，如有按关键词搜索，否则按媒体信息名称搜索
        :return: 资源列表
        """
        # 确认搜索的名字
        if keyword:
            search_word = keyword
        elif mediainfo:
            search_word = mediainfo.title
        else:
            search_word = None

        if search_word \
                and site.get('language') == "en" \
                and StringUtils.is_chinese(search_word):
            # 不支持中文
            return []

        # 开始索引
        result_array = []
        # 开始计时
        start_time = datetime.now()
        try:
            if site.get('parser') == "TNodeSpider":
                error_flag, result_array = TNodeSpider(site).search(keyword=search_word)
            elif site.get('parser') == "TorrentLeech":
                error_flag, result_array = TorrentLeech(site).search(keyword=search_word)
            else:
                error_flag, result_array = self.__spider_search(
                    keyword=search_word,
                    indexer=site,
                    mtype=mediainfo.type
                )
        except Exception as err:
            logger.error(f"{site.get('name')} 搜索出错：{err}")

        # 索引花费的时间
        seconds = round((datetime.now() - start_time).seconds, 1)

        # 返回结果
        if len(result_array) == 0:
            logger.warn(f"{site.get('name')} 未搜索到数据，耗时 {seconds} 秒")
            return []
        else:
            logger.warn(f"{site.get('name')} 搜索完成，耗时 {seconds} 秒，返回数据：{len(result_array)}")
            # 合并站点信息，以TorrentInfo返回
            return [TorrentInfo(site=site.get("id"),
                                site_name=site.get("name"),
                                site_cookie=site.get("cookie"),
                                site_ua=site.get("ua"),
                                site_proxy=site.get("proxy"),
                                site_order=site.get("order"),
                                **result) for result in result_array]

    @staticmethod
    def __spider_search(indexer: CommentedMap,
                        keyword: str = None,
                        mtype: MediaType = None,
                        page: int = None, timeout: int = 30) -> (bool, List[dict]):
        """
        根据关键字搜索单个站点
        :param: indexer: 站点配置
        :param: keyword: 关键字
        :param: page: 页码
        :param: mtype: 媒体类型
        :param: timeout: 超时时间
        :return: 是否发生错误, 种子列表
        """
        _spider = TorrentSpider()
        _spider.setparam(indexer=indexer,
                         mtype=mtype,
                         keyword=keyword,
                         page=page)
        _spider.start()
        # 循环判断是否获取到数据
        sleep_count = 0
        while not _spider.is_complete:
            sleep_count += 1
            time.sleep(1)
            if sleep_count > timeout:
                break
        # 是否发生错误
        result_flag = _spider.is_error
        # 种子列表
        result_array = _spider.torrents_info_array.copy()
        # 重置状态
        _spider.torrents_info_array.clear()
        return result_flag, result_array

    def refresh_torrents(self, sites: List[CommentedMap]) -> Optional[List[TorrentInfo]]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param sites:  站点列表
        :reutrn: 种子资源列表
        """
        return self.search_torrents(mediainfo=None, sites=sites, keyword=None)
