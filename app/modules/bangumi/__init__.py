"""
Bangumi 模块，提供番剧识别、搜索、每日放送、人物和刮削接口。
"""

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Union

from app.adapters.network.http import RequestUtils
from app.domain.context import MediaInfo
from app.domain.media import is_media_source_enabled
from app.domain.meta.metabase import MetaBase
from app.domain.projection.bangumi import resolve_media_type as resolve_bangumi_media_type
from app.domain.scraper import MediaScraperHelper
from app.modules import _ModuleBase
from app.modules._base.media import MediaAuxiliaryProviderMixin
from app.modules.bangumi.bangumi import BangumiApi
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.context import MediaImageSet
from app.schemas.context import MediaPerson as _SchemaMediaPerson
from app.schemas.types import (
    MediaRecognizeType,
    MediaSource,
    MediaSourceSelection,
    MediaType,
    ModuleType,
)


@dataclass(frozen=True, slots=True)
class BangumiConfigSnapshot:
    """
    一次模块配置加载周期内稳定使用的网络设置。

    :param proxy: 宿主 HTTP 代理配置，None 表示不使用代理
    :param proxy_enabled: 是否启用自定义 Bangumi 数据接口，默认关闭
    :param api_domain: 自定义数据接口域名或 URL；空值使用官方接口
    """

    proxy: Any
    proxy_enabled: bool = False
    api_domain: str = ""


@dataclass(frozen=True, slots=True)
class _BangumiRecognitionPlan:
    """
    决定一次 Bangumi 识别使用明确身份还是标题候选。

    :param media_id: 已校验的正整数条目标识，None 表示按标题搜索
    :param meta: 标题解析线索，明确身份查询时可为 None
    :param label: 用于识别日志的条目标识或搜索名称
    """

    media_id: Optional[int]
    meta: Optional[MetaBase]
    label: str

    def require_meta(self) -> MetaBase:
        """
        读取标题搜索所需的解析线索。

        :return: 计划保存的标题线索
        :raises RuntimeError: 计划不包含 meta，调用方错误进入标题搜索分支
        """
        if self.meta is None:
            raise RuntimeError("Bangumi 标题识别计划缺少解析元数据")
        return self.meta


@dataclass(frozen=True, slots=True)
class _BangumiSearchPlan:
    """
    保存来源是否启用以及有效的标题搜索线索。

    :param enabled: 请求来源选择是否包含 Bangumi
    :param meta: 有效标题线索；来源未启用或名称缺失时为 None
    """

    enabled: bool
    meta: Optional[MetaBase]


class BangumiModule(MediaAuxiliaryProviderMixin, _ModuleBase):
    """
    提供 Bangumi 媒体识别、人物查询及刮削能力。
    """
    auxiliary_media_source = MediaSource.Bangumi
    CONFIG_WATCH = {
        "PROXY_HOST",
        "BANGUMI_PROXY_ENABLE",
        "BANGUMI_API_DOMAIN",
    }

    bangumiapi: Optional[BangumiApi] = None
    scraper: Optional[MediaScraperHelper] = None
    _config: BangumiConfigSnapshot = BangumiConfigSnapshot(proxy=None)

    def _require_api(self) -> BangumiApi:
        """
        取得已激活的 Bangumi 客户端，避免把尚未初始化的兼容属性当作有效实例。

        :return: 模块初始化时创建的客户端
        :raises RuntimeError: 模块尚未初始化
        """
        if self.bangumiapi is None:
            raise RuntimeError("Bangumi 模块尚未初始化")
        return self.bangumiapi

    def _require_scraper(self) -> MediaScraperHelper:
        """
        取得模块初始化时创建的刮削器。

        :return: 已初始化的刮削器
        :raises RuntimeError: 模块尚未初始化
        """
        if self.scraper is None:
            raise RuntimeError("Bangumi 刮削器尚未初始化")
        return self.scraper

    def init_module(self) -> None:
        """
        初始化Bangumi客户端。
        """
        self._config = BangumiConfigSnapshot(
            proxy=get_runtime_setting('PROXY'),
            proxy_enabled=bool(
                get_runtime_setting('BANGUMI_PROXY_ENABLE', False)
            ),
            api_domain=str(
                get_runtime_setting('BANGUMI_API_DOMAIN', '') or ''
            ),
        )
        self.bangumiapi = BangumiApi(
            base_url=self._config.api_domain if self._config.proxy_enabled else '',
        )
        self.scraper = MediaScraperHelper()

    def stop(self) -> None:
        """
        关闭Bangumi客户端。
        """
        # 请求缓存键已包含 API 地址，切换代理或域名后自然使用新键；这里不清缓存，
        # 以免 Redis 缓存在每次重载和关闭时都被清空。
        if self.bangumiapi:
            self._require_api().close()

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性。
        """
        api_url = self._require_api().base_url if self.bangumiapi else "https://api.bgm.tv/"
        ret = RequestUtils(proxies=self._config.proxy).get_res(api_url)
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接Bangumi，错误码：{ret.status_code}"
        return False, "Bangumi网络连接失败"

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        Bangumi模块无需独立开关。
        """
        return None

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称。
        """
        return "Bangumi"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        """
        获取模块子类型。
        """
        return MediaRecognizeType.Bangumi

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 3

    @staticmethod
    def _recognition_plan(
        meta: Optional[MetaBase],
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        requested_type: Optional[MediaType],
    ) -> Optional[_BangumiRecognitionPlan]:
        """
        统一校验来源、媒体类型和显式 Bangumi ID。
        """
        if requested_type == MediaType.MUSIC or getattr(meta, "type", None) == MediaType.MUSIC:
            return None
        if media_source and media_source != MediaSource.Bangumi:
            return None
        if media_id is not None:
            if media_source != MediaSource.Bangumi or not str(media_id).isdigit():
                return None
            normalized_id = int(media_id)
            if normalized_id <= 0:
                return None
            return _BangumiRecognitionPlan(
                media_id=normalized_id, meta=meta, label=str(normalized_id)
            )
        selected_source = media_source or get_runtime_setting('RECOGNIZE_SOURCE')
        if not meta or selected_source != MediaSource.Bangumi:
            return None
        return _BangumiRecognitionPlan(media_id=None, meta=meta, label=meta.name)

    @staticmethod
    def _search_plan(
        meta: Optional[MetaBase],
        media_source: Optional[MediaSourceSelection],
    ) -> _BangumiSearchPlan:
        """
        统一决定 Bangumi 搜索是否响应以及是否具备查询标题。
        """
        enabled = is_media_source_enabled(media_source, MediaSource.Bangumi)
        return _BangumiSearchPlan(
            enabled=enabled,
            meta=meta if enabled and meta and meta.name else None,
        )

    @staticmethod
    def _candidate_ids(
        infos: Optional[list[dict[str, Any]]],
    ) -> tuple[int, ...]:
        """
        提取前十个可用于详情查询的 Bangumi 数字 ID。
        """
        return tuple(
            int(item["id"])
            for item in (infos or [])[:10]
            if str(item.get("id") or "").isdigit()
        )

    @staticmethod
    def _build_recognized_media(
        info: dict[str, Any],
        actors: list[dict[str, Any]],
        meta: Optional[MetaBase],
    ) -> MediaInfo:
        """
        把 Bangumi 详情和演职员投影为统一媒体信息。
        """
        enriched = {**info, "actors": actors}
        mediainfo = MediaInfo(bangumi_info=enriched)
        if meta and meta.begin_season is not None:
            mediainfo.season = meta.begin_season
        return mediainfo

    @staticmethod
    def _project_media_list(
        infos: Optional[list[dict[str, Any]]],
    ) -> list[MediaInfo]:
        """
        把 Bangumi 媒体列表投影为统一媒体信息列表。
        """
        return [MediaInfo(bangumi_info=info) for info in infos or []]

    @classmethod
    def _project_search_results(
        cls, meta: MetaBase, infos: Optional[list[dict[str, Any]]]
    ) -> list[MediaInfo]:
        """
        按标题包含关系过滤 Bangumi 搜索结果并统一投影。
        """
        query = meta.name.lower()
        return cls._project_media_list(
            [
                info
                for info in infos or []
                if query in str(info.get("name")).lower()
                or query in str(info.get("name_cn")).lower()
            ]
        )

    @staticmethod
    def _project_people(
        persons: Optional[list[dict[str, Any]]],
    ) -> list[_SchemaMediaPerson]:
        """
        把 Bangumi 演职员列表投影为统一人物列表。
        """
        return [_SchemaMediaPerson(source='bangumi', **person) for person in persons or []]

    def recognize_media(
        self,
        meta: Optional[MetaBase] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[MediaInfo]:
        """
        按明确条目标识或标题候选识别 Bangumi 媒体。

        :param meta: 标题、年份、媒体类型和可选季号线索
        :param media_source: 明确来源；其他来源不处理，标题搜索缺省时使用识别来源配置
        :param media_id: Bangumi 正整数标识，仅在明确选择 Bangumi 时接受
        :param kwargs: mtype 指定请求媒体类型，音乐请求不处理，其他参数不使用
        :return: 带配音人物的统一媒体信息；请求不适用、查询失败或无匹配时为 None
        """
        plan = self._recognition_plan(
            meta, media_source, media_id, kwargs.get("mtype")
        )
        if not plan:
            return None
        info = (
            self.bangumi_info(bangumiid=plan.media_id)
            if plan.media_id is not None
            else self._match_by_meta(plan.require_meta())
        )
        if info:
            bangumi_id = info.get("id")
            if not isinstance(bangumi_id, int):
                return None
            actors = self._require_api().credits(bangumi_id)
            mediainfo = self._build_recognized_media(info, actors, plan.meta)
            logger.info(f"{plan.label} Bangumi识别结果：{mediainfo.type.value if mediainfo.type else ''} "
                        f"{mediainfo.title_year}")
            return mediainfo
        logger.info(f"{plan.label} 未匹配到Bangumi媒体信息")

        return None

    async def async_recognize_media(
        self,
        meta: Optional[MetaBase] = None,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[MediaInfo]:
        """
        异步按明确条目标识或标题候选识别 Bangumi 媒体。

        :param meta: 标题、年份、媒体类型和可选季号线索
        :param media_source: 明确来源；其他来源不处理，标题搜索缺省时使用识别来源配置
        :param media_id: Bangumi 正整数标识，仅在明确选择 Bangumi 时接受
        :param kwargs: mtype 指定请求媒体类型，音乐请求不处理，其他参数不使用
        :return: 带配音人物的统一媒体信息；请求不适用、查询失败或无匹配时为 None
        """
        plan = self._recognition_plan(
            meta, media_source, media_id, kwargs.get("mtype")
        )
        if not plan:
            return None
        info = (
            await self.async_bangumi_info(bangumiid=plan.media_id)
            if plan.media_id is not None
            else await self._async_match_by_meta(plan.require_meta())
        )
        if info:
            bangumi_id = info.get("id")
            if not isinstance(bangumi_id, int):
                return None
            actors = await self._require_api().async_credits(bangumi_id)
            mediainfo = self._build_recognized_media(info, actors, plan.meta)
            logger.info(f"{plan.label} Bangumi识别结果：{mediainfo.type.value if mediainfo.type else ''} "
                        f"{mediainfo.title_year}")
            return mediainfo
        logger.info(f"{plan.label} 未匹配到Bangumi媒体信息")

        return None

    @staticmethod
    def _matches_meta(meta: MetaBase, info: dict[str, Any]) -> bool:
        """
        判断Bangumi候选项是否符合标题解析出的类型与年份。

        :param meta: 标题解析元数据
        :param info: Bangumi候选项详情
        :return: 是否符合筛选条件
        """
        if (
            meta.type in {MediaType.MOVIE, MediaType.TV}
            and resolve_bangumi_media_type(info) != meta.type
        ):
            return False
        release_date = info.get("date") or info.get("air_date") or ""
        return not meta.year or not release_date or release_date[:4] == str(meta.year)

    def _match_by_meta(self, meta: MetaBase) -> Optional[dict[str, Any]]:
        """
        搜索并获取最符合标题解析结果的Bangumi详情。

        :param meta: 标题解析元数据
        :return: Bangumi媒体详情
        """
        for media_id in self._candidate_ids(self._require_api().search(meta.name)):
            info = self._require_api().detail(media_id)
            if info and self._matches_meta(meta, info):
                return info
        return None

    async def _async_match_by_meta(self, meta: MetaBase) -> Optional[dict[str, Any]]:
        """
        异步搜索并获取最符合标题解析结果的Bangumi详情。

        :param meta: 标题解析元数据
        :return: Bangumi媒体详情
        """
        infos = await self._require_api().async_search(meta.name)
        for media_id in self._candidate_ids(infos):
            info = await self._require_api().async_detail(media_id)
            if info and self._matches_meta(meta, info):
                return info
        return None

    def search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        搜索标题候选，并保留原名或中文名包含查询词的条目。

        :param meta: 包含搜索名称的媒体线索
        :param media_source: 请求的来源选择条件
        :return: 统一媒体列表；来源未启用为 None，标题缺失或无匹配时为空列表
        """
        plan = self._search_plan(meta, media_source)
        if not plan.enabled:
            return None
        if not plan.meta:
            return []
        return self._project_search_results(
            plan.meta, self._require_api().search(plan.meta.name)
        )

    async def async_search_medias(
        self, meta: MetaBase, media_source: Optional[MediaSourceSelection] = None
    ) -> Optional[List[MediaInfo]]:
        """
        异步搜索标题候选，并保留原名或中文名包含查询词的条目。

        :param meta: 包含搜索名称的媒体线索
        :param media_source: 请求的来源选择条件
        :return: 统一媒体列表；来源未启用为 None，标题缺失或无匹配时为空列表
        """
        plan = self._search_plan(meta, media_source)
        if not plan.enabled:
            return None
        if not plan.meta:
            return []
        infos = await self._require_api().async_search(plan.meta.name)
        return self._project_search_results(plan.meta, infos)

    def bangumi_info(self, bangumiid: int) -> Optional[dict[str, Any]]:
        """
        获取原始 Bangumi 条目详情。

        :param bangumiid: Bangumi 条目数字标识
        :return: 原始详情；标识为空或查询失败时为 None
        """
        if not bangumiid:
            return None
        logger.info(f"开始获取Bangumi信息：{bangumiid} ...")
        return self._require_api().detail(bangumiid)

    async def async_bangumi_info(self, bangumiid: int) -> Optional[dict[str, Any]]:
        """
        异步获取原始 Bangumi 条目详情。

        :param bangumiid: Bangumi 条目数字标识
        :return: 原始详情；标识为空或查询失败时为 None
        """
        if not bangumiid:
            return None
        logger.info(f"开始获取Bangumi信息：{bangumiid} ...")
        return await self._require_api().async_detail(bangumiid)

    def metadata_nfo(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        """
        为选择 Bangumi 刮削来源的媒体生成 NFO 内容。

        :param mediainfo: 统一媒体信息，scrape_source 优先于全局刮削配置
        :param season: 目标季号，省略时生成媒体级元数据
        :param episode: 目标集号，省略时由季号决定元数据范围
        :param kwargs: 统一刮削接口的额外参数，当前不使用
        :return: 包含 UTF-8 编码声明的 XML 文本；刮削来源不是 Bangumi 或无法生成时为 None
        """
        scrape_source = mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')
        if scrape_source != "bangumi":
            return None
        return self._require_scraper().get_metadata_nfo(mediainfo, season=season, episode=episode)

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """
        为选择 Bangumi 刮削来源的媒体生成图片清单。

        :param mediainfo: 统一媒体信息，scrape_source 优先于全局刮削配置
        :param season: 可选目标季号
        :param episode: 可选目标集号
        :return: 图片文件名与地址映射；刮削来源不匹配时为 None
        """
        scrape_source = mediainfo.scrape_source or get_runtime_setting('SCRAP_SOURCE')
        if scrape_source != "bangumi":
            return None
        return self._require_scraper().get_metadata_img(mediainfo, season=season, episode=episode)

    def bangumi_calendar(self) -> Optional[List[MediaInfo]]:
        """
        读取每日放送的统一媒体列表。

        :return: 媒体列表；没有放送内容或请求失败时为空列表
        """
        return self._project_media_list(self._require_api().calendar())

    async def async_bangumi_calendar(self) -> Optional[List[MediaInfo]]:
        """
        异步读取每日放送的统一媒体列表。

        :return: 媒体列表；没有放送内容或请求失败时为空列表
        """
        infos = await self._require_api().async_calendar()
        return self._project_media_list(infos)

    def bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        按 Bangumi 条目标识查询角色配音人物。

        :param bangumiid: Bangumi 条目标识，不接受其他来源标识
        :return: 统一人物列表；无可用结果时为空列表
        """
        return self._project_people(self._require_api().credits(bangumiid))

    async def async_bangumi_credits(self, bangumiid: int) -> List[_SchemaMediaPerson]:
        """
        异步按 Bangumi 条目标识查询角色配音人物。

        :param bangumiid: Bangumi 条目标识，不接受其他来源标识
        :return: 统一人物列表；无可用结果时为空列表
        """
        persons = await self._require_api().async_credits(bangumiid)
        return self._project_people(persons)

    def bangumi_recommend(self, bangumiid: int) -> List[MediaInfo]:
        """
        查询 Bangumi 条目的关联作品。

        :param bangumiid: Bangumi 条目数字标识
        :return: 统一媒体列表；无关联作品或请求失败时为空列表
        """
        return self._project_media_list(self._require_api().subjects(bangumiid))

    async def async_bangumi_recommend(self, bangumiid: int) -> List[MediaInfo]:
        """
        异步查询 Bangumi 条目的关联作品。

        :param bangumiid: Bangumi 条目数字标识
        :return: 统一媒体列表；无关联作品或请求失败时为空列表
        """
        subjects = await self._require_api().async_subjects(bangumiid)
        return self._project_media_list(subjects)

    def bangumi_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        查询 Bangumi 人物详情并转换为统一模型。

        :param person_id: Bangumi 人物数字标识
        :return: 统一人物对象；查询失败或无结果时为 None
        """
        personinfo = self._require_api().person_detail(person_id)
        if personinfo:
            return self._build_person_detail(personinfo)
        return None

    async def async_bangumi_person_detail(self, person_id: int) -> Optional[_SchemaMediaPerson]:
        """
        异步查询 Bangumi 人物详情并转换为统一模型。

        :param person_id: Bangumi 人物数字标识
        :return: 统一人物对象；查询失败或无结果时为 None
        """
        personinfo = await self._require_api().async_person_detail(person_id)
        if personinfo:
            return self._build_person_detail(personinfo)
        return None

    @classmethod
    def _build_person_detail(cls, personinfo: dict[str, Any]) -> _SchemaMediaPerson:
        """
        构造Bangumi人物详情信息。

        :param personinfo: Bangumi人物详情接口返回数据
        :return: 媒体人物信息
        """
        images = personinfo.get("images")
        image_set = MediaImageSet(**images) if isinstance(images, dict) else None
        person_id = personinfo.get("id")
        person_name = personinfo.get("name")
        gender = personinfo.get("gender")
        return _SchemaMediaPerson(
            source="bangumi",
            id=person_id if isinstance(person_id, (int, str)) else None,
            name=str(person_name) if person_name is not None else None,
            images=image_set,
            biography=cls._normalize_optional_string(personinfo.get("summary")),
            birthday=cls._normalize_optional_string(personinfo.get("birth_day")),
            gender=gender if isinstance(gender, (int, str)) else None,
        )

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
        查询 Bangumi 人物参与的作品。

        :param person_id: Bangumi 人物数字标识
        :return: 统一媒体列表；无作品或请求失败时为空列表
        """
        return self._project_media_list(
            self._require_api().person_credits(person_id=person_id)
        )

    async def async_bangumi_person_credits(self, person_id: int) -> List[MediaInfo]:
        """
        异步查询 Bangumi 人物参与的作品。

        :param person_id: Bangumi 人物数字标识
        :return: 统一媒体列表；无作品或请求失败时为空列表
        """
        credits_info = await self._require_api().async_person_credits(person_id=person_id)
        return self._project_media_list(credits_info)

    def bangumi_discover(self, **kwargs: Any) -> Optional[List[MediaInfo]]:
        """
        按筛选条件发现 Bangumi 番剧。

        :param kwargs: 原样传入 API 的筛选和分页参数
        :return: 统一媒体列表；无结果或请求失败时为空列表
        """
        return self._project_media_list(self._require_api().discover(**kwargs))

    async def async_bangumi_discover(self, **kwargs: Any) -> Optional[List[MediaInfo]]:
        """
        异步按筛选条件发现 Bangumi 番剧。

        :param kwargs: 原样传入 API 的筛选和分页参数
        :return: 统一媒体列表；无结果或请求失败时为空列表
        """
        infos = await self._require_api().async_discover(**kwargs)
        return self._project_media_list(infos)

    def clear_cache(self) -> None:
        """
        清除缓存。
        """
        logger.info(f"开始清除{self.get_name()}缓存 ...")
        self._require_api().clear_cache()
        logger.info(f"{self.get_name()}缓存清除完成")
