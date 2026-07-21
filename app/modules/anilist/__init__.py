from typing import List, Optional, Tuple, Union

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.helper.scraper import MediaScraperHelper
from app.log import logger
from app.modules import _ModuleBase
from app.modules.anilist.anilist import AniListApi
from app.schemas.types import MediaRecognizeType, MediaType, ModuleType


class AniListModule(_ModuleBase):
    """
    AniList 动画媒体识别与刮削模块
    """

    CONFIG_WATCH = {"PROXY_HOST"}

    anilist_api: AniListApi = None
    scraper: MediaScraperHelper = None

    def init_module(self) -> None:
        """初始化 AniList 客户端与通用刮削器"""
        self.anilist_api = AniListApi()
        self.scraper = MediaScraperHelper()

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """AniList 模块无需独立开关"""
        return None

    def stop(self) -> None:
        """关闭 AniList 模块"""
        return None

    def test(self) -> Tuple[bool, str]:
        """测试 AniList GraphQL API 连通性"""
        result = self.anilist_api.search("Cowboy Bebop", count=1)
        return (True, "") if result else (False, "AniList网络连接失败")

    @staticmethod
    def get_name() -> str:
        """获取模块名称"""
        return "AniList"

    @staticmethod
    def get_type() -> ModuleType:
        """获取模块类型"""
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """获取模块子类型"""
        return MediaRecognizeType.AniList

    @staticmethod
    def get_priority() -> int:
        """获取模块优先级"""
        return 4

    @staticmethod
    def _source_enabled(source: Optional[str]) -> bool:
        """
        判断本次识别是否指定 AniList。

        :param source: 请求级识别数据源
        :return: 是否启用 AniList 识别
        """
        return (source or settings.RECOGNIZE_SOURCE) == "anilist"

    @staticmethod
    def _media_type(info: dict) -> MediaType:
        """
        将 AniList 发布格式转换为系统媒体类型。

        :param info: AniList 媒体信息
        :return: 系统媒体类型
        """
        return MediaType.MOVIE if info.get("format") == "MOVIE" else MediaType.TV

    @classmethod
    def _matches_meta(cls, meta: MetaBase, info: dict) -> bool:
        """
        判断 AniList 候选项是否符合标题解析出的类型与年份。

        :param meta: 标题解析元数据
        :param info: AniList 候选项
        :return: 是否符合筛选条件
        """
        if meta.type in {MediaType.MOVIE, MediaType.TV} and cls._media_type(info) != meta.type:
            return False
        year = info.get("startDate", {}).get("year") or info.get("seasonYear")
        return not meta.year or not year or str(year) == str(meta.year)

    @staticmethod
    def _enrich_people(info: dict) -> dict:
        """
        将 AniList 人物连接转换为统一媒体信息所需的演职员结构。

        :param info: AniList 媒体详情
        :return: 补充演员和导演后的媒体详情
        """
        enriched = dict(info)
        actors = []
        for edge in info.get("characters", {}).get("edges") or []:
            character = edge.get("node") or {}
            voice_actors = edge.get("voiceActors") or []
            actor = voice_actors[0] if voice_actors else {}
            actor_name = actor.get("name", {}).get("full")
            if not actor_name:
                continue
            actors.append(
                {
                    "name": actor_name,
                    "character": character.get("name", {}).get("full")
                    or character.get("name", {}).get("native"),
                    "avatar": {"large": actor.get("image", {}).get("large")},
                    "url": actor.get("siteUrl"),
                }
            )
        enriched["actors"] = actors

        directors = []
        for edge in info.get("staff", {}).get("edges") or []:
            role = edge.get("role") or ""
            if "Director" not in role:
                continue
            staff = edge.get("node") or {}
            directors.append(
                {
                    "name": staff.get("name", {}).get("full"),
                    "job": role,
                    "avatar": {"large": staff.get("image", {}).get("large")},
                    "url": staff.get("siteUrl"),
                }
            )
        enriched["directors"] = directors
        return enriched

    def recognize_media(
        self,
        meta: MetaBase = None,
        anilistid: Optional[int] = None,
        source: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        按 AniList ID 或标题识别动画媒体信息。

        :param meta: 标题解析元数据
        :param anilistid: AniList 媒体 ID
        :param source: 请求级识别数据源
        :return: 统一媒体信息
        """
        if not anilistid and (not meta or not self._source_enabled(source)):
            return None
        info = self.anilist_api.detail(anilistid) if anilistid else self._match_by_meta(meta)
        if not info:
            return None
        mediainfo = MediaInfo(anilist_info=self._enrich_people(info))
        if meta and meta.begin_season is not None:
            mediainfo.season = meta.begin_season
        logger.info(
            f"{anilistid or meta.name} AniList识别结果：{mediainfo.type.value} "
            f"{mediainfo.title_year}"
        )
        return mediainfo

    async def async_recognize_media(
        self,
        meta: MetaBase = None,
        anilistid: Optional[int] = None,
        source: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        异步按 AniList ID 或标题识别动画媒体信息。

        :param meta: 标题解析元数据
        :param anilistid: AniList 媒体 ID
        :param source: 请求级识别数据源
        :return: 统一媒体信息
        """
        if not anilistid and (not meta or not self._source_enabled(source)):
            return None
        info = (
            await self.anilist_api.async_detail(anilistid)
            if anilistid
            else await self._async_match_by_meta(meta)
        )
        if not info:
            return None
        mediainfo = MediaInfo(anilist_info=self._enrich_people(info))
        if meta and meta.begin_season is not None:
            mediainfo.season = meta.begin_season
        logger.info(
            f"{anilistid or meta.name} AniList识别结果：{mediainfo.type.value} "
            f"{mediainfo.title_year}"
        )
        return mediainfo

    def _match_by_meta(self, meta: MetaBase) -> Optional[dict]:
        """
        同步搜索并筛选最符合标题解析结果的 AniList 条目。

        :param meta: 标题解析元数据
        :return: AniList 媒体详情
        """
        for info in self.anilist_api.search(meta.name):
            if self._matches_meta(meta, info):
                return info
        return None

    async def _async_match_by_meta(self, meta: MetaBase) -> Optional[dict]:
        """
        异步搜索并筛选最符合标题解析结果的 AniList 条目。

        :param meta: 标题解析元数据
        :return: AniList 媒体详情
        """
        for info in await self.anilist_api.async_search(meta.name):
            if self._matches_meta(meta, info):
                return info
        return None

    def search_medias(
        self, meta: MetaBase, source: Optional[str] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索 AniList 动画媒体信息。

        :param meta: 标题解析元数据
        :param source: 请求级搜索数据源
        :return: 统一媒体信息列表
        """
        if source and source != "anilist":
            return None
        if not source and settings.SEARCH_SOURCE and "anilist" not in settings.SEARCH_SOURCE:
            return None
        if not meta or not meta.name:
            return []
        return [
            MediaInfo(anilist_info=self._enrich_people(info))
            for info in self.anilist_api.search(meta.name)
            if self._matches_meta(meta, info)
        ]

    async def async_search_medias(
        self, meta: MetaBase, source: Optional[str] = None
    ) -> Optional[List[MediaInfo]]:
        """
        异步搜索 AniList 动画媒体信息。

        :param meta: 标题解析元数据
        :param source: 请求级搜索数据源
        :return: 统一媒体信息列表
        """
        if source and source != "anilist":
            return None
        if not source and settings.SEARCH_SOURCE and "anilist" not in settings.SEARCH_SOURCE:
            return None
        if not meta or not meta.name:
            return []
        return [
            MediaInfo(anilist_info=self._enrich_people(info))
            for info in await self.anilist_api.async_search(meta.name)
            if self._matches_meta(meta, info)
        ]

    def metadata_nfo(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        **kwargs,
    ) -> Optional[str]:
        """
        生成 AniList 来源的 NFO 内容。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: NFO XML 文本
        """
        scrape_source = mediainfo.scrape_source or settings.SCRAP_SOURCE
        if scrape_source != "anilist":
            return None
        return self.scraper.get_metadata_nfo(mediainfo, season=season, episode=episode)

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取 AniList 来源的刮削图片清单。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: 图片文件名与下载地址映射
        """
        scrape_source = mediainfo.scrape_source or settings.SCRAP_SOURCE
        if scrape_source != "anilist":
            return None
        return self.scraper.get_metadata_img(mediainfo, season=season, episode=episode)

    def clear_cache(self) -> None:
        """清理 AniList 接口缓存"""
        self.anilist_api.clear_cache()
