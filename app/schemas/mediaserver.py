from pathlib import Path

from pydantic import BaseModel

from app.schemas.types import MediaType


class ExistMediaInfo(BaseModel):
    """
    媒体服务器存在媒体信息
    """
    # 类型 电影、电视剧
    type: MediaType | None
    # 季
    seasons: dict[int, list] | None = {}
    # 媒体服务器
    server: str | None = None
    # 媒体ID
    itemid: str | int | None = None


class NotExistMediaInfo(BaseModel):
    """
    媒体服务器不存在媒体信息
    """
    # 季
    season: int | None = None
    # 剧集列表
    episodes: list | None = []
    # 总集数
    total_episode: int | None = 0
    # 开始集
    start_episode: int | None = 0


class RefreshMediaItem(BaseModel):
    """
    媒体库刷新信息
    """
    # 标题
    title: str | None = None
    # 年份
    year: str | None = None
    # 类型
    type: MediaType | None = None
    # 类别
    category: str | None = None
    # 目录
    target_path: Path | None = None


class MediaServerLibrary(BaseModel):
    """
    媒体服务器媒体库信息
    """
    # 服务器
    server: str | None = None
    # ID
    id: str | int | None = None
    # 名称
    name: str | None = None
    # 路径
    path: str | list | None = None
    # 类型
    type: str | None = None
    # 封面图
    image: str | None = None
    # 封面图列表
    image_list: list[str] | None = None
    # 跳转链接
    link: str | None = None


class MediaServerItem(BaseModel):
    """
    媒体服务器媒体信息
    """
    # ID
    id: str | int | None = None
    # 服务器
    server: str | None = None
    # 媒体库ID
    library: str | int | None = None
    # ID
    item_id: str | None = None
    # 类型
    item_type: str | None = None
    # 标题
    title: str | None = None
    # 原标题
    original_title: str | None = None
    # 年份
    year: str | None = None
    # TMDBID
    tmdbid: int | None = None
    # IMDBID
    imdbid: str | None = None
    # TVDBID
    tvdbid: str | None = None
    # 路径
    path: str | None = None
    # 季集
    seasoninfo: dict[int, list] | None = None
    # 备注
    note: str | None = None
    # 同步时间
    lst_mod_date: str | None = None

    class Config:
        orm_mode = True


class MediaServerSeasonInfo(BaseModel):
    """
    媒体服务器媒体剧集信息
    """
    season: int | None = None
    episodes: list[int] | None = []


class WebhookEventInfo(BaseModel):
    """
    Webhook事件信息
    """
    event: str | None = None
    channel: str | None = None
    item_type: str | None = None
    item_name: str | None = None
    item_id: str | None = None
    item_path: str | None = None
    season_id: str | None = None
    episode_id: str | None = None
    tmdb_id: str | None = None
    overview: str | None = None
    percentage: float | None = None
    ip: str | None = None
    device_name: str | None = None
    client: str | None = None
    user_name: str | None = None
    image_url: str | None = None
    item_favorite: bool | None = None
    save_reason: str | None = None
    item_isvirtual: bool | None = None
    media_type: str | None = None


class MediaServerPlayItem(BaseModel):
    """
    媒体服务器可播放项目信息
    """
    id: str | int | None = None
    title: str | None = None
    subtitle: str | None = None
    type: str | None = None
    image: str | None = None
    link: str | None = None
    percent: float | None = None
