"""
媒体服务器播放、媒体条目和回调事件的数据结构。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from app.schemas.common import JsonData
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.types import MediaSource, MediaType


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
    server_type: Optional[str] = Field(default=None, description="媒体服务器类型")


class MediaServerExistsData(BaseModel):
    """
    本地媒体存在性查询结果。
    """

    item: Dict[str, str] = Field(default_factory=dict, description="命中的媒体项目")


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
    name: Optional[str] = Field(default=None, description="名称")
    path: Optional[Union[str, List[str]]] = Field(default=None, description="路径")
    type: Optional[str] = Field(default=None, description="类型")
    item_count: Optional[int] = Field(default=None, description="媒体库内媒体数量")
    image: Optional[str] = Field(default=None, description="封面图")
    image_list: Optional[List[str]] = Field(default=None, description="封面图列表")
    link: Optional[str] = Field(default=None, description="跳转链接")
    server_type: Optional[str] = Field(default=None, description="服务器类型")
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
    server_id: Optional[str] = Field(default=None, description="媒体服务器 ID")
    title: Optional[str] = Field(default=None, description="标题")
    subtitle: Optional[str] = Field(default=None, description="副标题")
    type: Optional[str] = Field(default=None, description="类型标识")
    image: Optional[str] = Field(default=None, description="图片")
    link: Optional[str] = Field(default=None, description="链接")
    percent: Optional[float] = Field(default=None, description="播放进度百分比")
    BackdropImageTags: Optional[List[str]] = Field(default_factory=list, description="背景图的服务端图片标签")
    server_type: Optional[str] = Field(default=None, description="媒体服务器类型：emby、jellyfin、plex 等")
    use_cookies: Optional[bool] = Field(default=None, description="飞牛的图片需要Cookies")
