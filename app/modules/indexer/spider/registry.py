"""
索引器 Spider 注册表：站点配置中的 parser 标识与专用 Spider 类的映射。
"""
import inspect
from functools import lru_cache
from typing import Any, Dict, Optional, Type

from app.modules.indexer.spider import SiteSpider
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.modules.indexer.spider.rousi import RousiSpider
from app.modules.indexer.spider.sunnypt import SunnyPTSpider
from app.modules.indexer.spider.tnode import TNodeSpider
from app.modules.indexer.spider.torrentleech import TorrentLeech
from app.modules.indexer.spider.yema import YemaSpider
from app.schemas.types import MediaType

SPIDER_REGISTRY: Dict[str, Type[Any]] = {
    "TNodeSpider": TNodeSpider,
    "TorrentLeech": TorrentLeech,
    "mTorrent": MTorrentSpider,
    "Yema": YemaSpider,
    "Haidan": HaiDanSpider,
    "HDDolby": HddolbySpider,
    "RousiPro": RousiSpider,
    "SunnyPT": SunnyPTSpider,
}


def resolve_spider_class(parser: Optional[str]) -> Type[Any]:
    """
    根据站点配置中的 parser 标识解析对应的 Spider 类

    :param parser: 站点配置的 parser 标识
    :return: 匹配的专用 Spider 类；未匹配到已注册标识时返回通用模板爬虫 SiteSpider
    """
    return SPIDER_REGISTRY.get(parser, SiteSpider)


@lru_cache(maxsize=None)
def _search_param_names(spider_cls: Type[Any]) -> frozenset:
    """
    获取 Spider 类 search 方法声明的形参名集合

    :param spider_cls: 专用 Spider 类
    :return: search 方法可接受的形参名集合
    """
    return frozenset(inspect.signature(spider_cls.search).parameters)


def build_search_kwargs(spider_cls: Type[Any],
                        keyword: Optional[str] = None,
                        mtype: MediaType = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0) -> Dict[str, Any]:
    """
    按 Spider 类 search/async_search 方法实际声明的形参过滤搜索参数

    各注册的 Spider 类 search 与 async_search 方法形参名一致，
    因此同一份过滤结果同时适用于同步与异步调用。

    :param spider_cls: 专用 Spider 类
    :param keyword: 搜索关键词
    :param mtype: 媒体类型
    :param cat: 分类
    :param page: 页码
    :return: 该 Spider 类 search 方法能够接受的参数子集
    """
    candidates = {"keyword": keyword, "mtype": mtype, "cat": cat, "page": page}
    accepted = _search_param_names(spider_cls)
    return {name: value for name, value in candidates.items() if name in accepted}
