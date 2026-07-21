from typing import List, Optional, Tuple, Union

from app import schemas
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.helper.scraper import MediaScraperHelper
from app.log import logger
from app.modules import _ModuleBase
from app.modules.bangumi.bangumi import BangumiApi
from app.schemas.types import MediaRecognizeType, MediaType, ModuleType
from app.utils.http import RequestUtils


class BangumiModule(_ModuleBase):
    """
    Bangumi媒体信息匹配
    """
    CONFIG_WATCH = {"PROXY_HOST"}

    bangumiapi: BangumiApi = None
    scraper: MediaScraperHelper = None

    def init_module(self) -> None:
        """
        初始化Bangumi客户端
        """
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
        ret = RequestUtils(proxies=settings.PROXY).get_res("https://api.bgm.tv/")
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
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        获取模块子类型
        """
        return MediaRecognizeType.Bangumi

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 3

    def recognize_media(
        self,
        meta: MetaBase = None,
        bangumiid: int = None,
        source: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息
        :param meta: 识别的元数据
        :param bangumiid: 识别的Bangumi ID
        :param source: 请求级识别数据源
        :return: 识别的媒体信息，包括剧集信息
        """
        if not bangumiid and (
            not meta or (source or settings.RECOGNIZE_SOURCE) != "bangumi"
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
        bangumiid: int = None,
        source: Optional[str] = None,
        **kwargs,
    ) -> Optional[MediaInfo]:
        """
        识别媒体信息（异步版本）
        :param meta: 识别的元数据
        :param bangumiid: 识别的Bangumi ID
        :param source: 请求级识别数据源
        :return: 识别的媒体信息，包括剧集信息
        """
        if not bangumiid and (
            not meta or (source or settings.RECOGNIZE_SOURCE) != "bangumi"
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
        self, meta: MetaBase, source: Optional[str] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :param source: 请求级搜索数据源
        :return: 媒体信息
        """
        if source and source != "bangumi":
            return None
        if not source and settings.SEARCH_SOURCE and "bangumi" not in settings.SEARCH_SOURCE:
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
        self, meta: MetaBase, source: Optional[str] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息（异步版本）
        :param meta:  识别的元数据
        :param source: 请求级搜索数据源
        :return: 媒体信息
        """
        if source and source != "bangumi":
            return None
        if not source and settings.SEARCH_SOURCE and "bangumi" not in settings.SEARCH_SOURCE:
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

    def bangumi_credits(self, bangumiid: int) -> List[schemas.MediaPerson]:
        """
        根据TMDBID查询电影演职员表
        :param bangumiid:  BangumiID
        """
        persons = self.bangumiapi.credits(bangumiid)
        if persons:
            return [schemas.MediaPerson(source='bangumi', **person) for person in persons]
        return []

    async def async_bangumi_credits(self, bangumiid: int) -> List[schemas.MediaPerson]:
        """
        根据TMDBID查询电影演职员表（异步版本）
        :param bangumiid:  BangumiID
        """
        persons = await self.bangumiapi.async_credits(bangumiid)
        if persons:
            return [schemas.MediaPerson(source='bangumi', **person) for person in persons]
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

    def bangumi_person_detail(self, person_id: int) -> Optional[schemas.MediaPerson]:
        """
        获取人物详细信息
        :param person_id:  豆瓣人物ID
        """
        personinfo = self.bangumiapi.person_detail(person_id)
        if personinfo:
            return self._build_person_detail(personinfo)
        return None

    async def async_bangumi_person_detail(self, person_id: int) -> Optional[schemas.MediaPerson]:
        """
        获取人物详细信息（异步版本）
        :param person_id:  豆瓣人物ID
        """
        personinfo = await self.bangumiapi.async_person_detail(person_id)
        if personinfo:
            return self._build_person_detail(personinfo)
        return None

    @classmethod
    def _build_person_detail(cls, personinfo: dict) -> schemas.MediaPerson:
        """
        构造Bangumi人物详情信息。
        :param personinfo: Bangumi人物详情接口返回数据
        :return: 媒体人物信息
        """
        return schemas.MediaPerson(source='bangumi', **{
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
