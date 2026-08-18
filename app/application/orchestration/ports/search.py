"""搜索与资源发现域的能力端口客户端。"""

from __future__ import annotations

from typing import List, Optional

from app.application.orchestration.ports.dispatch import CapabilityPorts
from app.domain.context import MediaInfo, SubtitleInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.context import MediaPerson
from app.schemas.filter import TorrentVerdict
from app.schemas.types import MediaSourceSelection, MediaType


class SearchPorts(CapabilityPorts):
    """媒体、人物、合集搜索与站点资源检索、过滤的能力端口。"""

    def search_medias(
            self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        return [
            media
            for medias in self._dispatch.multicast(
                "search_medias", meta=meta, media_source=media_source
            )
            for media in medias
        ]

    async def async_search_medias(
            self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息列表
        """
        return [
            media
            for medias in await self._dispatch.async_multicast(
                "async_search_medias", meta=meta, media_source=media_source
            )
            for media in medias
        ]

    def search_persons(
            self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息
        :param name:  人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        return [
            person
            for persons in self._dispatch.multicast(
                "search_persons", name=name, media_source=media_source
            )
            for person in persons
        ]

    async def async_search_persons(
            self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaPerson]]:
        """
        搜索人物信息（异步版本）
        :param name:  人物名称
        :param media_source: 请求级搜索数据源
        :return: 人物信息列表
        """
        return [
            person
            for persons in await self._dispatch.async_multicast(
                "async_search_persons", name=name, media_source=media_source
            )
            for person in persons
        ]

    def search_collections(
            self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息
        :param name:  集合名称
        :param media_source: 请求级搜索数据源
        :return: 合集信息列表
        """
        return [
            collection
            for collections in self._dispatch.multicast(
                "search_collections", name=name, media_source=media_source
            )
            for collection in collections
        ]

    async def async_search_collections(
            self, name: str, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索集合信息（异步版本）
        :param name:  集合名称
        :param media_source: 请求级搜索数据源
        :return: 合集信息列表
        """
        return [
            collection
            for collections in await self._dispatch.async_multicast(
                "async_search_collections", name=name, media_source=media_source
            )
            for collection in collections
        ]

    def get_search_page_size(
            self,
            site: dict,
            keyword: Optional[str] = None,
    ) -> Optional[int]:
        """
        获取站点搜索单页容量；返回 None 表示当前搜索入口不支持可靠翻页。
        """
        return self._dispatch.unicast(
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
        return self._dispatch.unicast(
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
        return self._dispatch.unicast(
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
        return await self._dispatch.async_unicast(
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
        return await self._dispatch.async_unicast(
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
        return self._dispatch.unicast(
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
        return await self._dispatch.async_unicast(
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
        return self._dispatch.unicast(
            "filter_torrents",
            rule_groups=rule_groups,
            torrent_list=torrent_list,
            mediainfo=mediainfo,
        )

    def analyze_torrent_candidates(
            self,
            rule_groups: List[str],
            torrent_list: List[TorrentInfo],
            mediainfo: MediaInfo = None,
    ) -> List[List[TorrentVerdict]]:
        """
        收集全部分析器对候选种子的判定
        :param rule_groups:  过滤规则组名称列表
        :param torrent_list:  资源列表
        :param mediainfo:  识别的媒体信息
        :return: 每个分析器一份、按下标与资源列表对应的判定列表；不参与的分析器不计入
        """
        return self._dispatch.multicast(
            "analyze_torrent_candidates",
            rule_groups=rule_groups,
            torrent_list=torrent_list,
            mediainfo=mediainfo,
        )
