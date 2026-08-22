from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Union

from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.runtime.config import settings
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.domain.scraper import MediaScraperHelper
from app.runtime.log import logger
from app.modules import _ModuleBase
from app.modules.bangumi.bangumi import BangumiApi
from app.schemas.types import (
    MediaSource,
    MediaSourceSelection,
    MediaType,
)
from app.adapters.network.http import RequestUtils
from app.domain.media import is_media_source_enabled
from app.schemas.media import normalize_media_source

# 榜单标识到本模块方法名的映射，discover_board 只接受在册标识，白名单校验先于 getattr 完成
_DISCOVER_BOARDS = {
    "calendar": "bangumi_calendar",
}


@dataclass(frozen=True, slots=True)
class BangumiConfigSnapshot:
    """Bangumi 模块一次配置 generation 使用的稳定网络快照。"""

    proxy: Any


class BangumiModule(_ModuleBase):
    """
    Bangumi媒体信息匹配
    """
    CONFIG_WATCH = {"PROXY_HOST"}

    bangumiapi: BangumiApi = None
    scraper: MediaScraperHelper = None
    _config: BangumiConfigSnapshot = BangumiConfigSnapshot(proxy=None)

    def init_module(self) -> None:
        """
        初始化Bangumi客户端
        """
        self._config = BangumiConfigSnapshot(proxy=settings.PROXY)
        self.bangumiapi = BangumiApi()
        self.scraper = MediaScraperHelper()

    def stop(self) -> None:
        """
        关闭Bangumi客户端
        """
        if self.bangumiapi:
            self.bangumiapi.close()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils(proxies=self._config.proxy).get_res("https://api.bgm.tv/")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接Bangumi，错误码：{ret.status_code}"
        return False, "Bangumi网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """Bangumi模块无需独立开关"""
        return None

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称
        """
        return "Bangumi"

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 3

    def recognize_media(
        self,
        meta: MetaBase = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta: 识别的元数据
        :param media_source: 请求级识别数据源
        :param media_id: 数据源原生ID
        :return: 识别的媒体信息，包括剧集信息
        """
        # Bangumi 只处理影视，不能在音乐模块未响应时接管音乐请求。
        if (
                kwargs.get("mtype") == MediaType.MUSIC
                or getattr(meta, "type", None) == MediaType.MUSIC
        ):
            return None
        if media_source and media_source != MediaSource.Bangumi:
            return None
        if media_id is not None and (
                media_source != MediaSource.Bangumi or not str(media_id).isdigit()
        ):
            return None
        bangumiid = int(media_id) if media_id is not None else None
        if not bangumiid and (
            not meta or (media_source or settings.RECOGNIZE_SOURCE) != MediaSource.Bangumi
        ):
            return None

        info = (
            self.bangumi_info(bangumiid=bangumiid)
            if bangumiid
            else self._match_by_meta(meta)
        )
        if info:
            info["actors"] = self.bangumiapi.credits(info.get("id"))
            mediainfo = MediaInfo(bangumi_info=info)
            if meta and meta.begin_season is not None:
                mediainfo.season = meta.begin_season
            logger.info(f"{bangumiid or meta.name} Bangumi识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year}")
            return mediainfo
        logger.info(f"{bangumiid or meta.name} 未匹配到Bangumi媒体信息")

        return None

    async def async_recognize_media(
        self,
        meta: MetaBase = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息（异步版本）
        :param meta: 识别的元数据
        :param media_source: 请求级识别数据源
        :param media_id: 数据源原生ID
        :return: 识别的媒体信息，包括剧集信息
        """
        # 与同步入口保持同一类型边界，音乐请求不得进入 Bangumi。
        if (
                kwargs.get("mtype") == MediaType.MUSIC
                or getattr(meta, "type", None) == MediaType.MUSIC
        ):
            return None
        if media_source and media_source != MediaSource.Bangumi:
            return None
        if media_id is not None and (
                media_source != MediaSource.Bangumi or not str(media_id).isdigit()
        ):
            return None
        bangumiid = int(media_id) if media_id is not None else None
        if not bangumiid and (
            not meta or (media_source or settings.RECOGNIZE_SOURCE) != MediaSource.Bangumi
        ):
            return None

        info = (
            await self.async_bangumi_info(bangumiid=bangumiid)
            if bangumiid
            else await self._async_match_by_meta(meta)
        )
        if info:
            info["actors"] = await self.bangumiapi.async_credits(info.get("id"))
            mediainfo = MediaInfo(bangumi_info=info)
            if meta and meta.begin_season is not None:
                mediainfo.season = meta.begin_season
            logger.info(f"{bangumiid or meta.name} Bangumi识别结果：{mediainfo.type.value} "
                        f"{mediainfo.title_year}")
            return mediainfo
        logger.info(f"{bangumiid or meta.name} 未匹配到Bangumi媒体信息")

        return None

    @staticmethod
    def _matches_meta(meta: MetaBase, info: dict) -> bool:
        """
        判断Bangumi候选项是否符合标题解析出的类型与年份。

        :param meta: 标题解析元数据
        :param info: Bangumi候选项详情
        :return: 是否符合筛选条件
        """
        if (
            meta.type in {MediaType.MOVIE, MediaType.TV}
            and MediaInfo.get_bangumi_media_type(info) != meta.type
        ):
            return False
        release_date = info.get("date") or info.get("air_date") or ""
        return not meta.year or not release_date or release_date[:4] == str(meta.year)

    def _match_by_meta(self, meta: MetaBase) -> Optional[dict]:
        """
        搜索并获取最符合标题解析结果的Bangumi详情。

        :param meta: 标题解析元数据
        :return: Bangumi媒体详情
        """
        for item in (self.bangumiapi.search(meta.name) or [])[:10]:
            info = self.bangumiapi.detail(item.get("id")) if item.get("id") else None
            if info and self._matches_meta(meta, info):
                return info
        return None

    async def _async_match_by_meta(self, meta: MetaBase) -> Optional[dict]:
        """
        异步搜索并获取最符合标题解析结果的Bangumi详情。

        :param meta: 标题解析元数据
        :return: Bangumi媒体详情
        """
        for item in (await self.bangumiapi.async_search(meta.name) or [])[:10]:
            info = await self.bangumiapi.async_detail(item.get("id")) if item.get("id") else None
            if info and self._matches_meta(meta, info):
                return info
        return None

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息
        """
        if not is_media_source_enabled(media_source, MediaSource.Bangumi):
            return None
        if not meta.name:
            return []
        infos = self.bangumiapi.search(meta.name)
        if infos:
            return [MediaInfo(bangumi_info=info) for info in infos
                    if meta.name.lower() in str(info.get("name")).lower()
                    or meta.name.lower() in str(info.get("name_cn")).lower()]
        return []

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param media_source: 请求级搜索数据源
        :return: 媒体信息
        """
        if not is_media_source_enabled(media_source, MediaSource.Bangumi):
            return None
        if not meta.name:
            return []
        infos = await self.bangumiapi.async_search(meta.name)
        if infos:
            return [MediaInfo(bangumi_info=info) for info in infos
                    if meta.name.lower() in str(info.get("name")).lower()
                    or meta.name.lower() in str(info.get("name_cn")).lower()]
        return []

    def bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        if not bangumiid:
            return None
        logger.info(f"开始获取Bangumi信息：{bangumiid} ...")
        return self.bangumiapi.detail(bangumiid)

    async def async_bangumi_info(self, bangumiid: int) -> Optional[dict]:
        """
        获取Bangumi信息（异步版本）
        :param bangumiid: BangumiID
        :return: Bangumi信息
        """
        if not bangumiid:
            return None
        logger.info(f"开始获取Bangumi信息：{bangumiid} ...")
        return await self.bangumiapi.async_detail(bangumiid)

    def metadata_nfo(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        **kwargs,
    ) -> Optional[str]:
        """
        生成Bangumi来源的NFO内容。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: NFO XML文本
        """
        scrape_source = mediainfo.scrape_source or settings.SCRAP_SOURCE
        if scrape_source != "bangumi":
            return None
        return self.scraper.get_metadata_nfo(mediainfo, season=season, episode=episode)

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取Bangumi来源的刮削图片清单。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: 图片文件名与下载地址映射
        """
        scrape_source = mediainfo.scrape_source or settings.SCRAP_SOURCE
        if scrape_source != "bangumi":
            return None
        return self.scraper.get_metadata_img(mediainfo, season=season, episode=episode)

    def bangumi_calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送
        """
        infos = self.bangumiapi.calendar()
        if infos:
            return [MediaInfo(bangumi_info=info) for info in infos]
        return []

    async def async_bangumi_calendar(self) -> Optional[List[MediaInfo]]:
        """
        获取Bangumi每日放送（异步版本）
        """
        infos = await self.bangumiapi.async_calendar()
        if infos:
            return [MediaInfo(bangumi_info=info) for info in infos]
        return []

    def bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        根据TMDBID查询电影演职员表
        :param bangumiid:  BangumiID
        """
        persons = self.bangumiapi.credits(bangumiid)
        if persons:
            return [_SchemaMediaPerson(source='bangumi', **person) for person in persons]
        return []

    async def async_bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        根据TMDBID查询电影演职员表（异步版本）
        :param bangumiid:  BangumiID
        """
        persons = await self.bangumiapi.async_credits(bangumiid)
        if persons:
            return [_SchemaMediaPerson(source='bangumi', **person) for person in persons]
        return []

    def bangumi_recommend(self, bangumiid: int) -> List[MediaInfo]:
        """
        根据BangumiID查询推荐电影
        :param bangumiid:  BangumiID
        """
        subjects = self.bangumiapi.subjects(bangumiid)
        if subjects:
            return [MediaInfo(bangumi_info=subject) for subject in subjects]
        return []

    async def async_bangumi_recommend(self, bangumiid: int) -> List[MediaInfo]:
        """
        根据BangumiID查询推荐电影（异步版本）
        :param bangumiid:  BangumiID
        """
        subjects = await self.bangumiapi.async_subjects(bangumiid)
        if subjects:
            return [MediaInfo(bangumi_info=subject) for subject in subjects]
        return []

    def bangumi_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        获取人物详细信息
        :param person_id:  豆瓣人物ID
        """
        personinfo = self.bangumiapi.person_detail(person_id)
        if personinfo:
            return self._build_person_detail(personinfo)
        return None

    async def async_bangumi_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        获取人物详细信息（异步版本）
        :param person_id:  豆瓣人物ID
        """
        personinfo = await self.bangumiapi.async_person_detail(person_id)
        if personinfo:
            return self._build_person_detail(personinfo)
        return None

    @classmethod
    def _build_person_detail(cls, personinfo: dict) -> _SchemaMediaPerson:
        """
        构造Bangumi人物详情信息。
        :param personinfo: Bangumi人物详情接口返回数据
        :return: 媒体人物信息
        """
        return _SchemaMediaPerson(source='bangumi', **{
            "id": personinfo.get("id"),
            "name": personinfo.get("name"),
            "images": personinfo.get("images"),
            "biography": personinfo.get("summary"),
            "birthday": cls._normalize_optional_string(personinfo.get("birth_day")),
            "gender": personinfo.get("gender")
        })

    @staticmethod
    def _normalize_optional_string(value: object) -> Optional[str]:
        """
        规范化Bangumi接口中可能返回非字符串的可选文本字段。
        :param value: 原始字段值
        :return: 字符串字段值或None
        """
        if value is None:
            return None
        return str(value)

    def bangumi_person_credits(self, person_id: int) -> List[MediaInfo]:
        """
        根据TMDBID查询人物参演作品
        :param person_id:  人物ID
        """
        credits_info = self.bangumiapi.person_credits(person_id=person_id)
        if credits_info:
            return [MediaInfo(bangumi_info=credit) for credit in credits_info]
        return []

    async def async_bangumi_person_credits(self, person_id: int) -> List[MediaInfo]:
        """
        根据TMDBID查询人物参演作品（异步版本）
        :param person_id:  人物ID
        """
        credits_info = await self.bangumiapi.async_person_credits(person_id=person_id)
        if credits_info:
            return [MediaInfo(bangumi_info=credit) for credit in credits_info]
        return []

    def bangumi_discover(self, **kwargs) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧
        """
        infos = self.bangumiapi.discover(**kwargs)
        if infos:
            return [MediaInfo(bangumi_info=info) for info in infos]
        return []

    async def async_bangumi_discover(self, **kwargs) -> Optional[List[MediaInfo]]:
        """
        发现Bangumi番剧（异步版本）
        """
        infos = await self.bangumiapi.async_discover(**kwargs)
        if infos:
            return [MediaInfo(bangumi_info=info) for info in infos]
        return []

    def clear_cache(self) -> None:
        """
        清除缓存
        """
        logger.info(f"开始清除{self.get_name()}缓存 ...")
        self.bangumiapi.clear_cache()
        logger.info(f"{self.get_name()}缓存清除完成")

    def person_detail(self, source: Optional[MediaSource] = None,
                       person_id: int = None,
                       **kwargs) -> Optional[_SchemaMediaPerson]:
        """
        查询指定来源的人物详情
        :param source: 媒体来源，非Bangumi来源返回 None
        :param person_id: 人物ID
        :return: 人物详情
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        return self.bangumi_person_detail(person_id=person_id)

    async def async_person_detail(self, source: Optional[MediaSource] = None,
                                   person_id: int = None,
                                   **kwargs) -> Optional[_SchemaMediaPerson]:
        """
        查询指定来源的人物详情（异步版本）
        :param source: 媒体来源，非Bangumi来源返回 None
        :param person_id: 人物ID
        :return: 人物详情
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        return await self.async_bangumi_person_detail(person_id=person_id)

    def person_credits(self, source: Optional[MediaSource] = None,
                        person_id: int = None,
                        page: int = 1,
                        count: Optional[int] = None,
                        **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的人物参演作品
        :param source: 媒体来源，非Bangumi来源返回 None
        :param person_id: 人物ID
        :param page: 本源不支持
        :param count: 本源不支持
        :return: 参演作品列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        return self.bangumi_person_credits(person_id=person_id)

    async def async_person_credits(self, source: Optional[MediaSource] = None,
                                    person_id: int = None,
                                    page: int = 1,
                                    count: Optional[int] = None,
                                    **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的人物参演作品（异步版本）
        :param source: 媒体来源，非Bangumi来源返回 None
        :param person_id: 人物ID
        :param page: 本源不支持
        :param count: 本源不支持
        :return: 参演作品列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        return await self.async_bangumi_person_credits(person_id=person_id)

    def media_credits(self, source: Optional[MediaSource] = None,
                       media_id: Any = None,
                       mtype: Optional[MediaType] = None,
                       page: int = 1,
                       count: Optional[int] = None,
                       **kwargs) -> Optional[List[_SchemaMediaPerson]]:
        """
        查询指定来源的媒体演职员表
        :param source: 媒体来源，非Bangumi来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持，单一接口不分电影剧集
        :param page: 本源不支持
        :param count: 本源不支持
        :return: 演职员列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        if media_id is None:
            return None
        try:
            bangumiid = int(media_id)
        except (TypeError, ValueError):
            return None
        return self.bangumi_credits(bangumiid=bangumiid)

    async def async_media_credits(self, source: Optional[MediaSource] = None,
                                   media_id: Any = None,
                                   mtype: Optional[MediaType] = None,
                                   page: int = 1,
                                   count: Optional[int] = None,
                                   **kwargs) -> Optional[List[_SchemaMediaPerson]]:
        """
        查询指定来源的媒体演职员表（异步版本）
        :param source: 媒体来源，非Bangumi来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持，单一接口不分电影剧集
        :param page: 本源不支持
        :param count: 本源不支持
        :return: 演职员列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        if media_id is None:
            return None
        try:
            bangumiid = int(media_id)
        except (TypeError, ValueError):
            return None
        return await self.async_bangumi_credits(bangumiid=bangumiid)

    def media_recommend(self, source: Optional[MediaSource] = None,
                         media_id: Any = None,
                         mtype: Optional[MediaType] = None,
                         page: int = 1,
                         count: Optional[int] = None,
                         **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的相关推荐媒体
        :param source: 媒体来源，非Bangumi来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持，单一接口不分电影剧集
        :param page: 本源不支持
        :param count: 本源不支持
        :return: 推荐媒体列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        if media_id is None:
            return None
        try:
            bangumiid = int(media_id)
        except (TypeError, ValueError):
            return None
        return self.bangumi_recommend(bangumiid=bangumiid)

    async def async_media_recommend(self, source: Optional[MediaSource] = None,
                                     media_id: Any = None,
                                     mtype: Optional[MediaType] = None,
                                     page: int = 1,
                                     count: Optional[int] = None,
                                     **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的相关推荐媒体（异步版本）
        :param source: 媒体来源，非Bangumi来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持，单一接口不分电影剧集
        :param page: 本源不支持
        :param count: 本源不支持
        :return: 推荐媒体列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        if media_id is None:
            return None
        try:
            bangumiid = int(media_id)
        except (TypeError, ValueError):
            return None
        return await self.async_bangumi_recommend(bangumiid=bangumiid)

    def discover(self, source: Optional[MediaSource] = None,
                 **criteria) -> Optional[List[MediaInfo]]:
        """
        按条件发现指定来源的媒体
        :param source: 媒体来源，非Bangumi来源返回 None
        :param criteria: 筛选条件，原样转发给 bangumi_discover，不补默认值
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        return self.bangumi_discover(**criteria)

    async def async_discover(self, source: Optional[MediaSource] = None,
                              **criteria) -> Optional[List[MediaInfo]]:
        """
        按条件发现指定来源的媒体（异步版本）
        :param source: 媒体来源，非Bangumi来源返回 None
        :param criteria: 筛选条件，原样转发给 async_bangumi_discover，不补默认值
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        return await self.async_bangumi_discover(**criteria)

    def discover_board(self, source: Optional[MediaSource] = None,
                        board: str = None,
                        page: int = 1,
                        count: int = 30,
                        **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的榜单
        :param source: 媒体来源，非Bangumi来源返回 None
        :param board: 榜单标识，须命中本源白名单，未登记标识返回 None
        :param page: 本源不支持，calendar 接口无分页参数
        :param count: 本源不支持，calendar 接口无分页参数
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        method_name = _DISCOVER_BOARDS.get(board)
        if method_name is None:
            return None
        return getattr(self, method_name)()

    async def async_discover_board(self, source: Optional[MediaSource] = None,
                                    board: str = None,
                                    page: int = 1,
                                    count: int = 30,
                                    **kwargs) -> Optional[List[MediaInfo]]:
        """
        查询指定来源的榜单（异步版本）
        :param source: 媒体来源，非Bangumi来源返回 None
        :param board: 榜单标识，须命中本源白名单，未登记标识返回 None
        :param page: 本源不支持，calendar 接口无分页参数
        :param count: 本源不支持，calendar 接口无分页参数
        :return: 媒体信息列表
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        method_name = _DISCOVER_BOARDS.get(board)
        if method_name is None:
            return None
        return await getattr(self, f"async_{method_name}")()

    def media_detail(self, source: Optional[MediaSource] = None,
                      media_id: Any = None,
                      mtype: Optional[MediaType] = None,
                      season: Optional[int] = None,
                      raise_exception: bool = False,
                      **kwargs) -> Optional[dict]:
        """
        查询指定来源的媒体详情
        :param source: 媒体来源，非Bangumi来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持
        :param season: 本源不支持
        :param raise_exception: 本源不支持
        :return: 媒体详情
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        if media_id is None:
            return None
        try:
            bangumiid = int(media_id)
        except (TypeError, ValueError):
            return None
        return self.bangumi_info(bangumiid=bangumiid)

    async def async_media_detail(self, source: Optional[MediaSource] = None,
                                  media_id: Any = None,
                                  mtype: Optional[MediaType] = None,
                                  season: Optional[int] = None,
                                  raise_exception: bool = False,
                                  **kwargs) -> Optional[dict]:
        """
        查询指定来源的媒体详情（异步版本）
        :param source: 媒体来源，非Bangumi来源返回 None
        :param media_id: 媒体来源原生ID，须可转换为int，转换失败或为空返回 None
        :param mtype: 本源不支持
        :param season: 本源不支持
        :param raise_exception: 本源不支持
        :return: 媒体详情
        """
        if normalize_media_source(source) is not MediaSource.Bangumi:
            return None
        if media_id is None:
            return None
        try:
            bangumiid = int(media_id)
        except (TypeError, ValueError):
            return None
        return await self.async_bangumi_info(bangumiid=bangumiid)
