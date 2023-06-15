from pathlib import Path
from typing import Optional, Dict

from pydantic import BaseModel

from app.schemas.types import MediaType


class MetaInfo(BaseModel):
    # 是否处理的文件
    isfile: bool = False
    # 原字符串
    org_string: Optional[str] = None
    # 副标题
    subtitle: Optional[str] = None
    # 类型 电影、电视剧
    type: Optional[str] = None
    # 识别的中文名
    cn_name: Optional[str] = None
    # 识别的英文名
    en_name: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 总季数
    total_seasons: int = 0
    # 识别的开始季 数字
    begin_season: Optional[int] = None
    # 识别的结束季 数字
    end_season: Optional[int] = None
    # 总集数
    total_episodes: int = 0
    # 识别的开始集
    begin_episode: Optional[int] = None
    # 识别的结束集
    end_episode: Optional[int] = None
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


class MediaInfo(BaseModel):
    # 类型 电影、电视剧
    type: Optional[str] = None
    # 媒体标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # TMDB ID
    tmdb_id: Optional[int] = None
    # IMDB ID
    imdb_id: Optional[str] = None
    # TVDB ID
    tvdb_id: Optional[str] = None
    # 豆瓣ID
    douban_id: Optional[str] = None
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
    # 评分
    vote_average: int = 0
    # 描述
    overview: Optional[str] = None
    # 二级分类
    category: str = ""


class Context(BaseModel):
    # 元数据
    meta_info: Optional[MetaInfo]
    # 媒体信息
    media_info: Optional[MediaInfo]


class TransferTorrent(BaseModel):
    title: Optional[str] = None
    path: Optional[Path] = None
    hash: Optional[str] = None
    tags: Optional[str] = None


class DownloadingTorrent(BaseModel):
    title: Optional[str] = None
    name: Optional[str] = None
    year: Optional[str] = None
    season_episode: Optional[str] = None
    size: Optional[float] = 0
    progress: Optional[float] = 0


class TransferInfo(BaseModel):
    # 转移⼁路径
    path: Optional[Path] = None
    # 转移后路径
    target_path: Optional[Path] = None
    # 错误信息
    message: Optional[str] = None


class ExistMediaInfo(BaseModel):
    # 类型 电影、电视剧
    type: MediaType
    # 季
    seasons: Optional[Dict[int, list]] = None


class NotExistMediaInfo(BaseModel):
    # 季
    season: int
    # 剧集列表
    episodes: list = []
    # 总集数
    total_episodes: int = 0
    # 开始集
    start_episode: int = 0


class RefreshMediaItem(BaseModel):
    # 标题
    title: str
    # 年份
    year: Optional[str] = None
    # 类型
    type: Optional[MediaType] = None
    # 类别
    category: Optional[str] = None
    # 目录
    target_path: Optional[Path] = None
