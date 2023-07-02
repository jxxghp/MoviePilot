from pathlib import Path
from typing import Optional, Dict, List

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
    # 季
    season: Optional[int] = None
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
    # 季季集清单
    seasons: Dict[int, list] = {}
    # 季详情
    season_info: List[dict] = []
    # 别名和译名
    names: list = []
    # 演员
    actors: list = []
    # 导演
    directors: list = []
    # 其它TMDB属性
    adult: bool = False
    created_by: list = []
    episode_run_time: list = []
    genres: list = []
    first_air_date: Optional[str] = None
    homepage: Optional[str] = None
    languages: list = []
    last_air_date: Optional[str] = None
    networks: list = []
    number_of_episodes: int = 0
    number_of_seasons: int = 0
    origin_country: list = []
    original_name: Optional[str] = None
    production_companies: list = []
    production_countries: list = []
    spoken_languages: list = []
    status: Optional[str] = None
    tagline: Optional[str] = None
    vote_count: int = 0
    popularity: int = 0
    runtime: Optional[int] = None
    next_episode_to_air: Optional[str] = None


class TorrentInfo(BaseModel):
    # 站点ID
    site: Optional[int] = None
    # 站点名称
    site_name: Optional[str] = None
    # 站点Cookie
    site_cookie: Optional[str] = None
    # 站点UA
    site_ua: Optional[str] = None
    # 站点是否使用代理
    site_proxy: bool = False
    # 站点优先级
    site_order: int = 0
    # 种子名称
    title: Optional[str] = None
    # 种子副标题
    description: Optional[str] = None
    # IMDB ID
    imdbid: str = None
    # 种子链接
    enclosure: Optional[str] = None
    # 详情页面
    page_url: Optional[str] = None
    # 种子大小
    size: float = 0
    # 做种者
    seeders: int = 0
    # 下载者
    peers: int = 0
    # 完成者
    grabs: int = 0
    # 发布时间
    pubdate: Optional[str] = None
    # 已过时间
    date_elapsed: Optional[str] = None
    # 上传因子
    uploadvolumefactor: Optional[float] = None
    # 下载因子
    downloadvolumefactor: Optional[float] = None
    # HR
    hit_and_run: bool = False
    # 种子标签
    labels: Optional[list] = []
    # 种子优先级
    pri_order: int = 0


class Context(BaseModel):
    # 元数据
    meta_info: Optional[MetaInfo]
    # 媒体信息
    media_info: Optional[MediaInfo]
    # 种子信息
    torrent_info: Optional[TorrentInfo]


class TransferTorrent(BaseModel):
    title: Optional[str] = None
    path: Optional[Path] = None
    hash: Optional[str] = None
    tags: Optional[str] = None


class DownloadingTorrent(BaseModel):
    hash: Optional[str] = None
    title: Optional[str] = None
    name: Optional[str] = None
    year: Optional[str] = None
    season_episode: Optional[str] = None
    size: Optional[float] = 0
    progress: Optional[float] = 0
    state: Optional[str] = 'downloading'
    upspeed: Optional[str] = None
    dlspeed: Optional[str] = None
    media: Optional[dict] = {}


class TransferInfo(BaseModel):
    # 转移⼁路径
    path: Optional[Path] = None
    # 转移后路径
    target_path: Optional[Path] = None
    # 处理文件数
    file_count: int = 0
    # 总文件大小
    total_size: float = 0
    # 失败清单
    fail_list: list = []
    # 错误信息
    message: Optional[str] = None


class ExistMediaInfo(BaseModel):
    # 类型 电影、电视剧
    type: MediaType
    # 季
    seasons: Dict[int, list] = {}


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
