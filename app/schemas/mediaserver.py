from pathlib import Path
from typing import Optional, Dict, Union, List, Any

from pydantic import BaseModel, Field, ConfigDict, RootModel, model_validator

from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MediaSource, MediaType


class ExistMediaInfo(BaseModel):
    """
    媒体服务器存在媒体信息
    """
    # 类型 电影、电视剧、音乐
    type: Optional[MediaType] = None
    # 季
    seasons: Optional[Dict[int, List[int]]] = Field(default_factory=dict)
    # 媒体服务器类型：plex、jellyfin、emby、zspace、trimemedia、ugreen、navidrome
    server_type: Optional[str] = None
    # 媒体服务器名称
    server: Optional[str] = None
    # 媒体ID
    itemid: Optional[Union[str, int]] = None


class MediaServerPlayData(BaseModel):
    """媒体服务器在线播放地址。"""

    url: str = Field(description="播放地址")
    item_id: Optional[str] = Field(default=None, description="媒体项目 ID")
    server_id: Optional[str] = Field(default=None, description="媒体服务器 ID")
    server_type: Optional[str] = Field(default=None, description="媒体服务器类型")


class MediaServerExistsData(BaseModel):
    """本地媒体存在性查询结果。"""

    item: Dict[str, str] = Field(default_factory=dict, description="命中的媒体项目")


class MediaServerExistingEpisodes(RootModel[Dict[int, List[int]]]):
    """媒体服务器中按季号归组的已存在集号。"""


class NotExistMediaInfo(BaseModel):
    """
    媒体服务器不存在媒体信息
    """
    # 季
    season: Optional[int] = None
    # 剧集列表
    episodes: Optional[List[int]] = Field(default_factory=list)
    # 总集数
    total_episode: Optional[int] = 0
    # 开始集
    start_episode: Optional[int] = 0
    # 候选资源须完整覆盖目标范围
    require_complete_coverage: Optional[bool] = False


class RefreshMediaItem(BaseModel):
    """
    媒体库刷新信息
    """
    # 标题
    title: Optional[str] = None
    # 年份
    year: Optional[Union[str, int]] = None
    # 类型
    type: Optional[MediaType] = None
    # 类别
    category: Optional[str] = None
    # 目录
    target_path: Optional[Path] = None


class MediaServerLibrary(BaseModel):
    """
    媒体服务器媒体库信息
    """
    # 服务器
    server: Optional[str] = None
    # ID
    id: Optional[Union[str, int]] = None
    # 媒体服务器项目ID
    item_id: Optional[Union[str, int]] = None
    # 媒体服务器ID
    server_id: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 路径
    path: Optional[Union[str, List[str]]] = None
    # 类型
    type: Optional[str] = None
    # 媒体库内媒体数量
    item_count: Optional[int] = None
    # 封面图
    image: Optional[str] = None
    # 封面图列表
    image_list: Optional[List[str]] = None
    # 跳转链接
    link: Optional[str] = None
    # 服务器类型
    server_type: Optional[str] = None
    # 飞牛的图片需要Cookies
    use_cookies: Optional[bool] = None


class MediaServerItemUserState(BaseModel):
    """媒体服务器条目的用户播放状态。"""

    # 已播放
    played: Optional[bool] = None
    # 继续播放
    resume: Optional[bool] = None
    # 上次播放时间 10位时间戳
    last_played_date: Optional[str] = None
    # 播放次数(不等于完播次数，理解为浏览次数)
    play_count: Optional[int] = None
    # 播放进度
    percentage: Optional[float] = None


class MediaServerItem(OptionalMediaIdentityMixin, BaseModel):
    """
    媒体服务器媒体信息
    """
    # ID
    id: Optional[Union[str, int]] = None
    # 服务器
    server: Optional[str] = None
    # 媒体库ID
    library: Optional[Union[str, int]] = None
    # 媒体服务器ID
    server_id: Optional[str] = None
    # ID
    item_id: Optional[str] = None
    # 类型
    item_type: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 原标题
    original_title: Optional[str] = None
    # 年份
    year: Optional[Union[str, int]] = None
    # 媒体数据源与原生ID
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    # 路径
    path: Optional[str] = None
    # 季集
    seasoninfo: Optional[Dict[int, List[int]]] = None
    # 备注
    note: Optional[JsonData] = None
    # 同步时间
    lst_mod_date: Optional[str] = None
    user_state: Optional[MediaServerItemUserState] = None

    model_config = ConfigDict(from_attributes=True)


class MediaServerSeasonInfo(BaseModel):
    """
    媒体服务器媒体剧集信息
    """
    season: Optional[int] = None
    episodes: Optional[List[int]] = Field(default_factory=list)


class WebhookEventInfo(BaseModel):
    """
    Webhook事件信息
    """
    event: Optional[str] = None
    channel: Optional[str] = None
    server_name: Optional[str] = None
    item_type: Optional[str] = None
    item_name: Optional[str] = None
    item_id: Optional[str] = None
    item_path: Optional[str] = None
    season_id: Optional[str] = None
    episode_id: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    overview: Optional[str] = None
    percentage: Optional[float] = None
    ip: Optional[str] = None
    device_name: Optional[str] = None
    client: Optional[str] = None
    user_name: Optional[str] = None
    image_url: Optional[str] = None
    item_favorite: Optional[bool] = None
    save_reason: Optional[str] = None
    item_isvirtual: Optional[bool] = None
    media_type: Optional[str] = None
    json_object: Optional[dict[str, JsonData]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_tmdb_identity(cls, data: Any) -> Any:
        """在旧事件输入边界把 tmdb_id 迁移为统一媒体身份。"""
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
        """确保 webhook 媒体身份始终完整成对且不接受零值。"""
        normalized_id = str(self.media_id).strip() if self.media_id is not None else None
        if bool(self.media_source) != bool(normalized_id):
            raise ValueError("media_source 和 media_id 必须同时提供")
        if normalized_id == "0":
            raise ValueError("media_id 不能为 0")
        self.media_id = normalized_id
        return self

    @property
    def tmdb_id(self) -> Optional[str]:
        """兼容旧插件读取 TMDB 身份；新事件输出不再包含该字段。"""
        if self.media_source == MediaSource.TMDB:
            return self.media_id
        return None

    @tmdb_id.setter
    def tmdb_id(self, value: Optional[Union[str, int]]) -> None:
        """兼容旧插件写入 TMDB 身份，并同步为统一字段。"""
        if value in (None, ""):
            if self.media_source == MediaSource.TMDB:
                self.media_source = None
                self.media_id = None
            return
        self.media_source = MediaSource.TMDB
        self.media_id = str(value)


class MediaServerPlayItem(BaseModel):
    """
    媒体服务器可播放项目信息
    """
    id: Optional[Union[str, int]] = None
    item_id: Optional[Union[str, int]] = None
    server_id: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    type: Optional[str] = None
    image: Optional[str] = None
    link: Optional[str] = None
    percent: Optional[float] = None
    BackdropImageTags: Optional[List[str]] = Field(default_factory=list)
    server_type: Optional[str] = None
    # 飞牛的图片需要Cookies
    use_cookies: Optional[bool] = None
