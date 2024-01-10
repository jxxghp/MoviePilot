from pathlib import Path
from typing import Optional, Dict, Union, List

from pydantic import BaseModel

from app.schemas.types import MediaType


class ExistMediaInfo(BaseModel):
    """
    媒体服务器存在媒体信息
    """
    # 类型 电影、电视剧
    type: Optional[MediaType]
    # 季
    seasons: Optional[Dict[int, list]] = {}
    # 媒体服务器
    server: Optional[str] = None
    # 媒体ID
    itemid: Optional[Union[str, int]] = None


class NotExistMediaInfo(BaseModel):
    """
    媒体服务器不存在媒体信息
    """
    # 季
    season: Optional[int] = None
    # 剧集列表
    episodes: Optional[list] = []
    # 总集数
    total_episode: Optional[int] = 0
    # 开始集
    start_episode: Optional[int] = 0


class RefreshMediaItem(BaseModel):
    """
    媒体库刷新信息
    """
    # 标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
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
    # 名称
    name: Optional[str] = None
    # 路径
    path: Optional[Union[str, list]] = None
    # 类型
    type: Optional[str] = None
    # 封面图
    image: Optional[str] = None
    # 封面图列表
    image_list: Optional[List[str]] = None
    # 跳转链接
    link: Optional[str] = None


class MediaServerItem(BaseModel):
    """
    媒体服务器媒体信息
    """
    # ID
    id: Optional[Union[str, int]] = None
    # 服务器
    server: Optional[str] = None
    # 媒体库ID
    library: Optional[Union[str, int]] = None
    # ID
    item_id: Optional[str] = None
    # 类型
    item_type: Optional[str] = None
    # 标题
    title: Optional[str] = None
    # 原标题
    original_title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # TMDBID
    tmdbid: Optional[int] = None
    # IMDBID
    imdbid: Optional[str] = None
    # TVDBID
    tvdbid: Optional[str] = None
    # 路径
    path: Optional[str] = None
    # 季集
    seasoninfo: Optional[Dict[int, list]] = None
    # 备注
    note: Optional[str] = None
    # 同步时间
    lst_mod_date: Optional[str] = None

    class Config:
        orm_mode = True


class MediaServerSeasonInfo(BaseModel):
    """
    媒体服务器媒体剧集信息
    """
    season: Optional[int] = None
    episodes: Optional[List[int]] = []


class WebhookEventInfo(BaseModel):
    """
    Webhook事件信息
    """
    event: Optional[str] = None
    channel: Optional[str] = None
    item_type: Optional[str] = None
    item_name: Optional[str] = None
    item_id: Optional[str] = None
    item_path: Optional[str] = None
    season_id: Optional[str] = None
    episode_id: Optional[str] = None
    tmdb_id: Optional[str] = None
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


class MediaServerPlayItem(BaseModel):
    """
    媒体服务器可播放项目信息
    """
    id: Optional[Union[str, int]] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    type: Optional[str] = None
    image: Optional[str] = None
    link: Optional[str] = None
    percent: Optional[float] = None
