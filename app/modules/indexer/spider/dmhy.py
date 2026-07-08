from typing import List, Optional, Tuple
from urllib.parse import urlencode

from fastapi.concurrency import run_in_threadpool

from app.core.config import settings
from app.helper.rss import RssHelper
from app.log import logger
from app.schemas.types import MediaType


class DMHYSpider:
    """
    动漫花园公开 RSS 搜索。

    动漫花园搜索结果可通过 RSS 返回结构化的标题、详情页、发布时间和
    magnet 链接，比 HTML 页面更适合作为 MoviePilot 索引入口。
    """

    _size = 40
    _rss_path = "topics/rss/rss.xml"
    _season_pack_sort_id = "31"
    _season_pack_label = "季度全集"

    def __init__(self, indexer: dict):
        self._indexerid = indexer.get("id")
        self._name = indexer.get("name") or "动漫花园"
        self._domain = (
            indexer.get("domain") or indexer.get("url") or "https://dmhy.anoneko.com/"
        ).rstrip("/") + "/"
        self._rss = indexer.get("rss")
        self._proxy = bool(indexer.get("proxy"))
        self._ua = indexer.get("ua") or settings.USER_AGENT
        self._timeout = int(indexer.get("timeout") or 20)

    @classmethod
    def get_search_page_size(cls, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取搜索接口单页容量。
        """
        return cls._size

    def __build_rss_url(
            self, keyword: Optional[str] = None, sort_id: Optional[str] = None
    ) -> str:
        """
        生成动漫花园 RSS 搜索地址。
        """
        base = self._rss or f"{self._domain}{self._rss_path}"
        params = {}
        if keyword:
            params["keyword"] = keyword
        if sort_id:
            params["sort_id"] = sort_id
        if not params:
            return base
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{urlencode(params)}"

    @staticmethod
    def __format_pubdate(value):
        """
        将 RSS 发布时间统一转换为字符串。
        """
        if not value:
            return None
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)

    @staticmethod
    def __normalize_size(value) -> int:
        """
        规范化 RSS 文件大小，忽略动漫花园 magnet 占位大小。
        """
        try:
            size = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return size if size > 1 else 0

    def __parse_items(
            self,
            items: list,
            mtype: MediaType = None,
            page: Optional[int] = 0,
            season_pack: bool = False,
    ) -> List[dict]:
        """
        解析 RSS 条目为 MoviePilot 种子字段。
        """
        torrents = []
        if not items:
            return torrents

        start = max(int(page or 0), 0) * self._size
        end = start + self._size
        category = mtype.value if isinstance(mtype, MediaType) else MediaType.UNKNOWN.value

        for item in items[start:end]:
            title = item.get("title")
            enclosure = item.get("enclosure")
            if not title or not enclosure:
                continue
            labels = [self._season_pack_label] if season_pack else []
            torrents.append({
                "title": title,
                "description": "",
                "enclosure": enclosure,
                "page_url": item.get("link"),
                "pubdate": self.__format_pubdate(item.get("pubdate")),
                "size": self.__normalize_size(item.get("size")),
                "seeders": 0,
                "peers": 0,
                "grabs": 0,
                "downloadvolumefactor": 1,
                "uploadvolumefactor": 1,
                "category": category,
                "labels": labels,
            })
        return torrents

    @staticmethod
    def __merge_torrents(*groups: List[dict]) -> List[dict]:
        """
        合并不同 RSS 分类返回的重复种子，并保留补充标签。
        """
        merged = []
        by_key = {}
        for group in groups:
            for item in group or []:
                key = item.get("enclosure") or item.get("page_url") or item.get("title")
                if not key:
                    continue
                existing = by_key.get(key)
                if not existing:
                    by_key[key] = item
                    merged.append(item)
                    continue
                labels = list(
                    dict.fromkeys((existing.get("labels") or []) + (item.get("labels") or []))
                )
                existing["labels"] = labels
        return merged

    def search(
            self,
            keyword: str,
            mtype: MediaType = None,
            page: Optional[int] = 0,
            **kwargs,
    ) -> Tuple[bool, List[dict]]:
        """
        搜索动漫花园 RSS 资源。
        """
        rss = RssHelper()
        url = self.__build_rss_url(keyword)
        items = rss.parse(
            url=url,
            proxy=self._proxy,
            timeout=self._timeout,
            ua=self._ua,
        )
        if items is False or items is None:
            logger.warn(f"{self._name} RSS 搜索失败：{url}")
            return True, []

        torrents = self.__parse_items(items, mtype=mtype, page=page)

        season_url = self.__build_rss_url(keyword, sort_id=self._season_pack_sort_id)
        season_items = rss.parse(
            url=season_url,
            proxy=self._proxy,
            timeout=self._timeout,
            ua=self._ua,
        )
        if season_items is False or season_items is None:
            logger.warn(f"{self._name} 季度全集 RSS 补充搜索失败：{season_url}")
            return False, torrents

        season_torrents = self.__parse_items(
            season_items,
            mtype=mtype,
            page=page,
            season_pack=True,
        )
        return False, self.__merge_torrents(torrents, season_torrents)

    async def async_search(
            self,
            keyword: str,
            mtype: MediaType = None,
            page: Optional[int] = 0,
            **kwargs,
    ) -> Tuple[bool, List[dict]]:
        """
        异步搜索动漫花园 RSS 资源。
        """
        return await run_in_threadpool(
            self.search,
            keyword=keyword,
            mtype=mtype,
            page=page,
        )
