"""
媒体服务器播放、媒体条目和回调事件的数据结构。
"""

from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union
from typing import TypedDict as _TypedDict

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from app.schemas.common import JsonData
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MediaSource, MediaType


class MediaServerField(str, Enum):
    """
    各媒体服务器共用的可选查询内容组。

    :cvar LIBRARY: 条目所属媒体库或父目录
    :cvar ORIGINAL_TITLE: 原始语言标题
    :cvar YEAR: 年份及展示所需的日期
    :cvar IDENTITY: 成对的媒体来源和来源标识
    :cvar PATH: 媒体文件、目录及版本路径
    :cvar MUSIC: 音乐艺术家、专辑及匹配所需的附加信息
    :cvar USER_STATE: 播放状态、进度、次数及时间
    :cvar IMAGES: 图片元数据、链接及附加图片查询
    :cvar COUNTS: 媒体库数量及非业务必需的总量查询
    :cvar LINK: 播放页或媒体库跳转链接
    """

    LIBRARY = "library"
    ORIGINAL_TITLE = "original_title"
    YEAR = "year"
    IDENTITY = "identity"
    PATH = "path"
    MUSIC = "music"
    USER_STATE = "user_state"
    IMAGES = "images"
    COUNTS = "counts"
    LINK = "link"


class MediaServerQueryOptions(BaseModel):
    """
    不依赖服务端协议的媒体查询投影，未指定内容组时保留原行为。

    基础标识、类型、名称和完成当前操作所必需的字段始终保留；
    服务适配器只对有协议证据的能力实施远端裁剪。
    """

    fields: Optional[frozenset[MediaServerField]] = Field(
        default=None,
        description="所需内容组；None 保留原有内容，空集合仅取操作必需的基础信息",
    )

    model_config = ConfigDict(frozen=True, extra="forbid")

    @staticmethod
    def as_kwargs(query: Optional["MediaServerQueryOptions"]) -> "_MediaServerQueryKwargs":
        """
        仅在指定投影时转发 query，保持旧客户端和插件未传该参数的调用约定。

        :param query: 可选的媒体查询投影
        :return: 有类型约束的可选关键字参数
        """
        return {"query": query} if query is not None else {}

    def includes(self, field: MediaServerField) -> bool:
        """
        判断调用方是否需要某个内容组。

        :param field: 待检查的统一内容组
        :return: 未指定投影或显式选中该组时为 True
        """
        return self.fields is None or field in self.fields

    def with_fields(self, *fields: MediaServerField) -> "MediaServerQueryOptions":
        """
        为当前操作补充完成身份校验等所必需的内容，不修改原选项。

        :param fields: 需要补充的内容组
        :return: 保留原默认行为或包含补充内容的新选项
        """
        if self.fields is None:
            return self
        return MediaServerQueryOptions(fields=self.fields.union(fields))


class _MediaServerQueryKwargs(_TypedDict, total=False):
    """
    媒体客户端调用中可省略的投影参数。

    :param query: 仅在调用方明确指定时传入的查询投影
    """

    query: MediaServerQueryOptions


class ExistMediaInfo(BaseModel):
    """
    媒体服务器存在媒体信息。
    """
    type: Optional[MediaType] = Field(default=None, description="类型 电影、电视剧、音乐")
    seasons: Optional[Dict[int, List[int]]] = Field(default_factory=dict, description="季")
    server_type: Optional[str] = Field(
        default=None,
        description="媒体服务器类型：plex、jellyfin、emby、zspace、trimemedia、ugreen、navidrome",
    )
    server: Optional[str] = Field(default=None, description="媒体服务器名称")
    itemid: Optional[Union[str, int]] = Field(default=None, description="媒体ID")


class MediaServerPlayData(BaseModel):
    """
    媒体服务器在线播放地址。
    """

    url: str = Field(description="播放地址")
    item_id: Optional[str] = Field(default=None, description="媒体项目 ID")
    server_id: Optional[str] = Field(default=None, description="媒体服务器 ID")


class MediaServerExistsData(BaseModel):
    """
    本地媒体存在性查询结果。
    """

    item: Dict[str, str] = Field(
        default_factory=dict,
        description="命中的媒体项目：id 为条目标识，server 为其所属媒体服务器配置名称（旧缓存缺失时省略）；未命中时为空",
    )


class MediaServerExistingEpisodes(RootModel[Dict[int, List[int]]]):
    """
    媒体服务器中按季号归组的已存在集号。
    """


class NotExistMediaInfo(BaseModel):
    """
    媒体服务器不存在媒体信息。
    """
    season: Optional[int] = Field(default=None, description="季")
    episodes: Optional[List[int]] = Field(default_factory=list, description="剧集列表")
    total_episode: Optional[int] = Field(default=0, description="总集数")
    start_episode: Optional[int] = Field(default=0, description="开始集")
    require_complete_coverage: Optional[bool] = Field(default=False, description="候选资源须完整覆盖目标范围")


class RefreshMediaItem(BaseModel):
    """
    媒体库刷新信息。
    """
    title: Optional[str] = Field(default=None, description="标题")
    year: Optional[Union[str, int]] = Field(default=None, description="年份")
    type: Optional[MediaType] = Field(default=None, description="类型")
    category: Optional[str] = Field(default=None, description="类别")
    target_path: Optional[Path] = Field(default=None, description="目录")


class MediaServerLibrary(BaseModel):
    """
    媒体服务器媒体库信息。
    """
    server: Optional[str] = Field(default=None, description="服务器")
    id: Optional[Union[str, int]] = Field(default=None, description="记录标识")
    item_id: Optional[Union[str, int]] = Field(default=None, description="媒体服务器项目ID")
    server_id: Optional[str] = Field(default=None, description="媒体服务器ID")
    server_type: Optional[str] = Field(default=None, description="媒体服务器类型")
    name: Optional[str] = Field(default=None, description="名称")
    path: Optional[Union[str, List[str]]] = Field(default=None, description="路径")
    type: Optional[str] = Field(default=None, description="类型")
    item_count: Optional[int] = Field(default=None, description="媒体库内媒体数量")
    image: Optional[str] = Field(default=None, description="封面图")
    image_list: Optional[List[str]] = Field(default=None, description="封面图列表")
    link: Optional[str] = Field(default=None, description="跳转链接")
    use_cookies: Optional[bool] = Field(default=None, description="飞牛的图片需要Cookies")


class MediaServerItemUserState(BaseModel):
    """
    媒体服务器条目的用户播放状态。
    """

    played: Optional[bool] = Field(default=None, description="已播放")
    resume: Optional[bool] = Field(default=None, description="继续播放")
    last_played_date: Optional[str] = Field(default=None, description="上次播放时间 10位时间戳")
    play_count: Optional[int] = Field(default=None, description="播放次数(不等于完播次数，理解为浏览次数)")
    percentage: Optional[float] = Field(default=None, description="播放进度百分比，通常为 0 至 100；未提供时为 None")


class MediaServerItem(OptionalMediaIdentityMixin, BaseModel):
    """
    媒体服务器媒体信息。
    """
    id: Optional[Union[str, int]] = Field(default=None, description="记录标识")
    server: Optional[str] = Field(default=None, description="服务器")
    library: Optional[Union[str, int]] = Field(default=None, description="媒体库ID")
    server_id: Optional[str] = Field(default=None, description="媒体服务器ID")
    server_type: Optional[str] = Field(default=None, description="媒体服务器类型")
    item_id: Optional[str] = Field(default=None, description="媒体项目 ID")
    item_type: Optional[str] = Field(default=None, description="类型")
    title: Optional[str] = Field(default=None, description="标题")
    original_title: Optional[str] = Field(default=None, description="原标题")
    year: Optional[Union[str, int]] = Field(default=None, description="年份")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源与原生ID")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    path: Optional[str] = Field(default=None, description="路径")
    seasoninfo: Optional[Dict[int, List[int]]] = Field(default=None, description="季集")
    note: Optional[JsonData] = Field(default=None, description="备注")
    lst_mod_date: Optional[str] = Field(default=None, description="同步时间")
    user_state: Optional[MediaServerItemUserState] = Field(default=None, description="当前用户在媒体服务器中的条目状态")

    model_config = ConfigDict(from_attributes=True)


class MediaServerSeasonInfo(BaseModel):
    """
    媒体服务器媒体剧集信息。
    """
    season: Optional[int] = Field(default=None, description="季号")
    episodes: Optional[List[int]] = Field(default_factory=list, description="剧集信息")


class MediaServerSubtitleTrack(BaseModel):
    """
    声明可通过本服务流式读取的同名外挂字幕。
    """

    subtitle_id: str = Field(description="字幕文件名标识，不包含服务端目录路径")
    title: str = Field(description="播放器显示名称")
    language: Optional[str] = Field(default=None, description="从文件名识别的语言标识")
    url: str = Field(description="当前 MoviePilot 实例的字幕流地址")


class MediaServerStreamInfo(BaseModel):
    """
    声明原生播放器使用的当前实例媒体流地址和外挂字幕。
    """

    item_id: str = Field(description="已解析的媒体服务器条目标识")
    server: Optional[str] = Field(default=None, description="媒体服务器配置名称；单文件票据省略")
    title: str = Field(description="媒体标题")
    content_url: str = Field(description="当前 MoviePilot 实例的媒体流地址")
    strm_url: Optional[str] = Field(
        default=None,
        description="STRM 条目解析出的无内嵌凭据 HTTP(S) 客户端直链地址；普通文件或需代理鉴权流时为 None",
    )
    media_type: Literal["video", "audio"] = Field(default="video", description="播放器使用的媒体类型")
    artist: Optional[str] = Field(default=None, description="音乐表演者")
    album: Optional[str] = Field(default=None, description="音乐专辑")
    subtitles: List[MediaServerSubtitleTrack] = Field(default_factory=list, description="同目录或媒体服务器提供的外挂字幕")
    lyrics_url: Optional[str] = Field(default=None, description="当前实例的同名 LRC 歌词流地址")
    media_source_id: Optional[str] = Field(default=None, description="本次播放绑定的媒体源标识")
    resume_position_ms: Optional[int] = Field(default=None, description="服务端记录的断点续播位置（毫秒）")
    duration_ms: Optional[int] = Field(default=None, description="媒体总时长（毫秒）")


class MediaServerMediaStream(BaseModel):
    """
    媒体服务器单个媒体源中的视频、音频或字幕轨道信息。
    """

    index: Optional[int] = Field(default=None, description="服务端轨道序号")
    type: Literal["video", "audio", "subtitle", "unknown"] = Field(default="unknown", description="轨道类型")
    codec: Optional[str] = Field(default=None, description="轨道编解码格式")
    language: Optional[str] = Field(default=None, description="轨道语言标识")
    title: Optional[str] = Field(default=None, description="原始轨道标题")
    display_title: Optional[str] = Field(default=None, description="适合界面展示的轨道名称")
    is_default: bool = Field(default=False, description="是否为默认轨道")
    is_forced: bool = Field(default=False, description="是否为强制字幕轨道")
    is_external: bool = Field(default=False, description="是否为独立外挂字幕轨道")
    subtitle_id: Optional[str] = Field(default=None, description="可通过本实例字幕代理下载的字幕标识")
    channels: Optional[int] = Field(default=None, description="音频声道数")
    channel_layout: Optional[str] = Field(default=None, description="音频声道布局")
    bitrate: Optional[int] = Field(default=None, description="轨道码率（bps）")
    width: Optional[int] = Field(default=None, description="视频宽度（像素）")
    height: Optional[int] = Field(default=None, description="视频高度（像素）")
    video_range: Optional[str] = Field(default=None, description="视频动态范围，例如 SDR、HDR")


class MediaServerMediaSource(BaseModel):
    """
    媒体服务器可播放条目的单个文件版本或媒体源信息。
    """

    id: str = Field(description="媒体源标识")
    name: Optional[str] = Field(default=None, description="版本展示名称")
    container: Optional[str] = Field(default=None, description="封装容器格式")
    size: Optional[int] = Field(default=None, description="文件字节大小")
    bitrate: Optional[int] = Field(default=None, description="整体码率（bps）")
    duration_ms: Optional[int] = Field(default=None, description="媒体源时长（毫秒）")
    direct_url: Optional[str] = Field(
        default=None,
        description="媒体源为远端 HTTP(S) STRM 时的无凭据直链地址；本地文件源为 None",
    )
    streams: List[MediaServerMediaStream] = Field(default_factory=list, description="包含的视频、音频与字幕轨道")


class MediaServerPerson(BaseModel):
    """
    媒体服务器条目关联的演职员信息。
    """

    id: Optional[str] = Field(default=None, description="演职员在媒体服务器中的标识")
    name: str = Field(description="演职员姓名")
    role: Optional[str] = Field(default=None, description="饰演角色或职务说明")
    type: Optional[str] = Field(default=None, description="人员类型，例如 Actor、Director")
    image: Optional[str] = Field(default=None, description="已签名的头像图片地址")


class MediaServerBrowseItem(BaseModel):
    """
    媒体服务器浏览列表、季列表与分集列表共用的轻量媒体卡片模型。
    """

    item_id: str = Field(description="媒体服务器条目标识")
    server: Optional[str] = Field(default=None, description="所属媒体服务器配置名称")
    server_type: Optional[str] = Field(default=None, description="媒体服务器类型")
    item_type: str = Field(default="Movie", description="条目维度：Movie、Series、Season、Episode、MusicAlbum、Audio、Folder")
    media_type: Optional[str] = Field(default=None, description="统一业务媒体类型：电影、电视剧、音乐")
    title: str = Field(description="展示标题")
    original_title: Optional[str] = Field(default=None, description="原始语言标题")
    subtitle: Optional[str] = Field(default=None, description="副标题或补充信息")
    overview: Optional[str] = Field(default=None, description="剧情或条目简介")
    year: Optional[Union[str, int]] = Field(default=None, description="发行或首播年份")
    premiere_date: Optional[str] = Field(default=None, description="首映或首播日期")
    rating: Optional[float] = Field(default=None, description="社区评分（0 至 10）")
    duration_ms: Optional[int] = Field(default=None, description="时长（毫秒）")
    poster: Optional[str] = Field(default=None, description="已签名的海报主图地址")
    backdrop: Optional[str] = Field(default=None, description="已签名的背景横图地址")
    thumb: Optional[str] = Field(default=None, description="已签名的剧照或缩略图地址")
    series_id: Optional[str] = Field(default=None, description="所属剧集条目标识")
    series_name: Optional[str] = Field(default=None, description="所属剧集标题")
    season_id: Optional[str] = Field(default=None, description="所属季条目标识")
    season_number: Optional[int] = Field(default=None, description="季号")
    episode_number: Optional[int] = Field(default=None, description="集号")
    child_count: Optional[int] = Field(default=None, description="子条目数量，例如季数或集数")
    played: Optional[bool] = Field(default=None, description="当前用户是否已观看完成")
    played_percentage: Optional[float] = Field(default=None, description="观看进度百分比（0 至 100）")
    resume_position_ms: Optional[int] = Field(default=None, description="续播位置（毫秒）")
    use_cookies: Optional[bool] = Field(default=None, description="图片代理访问是否需要携带服务端 Cookie")


class MediaServerBrowsePage(BaseModel):
    """
    媒体服务器媒体库分页浏览结果。
    """

    items: List[MediaServerBrowseItem] = Field(default_factory=list, description="当前页媒体条目")
    total_count: int = Field(default=0, description="符合筛选条件的总条目数")
    start_index: int = Field(default=0, description="当前页零基起始偏移")
    limit: int = Field(default=30, description="当前页请求上限")
    has_more: bool = Field(default=False, description="是否存在下一页数据")


class MediaServerFilterOptions(BaseModel):
    """
    媒体服务器指定媒体库可用的筛选与排序选项。
    """

    genres: List[str] = Field(default_factory=list, description="可用风格/类型标签列表")
    years: List[int] = Field(default_factory=list, description="可用年份列表，按降序排列")
    item_types: List[str] = Field(default_factory=list, description="可用媒体维度选项")
    sort_options: List[str] = Field(default_factory=list, description="可用排序键列表")


class MediaServerItemDetail(MediaServerBrowseItem):
    """
    媒体服务器单个条目（电影、剧集、季、集、音乐）的完整详情。
    """

    tagline: Optional[str] = Field(default=None, description="标语或副宣传语")
    official_rating: Optional[str] = Field(default=None, description="内容分级，例如 PG-13、TV-MA")
    genres: List[str] = Field(default_factory=list, description="风格与分类标签")
    studios: List[str] = Field(default_factory=list, description="制作公司或发行商")
    people: List[MediaServerPerson] = Field(default_factory=list, description="主要演职员列表")
    media_source: Optional[MediaSource] = Field(default=None, description="关联外部元数据源")
    media_id: Optional[str] = Field(default=None, description="关联外部元数据源标识")
    imdb_id: Optional[str] = Field(default=None, description="IMDb 编号")
    tmdb_id: Optional[str] = Field(default=None, description="TMDB 编号")
    tvdb_id: Optional[str] = Field(default=None, description="TVDB 编号")
    douban_id: Optional[str] = Field(default=None, description="豆瓣编号")
    parent_id: Optional[str] = Field(default=None, description="上级目录或父条目标识")
    season_name: Optional[str] = Field(default=None, description="所属季名称")
    season_count: Optional[int] = Field(default=None, description="剧集总季数")
    episode_count: Optional[int] = Field(default=None, description="剧集或当前季总集数")
    media_sources: List[MediaServerMediaSource] = Field(default_factory=list, description="可播放的多版本及音轨字幕轨道列表")
    play_url: Optional[str] = Field(default=None, description="媒体服务器网页跳转地址")
    can_play: bool = Field(default=False, description="当前条目是否支持直接发起播放")


class MediaServerProgressReportRequest(BaseModel):
    """
    客户端回传原生播放器播放进度的请求体。
    """

    item_id: str = Field(description="正在播放的媒体服务器条目标识")
    server: str = Field(description="所属媒体服务器配置名称")
    media_source_id: Optional[str] = Field(default=None, description="当前播放使用的媒体源标识")
    position_ms: int = Field(default=0, ge=0, description="当前播放位置（毫秒）")
    duration_ms: Optional[int] = Field(default=None, ge=0, description="媒体总时长（毫秒）")
    is_paused: bool = Field(default=False, description="当前是否处于暂停状态")
    state: Literal["playing", "paused", "stopped"] = Field(default="playing", description="回传事件状态")


class MediaServerMusicStreamRequest(BaseModel):
    """
    用标准音乐身份和元数据查找媒体库中的单曲条目。
    """

    media_source: MediaSource = Field(description="音乐元数据来源")
    media_id: str = Field(description="单曲录音的来源 ID")
    title: str = Field(description="单曲标题")
    artists: List[str] = Field(default_factory=list, description="单曲表演者")
    artist: Optional[str] = Field(default=None, description="单曲主表演者")
    album: Optional[str] = Field(default=None, description="所属专辑名称")
    album_artist: Optional[str] = Field(default=None, description="专辑表演者")
    year: Optional[int] = Field(default=None, description="发行年份")
    track_number: Optional[int] = Field(default=None, description="专辑曲序")
    disc_number: Optional[int] = Field(default=None, description="专辑碟号")


class MediaServerFileStreamTicketRequest(BaseModel):
    """
    请求为文件管理器选中的本地媒体文件签发短期播放票据。

    文件对象仍需在端点按当前管理用户的存储范围重新核验。
    """

    file: _SchemaFileItem = Field(description="文件管理器当前选中的本地文件条目")
    media_type: Literal["video", "audio"] = Field(default="video", description="所选文件的媒体类型")


class MediaServerFileStreamTicket(BaseModel):
    """
    返回单个文件的受控播放标识与显示标题。

    标识只代表票据签发时通过权限检查的那一个文件。
    """

    item_id: str = Field(description="不包含服务器路径的短期媒体票据标识")
    title: str = Field(description="供播放器显示的文件标题")
    media_type: Literal["video", "audio"] = Field(default="video", description="所选文件的媒体类型")


class WebhookEventInfo(BaseModel):
    """
    Webhook事件信息。
    """
    event: Optional[str] = Field(default=None, description="媒体服务器发出的事件名称")
    channel: Optional[str] = Field(default=None, description="消息渠道标识")
    server_name: Optional[str] = Field(default=None, description="媒体服务器名称")
    item_type: Optional[str] = Field(default=None, description="类型")
    item_name: Optional[str] = Field(default=None, description="媒体服务器中的条目名称")
    item_id: Optional[str] = Field(default=None, description="媒体项目 ID")
    item_path: Optional[str] = Field(default=None, description="媒体服务器报告的文件路径")
    season_id: Optional[str] = Field(default=None, description="媒体服务器中的季标识")
    episode_id: Optional[str] = Field(default=None, description="媒体服务器中的单集标识")
    media_source: Optional[MediaSource] = Field(default=None, description="媒体数据源")
    media_id: Optional[str] = Field(default=None, description="数据源原生 ID")
    overview: Optional[str] = Field(default=None, description="简介")
    percentage: Optional[float] = Field(default=None, description="播放进度百分比，通常为 0 至 100；来源未提供或无法计算时为 None")
    ip: Optional[str] = Field(default=None, description="播放客户端的地址")
    device_name: Optional[str] = Field(default=None, description="播放设备名称")
    client: Optional[str] = Field(default=None, description="触发事件的客户端名称")
    user_name: Optional[str] = Field(default=None, description="用户名")
    image_url: Optional[str] = Field(default=None, description="展示图片地址")
    item_favorite: Optional[bool] = Field(default=None, description="媒体条目是否已被收藏")
    save_reason: Optional[str] = Field(default=None, description="媒体服务器报告的保存原因")
    item_isvirtual: Optional[bool] = Field(default=None, description="媒体条目是否为虚拟条目")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型")
    json_object: Optional[dict[str, JsonData]] = Field(default_factory=dict, description="媒体服务器的原始事件内容")

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_tmdb_identity(cls, data: Any) -> Any:
        """
        在旧事件输入边界把 tmdb_id 迁移为统一媒体身份。
        """
        if not isinstance(data, dict):
            return data
        if data.get("media_source") is not None or data.get("media_id") is not None:
            migrated = dict(data)
            migrated.pop("tmdb_id", None)
            return migrated
        legacy_tmdb_id = data.get("tmdb_id")
        if legacy_tmdb_id in (None, ""):
            return data
        migrated = dict(data)
        migrated["media_source"] = MediaSource.TMDB
        migrated["media_id"] = str(legacy_tmdb_id)
        migrated.pop("tmdb_id", None)
        return migrated

    @model_validator(mode="after")
    def _validate_media_identity(self) -> "WebhookEventInfo":
        """
        确保 webhook 媒体身份始终完整成对且不接受零值。
        """
        normalized_id = str(self.media_id).strip() if self.media_id is not None else None
        if bool(self.media_source) != bool(normalized_id):
            raise ValueError("media_source 和 media_id 必须同时提供")
        if normalized_id == "0":
            raise ValueError("media_id 不能为 0")
        self.media_id = normalized_id
        return self

    @property
    def tmdb_id(self) -> Optional[str]:
        """
        兼容旧插件读取 TMDB 身份；新事件输出不再包含该字段。
        """
        if self.media_source == MediaSource.TMDB:
            return self.media_id
        return None

    @tmdb_id.setter
    def tmdb_id(self, value: Optional[Union[str, int]]) -> None:
        """
        兼容旧插件写入 TMDB 身份，并同步为统一字段。
        """
        if value in (None, ""):
            if self.media_source == MediaSource.TMDB:
                self.media_source = None
                self.media_id = None
            return
        self.media_source = MediaSource.TMDB
        self.media_id = str(value)


class MediaServerPlayItem(BaseModel):
    """
    媒体服务器可播放项目信息。
    """
    id: Optional[Union[str, int]] = Field(default=None, description="记录标识")
    item_id: Optional[Union[str, int]] = Field(default=None, description="媒体项目 ID")
    server: Optional[str] = Field(default=None, description="媒体服务器配置名称")
    server_id: Optional[str] = Field(default=None, description="媒体服务器 ID")
    title: Optional[str] = Field(default=None, description="标题")
    subtitle: Optional[str] = Field(default=None, description="副标题")
    year: Optional[Union[str, int]] = Field(default=None, description="发行年份")
    type: Optional[str] = Field(default=None, description="类型标识")
    image: Optional[str] = Field(default=None, description="图片")
    link: Optional[str] = Field(default=None, description="链接")
    percent: Optional[float] = Field(default=None, description="播放进度百分比")
    BackdropImageTags: Optional[List[str]] = Field(default_factory=list, description="背景图的服务端图片标签")
    server_type: Optional[str] = Field(default=None, description="媒体服务器类型：emby、jellyfin、plex 等")
    use_cookies: Optional[bool] = Field(default=None, description="飞牛的图片需要Cookies")
