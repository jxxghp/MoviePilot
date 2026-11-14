"""
资源识别、媒体详情和下载上下文的接口投影。
"""

from typing import Annotated, Any, Dict, List, Optional, Union

from pydantic import BaseModel, Discriminator, Field, PrivateAttr, RootModel, Tag, model_validator

from app.schemas.category import ClassificationFactValue, ClassificationResult
from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.music import MusicArtistInfo as _MusicArtistInfo
from app.schemas.music import MusicInfo, MusicMeta
from app.schemas.types import MediaSource
from app.schemas.types import MediaType as _MediaType


class MetaInfo(OptionalMediaIdentityMixin, BaseModel):
    """
    识别元数据。

    :ivar _season_list_override: 显式覆盖的季列表；为空时从解析结果推导
    """
    isfile: Optional[bool] = Field(default=False, description="是否处理的文件")
    org_string: Optional[str] = Field(default=None, description="原字符串")
    title: Optional[str] = Field(default=None, description="原标题")
    subtitle: Optional[str] = Field(default=None, description="副标题")
    type: Optional[str] = Field(default=None, description="类型 电影、电视剧、音乐")
    name: Optional[str] = Field(default=None, description="名称")
    cn_name: Optional[str] = Field(default=None, description="识别的中文名")
    en_name: Optional[str] = Field(default=None, description="识别的英文名")
    year: Optional[str] = Field(default=None, description="年份")
    total_season: Optional[int] = Field(default=0, description="总季数")
    begin_season: Optional[int] = Field(default=None, description="识别的开始季 数字")
    end_season: Optional[int] = Field(default=None, description="识别的结束季 数字")
    total_episode: Optional[int] = Field(default=0, description="总集数")
    begin_episode: Optional[int] = Field(default=None, description="识别的开始集")
    end_episode: Optional[int] = Field(default=None, description="识别的结束集")
    season_episode: Optional[str] = Field(default=None, description="季集信息的展示文本")
    episode_list: Optional[List[int]] = Field(default_factory=list, description="集列表")
    part: Optional[str] = Field(default=None, description="分段媒体的分段标识")
    resource_type: Optional[str] = Field(default=None, description="识别的资源类型")
    resource_effect: Optional[str] = Field(default=None, description="识别的效果")
    resource_pix: Optional[str] = Field(default=None, description="识别的分辨率")
    resource_team: Optional[str] = Field(default=None, description="识别的制作组/字幕组")
    video_encode: Optional[str] = Field(default=None, description="视频编码")
    audio_encode: Optional[str] = Field(default=None, description="音频编码")
    edition: Optional[str] = Field(default=None, description="资源类型")
    web_source: Optional[str] = Field(default=None, description="流媒体平台")
    apply_words: Optional[List[str]] = Field(default=None, description="应用的识别词信息")
    episode_group: Optional[str] = Field(default=None, description="剧集组")
    media_source: Optional[MediaSource] = Field(default=None, description="显式媒体数据源")
    media_id: Optional[str] = Field(default=None, description="显式媒体数据源原生ID")

    _season_list_override: Optional[List[int]] = PrivateAttr(default=None)

    @property
    def season_list(self) -> List[int]:
        """
        返回识别的季数字列表。
        """
        if self._season_list_override is not None:
            return self._season_list_override
        if self.begin_season is None:
            if self.type in (_MediaType.TV, _MediaType.TV.value):
                return [1]
            return []
        if self.end_season is not None:
            return list(range(self.begin_season, self.end_season + 1))
        return [self.begin_season]

    @season_list.setter
    def season_list(self, value: Optional[List[int]]) -> None:
        """
        兼容旧链路对季列表的显式覆盖。
        """
        self._season_list_override = list(value) if value is not None else None

    @property
    def season(self) -> str:
        """
        返回开始季、结束季字符串，确定是剧集没有季的返回S01。
        """
        if self.begin_season is not None:
            return "S%s" % str(self.begin_season).rjust(2, "0") \
                if self.end_season is None \
                else "S%s-S%s" % \
                     (str(self.begin_season).rjust(2, "0"),
                      str(self.end_season).rjust(2, "0"))
        if self.type in (_MediaType.TV, _MediaType.TV.value):
            return "S01"
        return ""

    @property
    def season_seq(self) -> str:
        """
        返回 begin_season 的数字，电视剧没有季的返回1。
        """
        if self.begin_season is not None:
            return str(self.begin_season)
        if self.type in (_MediaType.TV, _MediaType.TV.value):
            return "1"
        return ""

    @property
    def episode(self) -> str:
        """
        返回开始集、结束集字符串。
        """
        if self.begin_episode is not None:
            return "E%s" % str(self.begin_episode).rjust(2, "0") \
                if self.end_episode is None \
                else "E%s-E%s" % \
                     (
                         str(self.begin_episode).rjust(2, "0"),
                         str(self.end_episode).rjust(2, "0"))
        return ""


class MediaImageSet(BaseModel):
    """
    跨媒体源兼容的人物图片尺寸集合。
    """

    large: Optional[str] = Field(default=None, description="大尺寸图片")
    common: Optional[str] = Field(default=None, description="通用尺寸的图片")
    medium: Optional[str] = Field(default=None, description="中尺寸图片")
    normal: Optional[str] = Field(default=None, description="常规尺寸图片")
    small: Optional[str] = Field(default=None, description="小尺寸图片")
    grid: Optional[str] = Field(default=None, description="网格展示使用的图片")


class MediaCredit(BaseModel):
    """
    影视条目中的演职员摘要。
    """

    id: Optional[int | str] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    original_name: Optional[str] = Field(default=None, description="原名")
    character: Optional[str] = Field(default=None, description="饰演的角色名称")
    type: Optional[str | int] = Field(default=None, description="类型标识")
    gender: Optional[str | int] = Field(default=None, description="人物或艺人的性别信息")
    adult: Optional[bool] = Field(default=None, description="是否成人内容")
    known_for_department: Optional[str] = Field(default=None, description="人物主要从事的部门")
    profile_path: Optional[str] = Field(default=None, description="人物头像路径")
    credit_id: Optional[str] = Field(default=None, description="演职人员与作品之间的关联标识")
    cast_id: Optional[int] = Field(default=None, description="演员在演员表中的标识")
    order: Optional[int] = Field(default=None, description="展示顺序")
    department: Optional[str] = Field(default=None, description="演职人员所属部门")
    job: Optional[str] = Field(default=None, description="演职人员在作品中的具体职务")
    popularity: Optional[float] = Field(default=None, description="流行度")
    roles: list[str] = Field(default_factory=list, description="人物在作品中的角色列表")
    title: Optional[str] = Field(default=None, description="标题")
    url: Optional[str] = Field(default=None, description="访问地址")
    uri: Optional[str] = Field(default=None, description="条目链接")
    sharing_url: Optional[str] = Field(default=None, description="人物条目的分享地址")
    avatar: Optional[str | MediaImageSet] = Field(default=None, description="头像")
    images: Optional[MediaImageSet] = Field(default=None, description="人物不同展示尺寸的图片地址")
    latin_name: Optional[str] = Field(default=None, description="人物的拉丁字母名称")
    career: list[str] = Field(default_factory=list, description="人物的职业信息")
    relation: Optional[str] = Field(default=None, description="人物或艺人与当前条目的关系")
    user: Optional[JsonData] = Field(default=None, description="发起用户")


class MediaGenre(BaseModel):
    """
    影视风格摘要。
    """

    id: Optional[int | str] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="显示名称")


class MediaCompany(BaseModel):
    """
    电视网或制作公司的标准摘要。
    """

    id: Optional[int | str] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    logo_path: Optional[str] = Field(default=None, description="公司或电视网的标志图片路径")
    origin_country: Optional[str] = Field(default=None, description="原产国")


class MediaCountry(BaseModel):
    """
    影视制作国家或地区。
    """

    id: Optional[int | str] = Field(default=None, description="记录标识")
    iso_3166_1: Optional[str] = Field(default=None, description="国家或地区的 ISO 3166-1 代码")
    name: Optional[str] = Field(default=None, description="显示名称")


class MediaLanguage(BaseModel):
    """
    影视内容使用的语言。
    """

    english_name: Optional[str] = Field(default=None, description="语言的英文名称")
    iso_639_1: Optional[str] = Field(default=None, description="语言的 ISO 639-1 代码")
    name: Optional[str] = Field(default=None, description="显示名称")


class MediaReleaseDate(BaseModel):
    """
    电影在单个地区的一次发行记录。
    """

    date: str = Field(description="时间")
    iso_code: Optional[str] = Field(default=None, description="上映地区的标准代码")
    note: Optional[str] = Field(default=None, description="附加信息")
    type: Optional[int] = Field(default=None, description="类型标识")


class MediaEpisode(BaseModel):
    """
    电视剧即将播出的单集摘要。
    """

    id: Optional[int] = Field(default=None, description="记录标识")
    air_date: Optional[str] = Field(default=None, description="首次播出日期")
    episode_number: Optional[int] = Field(default=None, description="集号")
    episode_type: Optional[str] = Field(default=None, description="单集类型")
    name: Optional[str] = Field(default=None, description="显示名称")
    overview: Optional[str] = Field(default=None, description="简介")
    production_code: Optional[str] = Field(default=None, description="单集制作编号")
    runtime: Optional[int] = Field(default=None, description="单集时长，单位为分钟")
    season_number: Optional[int] = Field(default=None, description="季号")
    show_id: Optional[int] = Field(default=None, description="单集所属剧集的标识")
    still_path: Optional[str] = Field(default=None, description="单集剧照路径")
    vote_average: Optional[float] = Field(default=None, description="评分")
    vote_count: Optional[int] = Field(default=None, description="评分投票人数")


class MediaSeason(BaseModel):
    """
    标准季信息以及剧集组季信息。
    """

    id: Optional[int | str] = Field(default=None, description="记录标识")
    air_date: Optional[str] = Field(default=None, description="首次播出日期")
    episode_count: Optional[int] = Field(default=None, description="集数量")
    name: Optional[str] = Field(default=None, description="显示名称")
    overview: Optional[str] = Field(default=None, description="简介")
    poster_path: Optional[str] = Field(default=None, description="海报地址")
    season_number: Optional[int] = Field(default=None, description="季号")
    vote_average: Optional[float] = Field(default=None, description="评分")
    order: Optional[int] = Field(default=None, description="展示顺序")
    locked: Optional[bool] = Field(default=None, description="季信息是否被锁定")
    episodes: list[MediaEpisode] = Field(default_factory=list, description="本季的单集详情列表")


class MediaEpisodeGroupNetwork(BaseModel):
    """
    TMDB 剧集组所属电视网信息。
    """

    id: Optional[int] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    logo_path: Optional[str] = Field(default=None, description="标题 LOGO")
    origin_country: Optional[str] = Field(default=None, description="原产国")


class MediaEpisodeGroup(BaseModel):
    """
    TMDB 电视剧的剧集分组摘要。
    """

    description: str = Field(default="", description="描述")
    episode_count: int = Field(default=0, description="集数量")
    group_count: int = Field(default=0, description="剧集组数量")
    id: str = Field(description="记录标识")
    name: str = Field(description="显示名称")
    network: Optional[MediaEpisodeGroupNetwork] = Field(default=None, description="播出平台信息")
    type: int = Field(description="类型标识")


class MediaInfo(OptionalMediaIdentityMixin, BaseModel):
    """
    识别媒体信息。
    """
    media_source: Optional[MediaSource] = Field(default=None, description="媒体主身份来源")
    scrape_source: Optional[str] = Field(default=None, description="请求级刮削来源")
    type: Optional[str] = Field(default=None, description="类型 电影、电视剧、合集")
    title: Optional[str] = Field(default=None, description="媒体标题")
    en_title: Optional[str] = Field(default=None, description="英文标题")
    hk_title: Optional[str] = Field(default=None, description="香港、台湾、新加坡地区标题")
    tw_title: Optional[str] = Field(default=None, description="台湾地区标题")
    sg_title: Optional[str] = Field(default=None, description="新加坡地区标题")
    year: Optional[str] = Field(default=None, description="年份")
    title_year: Optional[str] = Field(default=None, description="标题（年份）")
    season: Optional[int] = Field(default=None, description="当前指定季，如有")
    tmdb_id: Optional[int] = Field(default=None, description="数据源返回的辅助 ID，仅作为元数据输出")
    imdb_id: Optional[str] = Field(default=None, description="IMDb 条目标识")
    tvdb_id: Optional[int] = Field(default=None, description="TVDB 条目标识")
    tvdb_slug: Optional[str] = Field(default=None, description="TVDB 条目的地址标识")
    douban_id: Optional[str] = Field(default=None, description="豆瓣 ID")
    bangumi_id: Optional[int] = Field(default=None, description="Bangumi 条目标识")
    anilist_id: Optional[int] = Field(default=None, description="AniList 条目标识")
    anidb_id: Optional[int] = Field(default=None, description="AniDB 条目标识")
    collection_id: Optional[int] = Field(default=None, description="合集ID")
    media_id: Optional[str] = Field(default=None, description="当前来源原生 ID")
    original_language: Optional[str] = Field(default=None, description="媒体原语种")
    original_title: Optional[str] = Field(default=None, description="媒体原发行标题")
    release_date: Optional[str] = Field(default=None, description="媒体发行日期")
    backdrop_path: Optional[str] = Field(default=None, description="背景图片")
    poster_path: Optional[str] = Field(default=None, description="海报图片")
    logo_path: Optional[str] = Field(default=None, description="标题 LOGO")
    vote_average: Optional[float] = Field(default=0.0, description="评分")
    overview: Optional[str] = Field(default=None, description="描述")
    library_category: Optional[str] = Field(default="", description="媒体库目录分类；category 是过渡期兼容字段")
    metadata_category: Optional[str] = Field(default="", description="来源提供的描述性分类，不参与目录选择")
    classification: Optional[ClassificationResult] = Field(default=None, description="本次分类使用的推荐、生效结果和策略快照")
    classification_facts: Dict[str, ClassificationFactValue] = Field(
        default_factory=dict,
        description="插件来源提交的受控扩展分类事实，键使用完整 extensions.<source>.* 字段 ID",
    )
    category: Optional[str] = Field(default="", description="二级分类兼容字段，始终映射到 library_category")
    seasons: Optional[Dict[int, list[int]]] = Field(default_factory=dict, description="季季集清单")
    season_info: Optional[List[MediaSeason]] = Field(default_factory=list, description="季详情")
    season_years: Optional[Dict[int, str]] = Field(default_factory=dict, description="各季首播年份")
    names: Optional[list[str]] = Field(default_factory=list, description="别名和译名")
    actors: Optional[list[Union[MediaCredit, str]]] = Field(default_factory=list, description="演员")
    directors: Optional[list[Union[MediaCredit, str]]] = Field(default_factory=list, description="导演")
    detail_link: Optional[str] = Field(default=None, description="详情链接")
    # 其它TMDB属性
    adult: Optional[bool] = Field(default=False, description="是否成人内容")
    created_by: Optional[list[MediaCredit]] = Field(default_factory=list, description="创建人")
    episode_run_time: Optional[list[int]] = Field(default_factory=list, description="来源提供的单集时长列表，单位为分钟；省略时为空列表，也允许显式传入 None")
    genres: Optional[List[MediaGenre]] = Field(default_factory=list, description="风格")
    first_air_date: Optional[str] = Field(default=None, description="首播日期")
    homepage: Optional[str] = Field(default=None, description="首页")
    languages: Optional[list[str]] = Field(default_factory=list, description="语种")
    last_air_date: Optional[str] = Field(default=None, description="最后上映日期")
    networks: Optional[list[MediaCompany]] = Field(default_factory=list, description="流媒体平台")
    number_of_episodes: Optional[int] = Field(default=0, description="集数")
    number_of_seasons: Optional[int] = Field(default=0, description="季数")
    origin_country: Optional[list[str]] = Field(default_factory=list, description="原产国")
    original_name: Optional[str] = Field(default=None, description="原名")
    production_companies: Optional[list[MediaCompany]] = Field(default_factory=list, description="出品公司")
    production_countries: Optional[list[MediaCountry]] = Field(default_factory=list, description="出品国")
    spoken_languages: Optional[list[MediaLanguage]] = Field(default_factory=list, description="语种")
    release_dates: list[MediaReleaseDate] = Field(default_factory=list, description="所有发行日期")
    status: Optional[str] = Field(default=None, description="来源提供的上映或播出状态，如已上映、连载中或已完结；未提供时为 None")
    tagline: Optional[str] = Field(default=None, description="标签")
    genre_ids: Optional[list[int | str]] = Field(default_factory=list, description="风格ID")
    vote_count: Optional[int] = Field(default=0, description="评价数量")
    popularity: Optional[float] = Field(default=0.0, description="流行度")
    runtime: Optional[int] = Field(default=None, description="媒体时长，单位为分钟")
    next_episode_to_air: Optional[MediaEpisode] = Field(default=None, description="下一集")
    content_rating: Optional[str] = Field(default=None, description="内容分级")
    episode_groups: Optional[list[MediaEpisodeGroup | MediaSeason]] = Field(default_factory=list, description="全部剧集组")
    episode_group: Optional[str] = Field(default=None, description="剧集组")
    tmdb_info: Optional[dict[str, JsonData]] = Field(
        default=None,
        description="各数据源原始信息；Core MediaInfo.to_dict() 保留这些键供兼容调用方使用",
    )
    douban_info: Optional[dict[str, JsonData]] = Field(default=None, description="豆瓣返回的媒体详情")
    bangumi_info: Optional[dict[str, JsonData]] = Field(default=None, description="Bangumi 返回的媒体详情")
    anilist_info: Optional[dict[str, JsonData]] = Field(default=None, description="AniList 返回的媒体详情")

    @model_validator(mode="before")
    @classmethod
    def _normalize_category_compatibility(cls, value: Any) -> Any:
        """
        把旧影视 category 输入归一为媒体库分类，并保持兼容字段双写。
        """
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
    搜索种子信息。
    """
    site: Optional[int] = Field(default=None, description="站点ID")
    site_name: Optional[str] = Field(default=None, description="站点名称")
    site_cookie: Optional[str] = Field(default=None, description="站点Cookie")
    site_ua: Optional[str] = Field(default=None, description="站点UA")
    site_proxy: Optional[bool] = Field(default=False, description="站点是否使用代理")
    site_order: Optional[int] = Field(default=0, description="站点优先级")
    site_downloader: Optional[str] = Field(default=None, description="站点下载器")
    title: Optional[str] = Field(default=None, description="种子名称")
    description: Optional[str] = Field(default=None, description="种子副标题")
    media_source: Optional[MediaSource] = Field(default=None, description="种子页面声明的媒体身份")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    enclosure: Optional[str] = Field(default=None, description="种子链接")
    page_url: Optional[str] = Field(default=None, description="详情页面")
    size: Optional[float] = Field(default=0.0, description="种子包含的文件总大小，单位为字节")
    seeders: Optional[int] = Field(default=0, description="做种者")
    peers: Optional[int] = Field(default=0, description="下载者")
    grabs: Optional[int] = Field(default=0, description="完成者")
    pubdate: Optional[str] = Field(default=None, description="发布时间")
    date_elapsed: Optional[str] = Field(default=None, description="已过时间")
    freedate: Optional[str] = Field(default=None, description="免费截止时间")
    uploadvolumefactor: Optional[float] = Field(default=None, description="上传统计倍率，例如 2 表示按实际上传量的两倍计入站点统计")
    downloadvolumefactor: Optional[float] = Field(default=None, description="下载统计倍率，例如 0 表示免费下载、0.5 表示按一半下载量计入统计")
    hit_and_run: Optional[bool] = Field(default=False, description="种子是否存在下载后做种义务")
    labels: Optional[list[str]] = Field(default_factory=list, description="种子标签")
    pri_order: Optional[int] = Field(default=0, description="种子优先级")
    category: Optional[str] = Field(default=None, description="种子分类 电影/电视剧/音乐")
    volume_factor: Optional[str] = Field(default=None, description="促销")
    freedate_diff: Optional[str] = Field(default=None, description="剩余免费时间")


class SubtitleInfo(BaseModel):
    """
    搜索字幕信息。
    """
    site: Optional[int] = Field(default=None, description="站点ID")
    site_name: Optional[str] = Field(default=None, description="站点名称")
    site_cookie: Optional[str] = Field(default=None, description="站点Cookie")
    site_ua: Optional[str] = Field(default=None, description="站点UA")
    site_proxy: Optional[bool] = Field(default=False, description="站点是否使用代理")
    site_order: Optional[int] = Field(default=0, description="站点优先级")
    title: Optional[str] = Field(default=None, description="字幕标题")
    description: Optional[str] = Field(default=None, description="字幕描述")
    enclosure: Optional[str] = Field(default=None, description="字幕下载链接")
    page_url: Optional[str] = Field(default=None, description="详情页面")
    language: Optional[str] = Field(default=None, description="语言")
    language_icon: Optional[str] = Field(default=None, description="语言图标")
    size: Optional[float] = Field(default=0.0, description="字幕文件大小，单位为字节")
    pubdate: Optional[str] = Field(default=None, description="发布时间")
    date_elapsed: Optional[str] = Field(default=None, description="已过时间")
    grabs: Optional[int] = Field(default=0, description="点击/下载次数")
    uploader: Optional[str] = Field(default=None, description="上传者")
    report_url: Optional[str] = Field(default=None, description="举报页面")
    torrent_id: Optional[str] = Field(default=None, description="种子ID")
    subtitle_id: Optional[str] = Field(default=None, description="字幕ID")
    file_name: Optional[str] = Field(default=None, description="下载文件名")
    meta_info: Optional[MetaInfo] = Field(default=None, description="识别元数据")
    season_episode: Optional[str] = Field(default=None, description="季集信息的展示文本")
    episode_list: Optional[List[int]] = Field(default_factory=list, description="集列表")


class Context(BaseModel):
    """
    上下文。
    """
    meta_info: Optional[Union[MusicMeta, MetaInfo]] = Field(default=None, description="元数据")
    media_info: Optional[Union[MusicInfo, MediaInfo]] = Field(default=None, description="媒体信息")
    torrent_info: Optional[TorrentInfo] = Field(default=None, description="种子信息")
    resource_source: Optional[str] = Field(default="unknown", description="候选资源来源：rss、spider、search、unknown")
    match_source: Optional[str] = Field(default="unknown", description="候选匹配来源：MediaSource 枚举值、title、unknown")
    candidate_recognized: Optional[bool] = Field(default=False, description="候选自身是否已经识别出有效媒体 ID")
    media_info_is_target: Optional[bool] = Field(default=False, description="当前 media_info 是否为目标媒体回填")
    match_status: Optional[str] = Field(default=None, description="资源匹配状态")
    match_reason: Optional[str] = Field(default=None, description="资源匹配或未匹配的原因")
    confirmed_full_coverage: Optional[bool] = Field(default=False, description="下载层确认候选资源覆盖完整目标范围，供订阅事实写入判断整包资源")
    subscription_download: Optional[Dict[str, JsonData]] = Field(default=None, description="本次订阅提交类型、全集洗版模式及新增和升级集数；旧记录或非订阅下载为空")
    music_track_keys: Optional[List[str]] = Field(default=None, description="下载层从种子文件清单提取的稳定音轨键，供专辑订阅跨轮次累计下载进度")


class MediaPerson(BaseModel):
    """
    媒体人物信息。
    """
    source: Optional[str] = Field(default=None, description="来源：themoviedb、douban、bangumi、anilist")
    id: Optional[int | str] = Field(default=None, description="公共")
    type: Optional[Union[str, int]] = Field(default=1, description="类型标识")
    name: Optional[str] = Field(default=None, description="显示名称")
    character: Optional[str] = Field(default=None, description="饰演的角色名称")
    images: Optional[MediaImageSet] = Field(default=None, description="人物不同展示尺寸的图片地址")
    profile_path: Optional[str] = Field(default=None, description="人物头像路径")
    gender: Optional[Union[str, int]] = Field(default=None, description="人物或艺人的性别信息")
    original_name: Optional[str] = Field(default=None, description="原名")
    credit_id: Optional[str] = Field(default=None, description="演职人员与作品之间的关联标识")
    also_known_as: Optional[list[str]] = Field(default_factory=list, description="人物的其他名称")
    birthday: Optional[str] = Field(default=None, description="人物出生日期")
    deathday: Optional[str] = Field(default=None, description="人物逝世日期")
    imdb_id: Optional[str] = Field(default=None, description="IMDb 条目标识")
    known_for_department: Optional[str] = Field(default=None, description="人物主要从事的部门")
    place_of_birth: Optional[str] = Field(default=None, description="人物出生地点")
    popularity: Optional[float] = Field(default=None, description="流行度")
    biography: Optional[str] = Field(default=None, description="人物生平介绍")
    roles: Optional[list[str]] = Field(default_factory=list, description="人物在作品中的角色列表")
    title: Optional[str] = Field(default=None, description="标题")
    url: Optional[str] = Field(default=None, description="访问地址")
    avatar: Optional[Union[str, MediaImageSet]] = Field(default=None, description="头像")
    latin_name: Optional[str] = Field(default=None, description="人物的拉丁字母名称")
    career: Optional[list[str]] = Field(default_factory=list, description="人物的职业信息")
    relation: Optional[str] = Field(default=None, description="人物或艺人与当前条目的关系")


def _media_result_kind(value: Any) -> str:
    """
    按稳定字段区分音乐、人物与影视/合集结果。
    """
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, dict):
        if value.get("type") == "音乐" or "music_type" in value:
            return "music"
        if "source" in value and "media_source" not in value:
            return "person"
    return "media"


def _media_search_result_kind(value: Any) -> str:
    """
    按搜索结果的稳定字段区分音乐艺术家、音乐、影视人物与媒体。
    """
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, dict):
        # 旧音乐搜索仍可能以 MusicInfo(title) 表示艺术家；canonical MusicArtistInfo
        # 带有 name 字段，只有这种结果进入人物/艺术家响应分支。
        if value.get("music_type") == "artist" and "name" in value:
            return "music_artist"
        if value.get("type") == "音乐" or "music_type" in value:
            return "music"
        if "source" in value and "media_source" not in value:
            return "person"
    return "media"


MediaDetailResult = Annotated[
    Union[
        Annotated[MusicInfo, Tag("music")],
        Annotated[MediaInfo, Tag("media")],
    ],
    Discriminator(_media_result_kind),
]


MediaSearchResult = Annotated[
    Union[
        Annotated[_MusicArtistInfo, Tag("music_artist")],
        Annotated[MusicInfo, Tag("music")],
        Annotated[MediaPerson, Tag("person")],
        Annotated[MediaInfo, Tag("media")],
    ],
    Discriminator(_media_search_result_kind),
]


class MediaSearchResults(RootModel[List[MediaSearchResult]]):
    """
    媒体、音乐、合集、影视人物与音乐艺术家的统一搜索结果列表。
    """
