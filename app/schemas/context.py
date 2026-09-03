from typing import Annotated, Any, Dict, List, Optional, Union

from pydantic import BaseModel, Discriminator, Field, RootModel, Tag, model_validator

from app.schemas.category import ClassificationFactValue, ClassificationResult
from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.music import MusicInfo, MusicMeta
from app.schemas.types import MediaSource


class MetaInfo(OptionalMediaIdentityMixin, BaseModel):
    """
    识别元数据
    """
    # 是否处理的文件
    isfile: Optional[bool] = False
    # 原字符串
    org_string: Optional[str] = None
    # 原标题
    title: Optional[str] = None
    # 副标题
    subtitle: Optional[str] = None
    # 类型 电影、电视剧、音乐
    type: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 识别的中文名
    cn_name: Optional[str] = None
    # 识别的英文名
    en_name: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 总季数
    total_season: Optional[int] = 0
    # 识别的开始季 数字
    begin_season: Optional[int] = None
    # 识别的结束季 数字
    end_season: Optional[int] = None
    # 总集数
    total_episode: Optional[int] = 0
    # 识别的开始集
    begin_episode: Optional[int] = None
    # 识别的结束集
    end_episode: Optional[int] = None
    # SxxExx
    season_episode: Optional[str] = None
    # 集列表
    episode_list: Optional[List[int]] = Field(default_factory=list)
    # Partx Cd Dvd Disk Disc
    part: Optional[str] = None
    # 识别的资源类型
    resource_type: Optional[str] = None
    # 识别的效果
    resource_effect: Optional[str] = None
    # 识别的分辨率
    resource_pix: Optional[str] = None
    # 识别的制作组/字幕组
    resource_team: Optional[str] = None
    # 视频编码
    video_encode: Optional[str] = None
    # 音频编码
    audio_encode: Optional[str] = None
    # 资源类型
    edition: Optional[str] = None
    # 流媒体平台
    web_source: Optional[str] = None
    # 应用的识别词信息
    apply_words: Optional[List[str]] = None
    # 剧集组
    episode_group: Optional[str] = None
    # 显式媒体数据源
    media_source: Optional[MediaSource] = None
    # 显式媒体数据源原生ID
    media_id: Optional[str] = None


class MediaImageSet(BaseModel):
    """跨媒体源兼容的人物图片尺寸集合。"""

    large: Optional[str] = None
    common: Optional[str] = None
    medium: Optional[str] = None
    normal: Optional[str] = None
    small: Optional[str] = None
    grid: Optional[str] = None


class MediaCredit(BaseModel):
    """影视条目中的演职员摘要。"""

    id: Optional[int | str] = None
    name: Optional[str] = None
    original_name: Optional[str] = None
    character: Optional[str] = None
    type: Optional[str | int] = None
    gender: Optional[str | int] = None
    adult: Optional[bool] = None
    known_for_department: Optional[str] = None
    profile_path: Optional[str] = None
    credit_id: Optional[str] = None
    cast_id: Optional[int] = None
    order: Optional[int] = None
    department: Optional[str] = None
    job: Optional[str] = None
    popularity: Optional[float] = None
    roles: list[str] = Field(default_factory=list)
    title: Optional[str] = None
    url: Optional[str] = None
    uri: Optional[str] = None
    sharing_url: Optional[str] = None
    avatar: Optional[str | MediaImageSet] = None
    images: Optional[MediaImageSet] = None
    latin_name: Optional[str] = None
    career: list[str] = Field(default_factory=list)
    relation: Optional[str] = None
    user: Optional[JsonData] = None


class MediaGenre(BaseModel):
    """影视风格摘要。"""

    id: Optional[int | str] = None
    name: Optional[str] = None


class MediaCompany(BaseModel):
    """电视网或制作公司的标准摘要。"""

    id: Optional[int | str] = None
    name: Optional[str] = None
    logo_path: Optional[str] = None
    origin_country: Optional[str] = None


class MediaCountry(BaseModel):
    """影视制作国家或地区。"""

    id: Optional[int | str] = None
    iso_3166_1: Optional[str] = None
    name: Optional[str] = None


class MediaLanguage(BaseModel):
    """影视内容使用的语言。"""

    english_name: Optional[str] = None
    iso_639_1: Optional[str] = None
    name: Optional[str] = None


class MediaReleaseDate(BaseModel):
    """电影在单个地区的一次发行记录。"""

    date: str
    iso_code: Optional[str] = None
    note: Optional[str] = None
    type: Optional[int] = None


class MediaEpisode(BaseModel):
    """电视剧即将播出的单集摘要。"""

    id: Optional[int] = None
    air_date: Optional[str] = None
    episode_number: Optional[int] = None
    episode_type: Optional[str] = None
    name: Optional[str] = None
    overview: Optional[str] = None
    production_code: Optional[str] = None
    runtime: Optional[int] = None
    season_number: Optional[int] = None
    show_id: Optional[int] = None
    still_path: Optional[str] = None
    vote_average: Optional[float] = None
    vote_count: Optional[int] = None


class MediaSeason(BaseModel):
    """标准季信息以及剧集组季信息。"""

    id: Optional[int | str] = None
    air_date: Optional[str] = None
    episode_count: Optional[int] = None
    name: Optional[str] = None
    overview: Optional[str] = None
    poster_path: Optional[str] = None
    season_number: Optional[int] = None
    vote_average: Optional[float] = None
    order: Optional[int] = None
    locked: Optional[bool] = None
    episodes: list[MediaEpisode] = Field(default_factory=list)


class MediaEpisodeGroupNetwork(BaseModel):
    """TMDB 剧集组所属电视网信息。"""

    id: Optional[int] = None
    name: Optional[str] = None
    logo_path: Optional[str] = None
    origin_country: Optional[str] = None


class MediaEpisodeGroup(BaseModel):
    """TMDB 电视剧的剧集分组摘要。"""

    description: str = ""
    episode_count: int = 0
    group_count: int = 0
    id: str
    name: str
    network: Optional[MediaEpisodeGroupNetwork] = None
    type: int


class MediaInfo(OptionalMediaIdentityMixin, BaseModel):
    """
    识别媒体信息
    """
    # 媒体主身份来源
    media_source: Optional[MediaSource] = None
    # 请求级刮削来源
    scrape_source: Optional[str] = None
    # 类型 电影、电视剧、合集
    type: Optional[str] = None
    # 媒体标题
    title: Optional[str] = None
    # 英文标题
    en_title: Optional[str] = None
    # 香港、台湾、新加坡地区标题
    hk_title: Optional[str] = None
    tw_title: Optional[str] = None
    sg_title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 标题（年份）
    title_year: Optional[str] = None
    # 当前指定季，如有
    season: Optional[int] = None
    # 数据源返回的辅助 ID，仅作为元数据输出
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    tvdb_id: Optional[int] = None
    tvdb_slug: Optional[str] = None
    douban_id: Optional[str] = None
    bangumi_id: Optional[int] = None
    anilist_id: Optional[int] = None
    anidb_id: Optional[int] = None
    # 合集ID
    collection_id: Optional[int] = None
    # 当前来源原生 ID
    media_id: Optional[str] = None
    # 媒体原语种
    original_language: Optional[str] = None
    # 媒体原发行标题
    original_title: Optional[str] = None
    # 媒体发行日期
    release_date: Optional[str] = None
    # 背景图片
    backdrop_path: Optional[str] = None
    # 海报图片
    poster_path: Optional[str] = None
    # 标题 LOGO
    logo_path: Optional[str] = None
    # 评分
    vote_average: Optional[float] = 0.0
    # 描述
    overview: Optional[str] = None
    # 媒体库目录分类；category 是过渡期兼容字段
    library_category: Optional[str] = ""
    # 来源提供的描述性分类，不参与目录选择
    metadata_category: Optional[str] = ""
    # 本次分类使用的推荐、生效结果和策略快照
    classification: Optional[ClassificationResult] = None
    # 插件来源提交的受控扩展分类事实，键使用完整 extensions.<source>.* 字段 ID
    classification_facts: Dict[str, ClassificationFactValue] = Field(default_factory=dict)
    # 二级分类兼容字段，始终映射到 library_category
    category: Optional[str] = ""
    # 季季集清单
    seasons: Optional[Dict[int, list[int]]] = Field(default_factory=dict)
    # 季详情
    season_info: Optional[List[MediaSeason]] = Field(default_factory=list)
    # 各季首播年份
    season_years: Optional[Dict[int, str]] = Field(default_factory=dict)
    # 别名和译名
    names: Optional[list[str]] = Field(default_factory=list)
    # 演员
    actors: Optional[list[Union[MediaCredit, str]]] = Field(default_factory=list)
    # 导演
    directors: Optional[list[Union[MediaCredit, str]]] = Field(default_factory=list)
    # 详情链接
    detail_link: Optional[str] = None
    # 其它TMDB属性
    # 是否成人内容
    adult: Optional[bool] = False
    # 创建人
    created_by: Optional[list[MediaCredit]] = Field(default_factory=list)
    # 集时长
    episode_run_time: Optional[list[int]] = Field(default_factory=list)
    # 风格
    genres: Optional[List[MediaGenre]] = Field(default_factory=list)
    # 首播日期
    first_air_date: Optional[str] = None
    # 首页
    homepage: Optional[str] = None
    # 语种
    languages: Optional[list[str]] = Field(default_factory=list)
    # 最后上映日期
    last_air_date: Optional[str] = None
    # 流媒体平台
    networks: Optional[list[MediaCompany]] = Field(default_factory=list)
    # 集数
    number_of_episodes: Optional[int] = 0
    # 季数
    number_of_seasons: Optional[int] = 0
    # 原产国
    origin_country: Optional[list[str]] = Field(default_factory=list)
    # 原名
    original_name: Optional[str] = None
    # 出品公司
    production_companies: Optional[list[MediaCompany]] = Field(default_factory=list)
    # 出品国
    production_countries: Optional[list[MediaCountry]] = Field(default_factory=list)
    # 语种
    spoken_languages: Optional[list[MediaLanguage]] = Field(default_factory=list)
    # 所有发行日期
    release_dates: list[MediaReleaseDate] = Field(default_factory=list)
    # 状态
    status: Optional[str] = None
    # 标签
    tagline: Optional[str] = None
    # 风格ID
    genre_ids: Optional[list[int | str]] = Field(default_factory=list)
    # 评价数量
    vote_count: Optional[int] = 0
    # 流行度
    popularity: Optional[float] = 0.0
    # 时长
    runtime: Optional[int] = None
    # 下一集
    next_episode_to_air: Optional[MediaEpisode] = None
    # 内容分级
    content_rating: Optional[str] = None
    # 全部剧集组
    episode_groups: Optional[list[MediaEpisodeGroup | MediaSeason]] = Field(default_factory=list)
    # 剧集组
    episode_group: Optional[str] = None
    # 各数据源原始信息；Core MediaInfo.to_dict() 保留这些键供兼容调用方使用。
    tmdb_info: Optional[dict[str, JsonData]] = None
    douban_info: Optional[dict[str, JsonData]] = None
    bangumi_info: Optional[dict[str, JsonData]] = None
    anilist_info: Optional[dict[str, JsonData]] = None

    @model_validator(mode="before")  # type: ignore[misc]
    @classmethod
    def _normalize_category_compatibility(cls, value: Any) -> Any:
        """把旧影视 category 输入归一为媒体库分类，并保持兼容字段双写。"""
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        library_category = payload.get("library_category")
        if library_category is None or library_category == "":
            library_category = payload.get("category") or ""
        payload["library_category"] = library_category
        payload["category"] = library_category
        return payload


class TorrentInfo(OptionalMediaIdentityMixin, BaseModel):
    """
    搜索种子信息
    """
    # 站点ID
    site: Optional[int] = None
    # 站点名称
    site_name: Optional[str] = None
    # 站点Cookie
    site_cookie: Optional[str] = None
    # 站点UA
    site_ua: Optional[str] = None
    # 站点是否使用代理
    site_proxy: Optional[bool] = False
    # 站点优先级
    site_order: Optional[int] = 0
    # 站点下载器
    site_downloader: Optional[str] = None
    # 种子名称
    title: Optional[str] = None
    # 种子副标题
    description: Optional[str] = None
    # 种子页面声明的媒体身份
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    # 种子链接
    enclosure: Optional[str] = None
    # 详情页面
    page_url: Optional[str] = None
    # 种子大小
    size: Optional[float] = 0.0
    # 做种者
    seeders: Optional[int] = 0
    # 下载者
    peers: Optional[int] = 0
    # 完成者
    grabs: Optional[int] = 0
    # 发布时间
    pubdate: Optional[str] = None
    # 已过时间
    date_elapsed: Optional[str] = None
    # 免费截止时间
    freedate: Optional[str] = None
    # 上传因子
    uploadvolumefactor: Optional[float] = None
    # 下载因子
    downloadvolumefactor: Optional[float] = None
    # HR
    hit_and_run: Optional[bool] = False
    # 种子标签
    labels: Optional[list[str]] = Field(default_factory=list)
    # 种子优先级
    pri_order: Optional[int] = 0
    # 种子分类 电影/电视剧/音乐
    category: Optional[str] = None
    # 促销
    volume_factor: Optional[str] = None
    # 剩余免费时间
    freedate_diff: Optional[str] = None


class SubtitleInfo(BaseModel):
    """
    搜索字幕信息
    """
    # 站点ID
    site: Optional[int] = None
    # 站点名称
    site_name: Optional[str] = None
    # 站点Cookie
    site_cookie: Optional[str] = None
    # 站点UA
    site_ua: Optional[str] = None
    # 站点是否使用代理
    site_proxy: Optional[bool] = False
    # 站点优先级
    site_order: Optional[int] = 0
    # 字幕标题
    title: Optional[str] = None
    # 字幕描述
    description: Optional[str] = None
    # 字幕下载链接
    enclosure: Optional[str] = None
    # 详情页面
    page_url: Optional[str] = None
    # 语言
    language: Optional[str] = None
    # 语言图标
    language_icon: Optional[str] = None
    # 字幕大小
    size: Optional[float] = 0.0
    # 发布时间
    pubdate: Optional[str] = None
    # 已过时间
    date_elapsed: Optional[str] = None
    # 点击/下载次数
    grabs: Optional[int] = 0
    # 上传者
    uploader: Optional[str] = None
    # 举报页面
    report_url: Optional[str] = None
    # 种子ID
    torrent_id: Optional[str] = None
    # 字幕ID
    subtitle_id: Optional[str] = None
    # 下载文件名
    file_name: Optional[str] = None
    # 识别元数据
    meta_info: Optional[MetaInfo] = None
    # SxxExx
    season_episode: Optional[str] = None
    # 集列表
    episode_list: Optional[List[int]] = Field(default_factory=list)


class Context(BaseModel):
    """
    上下文
    """
    # 元数据
    meta_info: Optional[Union[MusicMeta, MetaInfo]] = None
    # 媒体信息
    media_info: Optional[Union[MusicInfo, MediaInfo]] = None
    # 种子信息
    torrent_info: Optional[TorrentInfo] = None
    # 候选资源来源：rss、spider、search、unknown
    resource_source: Optional[str] = "unknown"
    # 候选匹配来源：MediaSource 枚举值、title、unknown
    match_source: Optional[str] = "unknown"
    # 候选自身是否已经识别出有效媒体 ID
    candidate_recognized: Optional[bool] = False
    # 当前 media_info 是否为目标媒体回填
    media_info_is_target: Optional[bool] = False
    # 下载层确认候选资源覆盖完整目标范围，供订阅事实写入判断整包资源
    confirmed_full_coverage: Optional[bool] = False


class MediaPerson(BaseModel):
    """
    媒体人物信息
    """
    # 来源：themoviedb、douban、bangumi、anilist
    source: Optional[str] = None
    # 公共
    id: Optional[int | str] = None
    type: Optional[Union[str, int]] = 1
    name: Optional[str] = None
    character: Optional[str] = None
    images: Optional[MediaImageSet] = None
    # themoviedb
    profile_path: Optional[str] = None
    gender: Optional[Union[str, int]] = None
    original_name: Optional[str] = None
    credit_id: Optional[str] = None
    also_known_as: Optional[list[str]] = Field(default_factory=list)
    birthday: Optional[str] = None
    deathday: Optional[str] = None
    imdb_id: Optional[str] = None
    known_for_department: Optional[str] = None
    place_of_birth: Optional[str] = None
    popularity: Optional[float] = None
    biography: Optional[str] = None
    # douban
    roles: Optional[list[str]] = Field(default_factory=list)
    title: Optional[str] = None
    url: Optional[str] = None
    avatar: Optional[Union[str, MediaImageSet]] = None
    latin_name: Optional[str] = None
    # bangumi
    career: Optional[list[str]] = Field(default_factory=list)
    relation: Optional[str] = None


def _media_search_result_kind(value: Any) -> str:
    """按稳定字段区分音乐、人物与影视/合集搜索结果。"""
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, dict):
        if value.get("type") == "音乐" or "music_type" in value:
            return "music"
        if "source" in value and "media_source" not in value:
            return "person"
    return "media"


MediaSearchResult = Annotated[
    Union[
        Annotated[MusicInfo, Tag("music")],
        Annotated[MediaPerson, Tag("person")],
        Annotated[MediaInfo, Tag("media")],
    ],
    Discriminator(_media_search_result_kind),
]


class MediaSearchResults(RootModel[List[MediaSearchResult]]):
    """媒体、音乐、合集与人物的统一搜索结果列表。"""
