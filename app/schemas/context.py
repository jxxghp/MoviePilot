from typing import Optional, Dict, List, Union

from pydantic import BaseModel


class MetaInfo(BaseModel):
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
    # 类型 电影、电视剧
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
    # 应用的识别词信息
    apply_words: Optional[List[str]] = None


class MediaInfo(BaseModel):
    """
    识别媒体信息
    """
    # 来源：themoviedb、douban、bangumi
    source: Optional[str] = None
    # 类型 电影、电视剧
    type: Optional[str] = None
    # 媒体标题
    title: Optional[str] = None
    # 英文标题
    en_title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 标题（年份）
    title_year: Optional[str] = None
    # 当前指定季，如有
    season: Optional[int] = None
    # TMDB ID
    tmdb_id: Optional[int] = None
    # IMDB ID
    imdb_id: Optional[str] = None
    # TVDB ID
    tvdb_id: Optional[str] = None
    # 豆瓣ID
    douban_id: Optional[str] = None
    # Bangumi ID
    bangumi_id: Optional[int] = None
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
    vote_average: Optional[float] = 0
    # 描述
    overview: Optional[str] = None
    # 二级分类
    category: Optional[str] = ""
    # 季季集清单
    seasons: Optional[Dict[int, list]] = {}
    # 季详情
    season_info: Optional[List[dict]] = []
    # 别名和译名
    names: Optional[list] = []
    # 演员
    actors: Optional[list] = []
    # 导演
    directors: Optional[list] = []
    # 详情链接
    detail_link: Optional[str] = None
    # 其它TMDB属性
    # 是否成人内容
    adult: Optional[bool] = False
    # 创建人
    created_by: Optional[list] = []
    # 集时长
    episode_run_time: Optional[list] = []
    # 风格
    genres: Optional[List[dict]] = []
    # 首播日期
    first_air_date: Optional[str] = None
    # 首页
    homepage: Optional[str] = None
    # 语种
    languages: Optional[list] = []
    # 最后上映日期
    last_air_date: Optional[str] = None
    # 流媒体平台
    networks: Optional[list] = []
    # 集数
    number_of_episodes: Optional[int] = 0
    # 季数
    number_of_seasons: Optional[int] = 0
    # 原产国
    origin_country: Optional[list] = []
    # 原名
    original_name: Optional[str] = None
    # 出品公司
    production_companies: Optional[list] = []
    # 出品国
    production_countries: Optional[list] = []
    # 语种
    spoken_languages: Optional[list] = []
    # 状态
    status: Optional[str] = None
    # 标签
    tagline: Optional[str] = None
    # 风格ID
    genre_ids: Optional[list] = []
    # 评价数量
    vote_count: Optional[int] = 0
    # 流行度
    popularity: Optional[int] = 0
    # 时长
    runtime: Optional[int] = None
    # 下一集
    next_episode_to_air: Optional[dict] = {}


class TorrentInfo(BaseModel):
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
    # 种子名称
    title: Optional[str] = None
    # 种子副标题
    description: Optional[str] = None
    # IMDB ID
    imdbid: Optional[str] = None
    # 种子链接
    enclosure: Optional[str] = None
    # 详情页面
    page_url: Optional[str] = None
    # 种子大小
    size: Optional[float] = 0
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
    labels: Optional[list] = []
    # 种子优先级
    pri_order: Optional[int] = 0
    # 促销
    volume_factor: Optional[str] = None
    # 剩余免费时间
    freedate_diff: Optional[str] = None


class Context(BaseModel):
    """
    上下文
    """
    # 元数据
    meta_info: Optional[MetaInfo] = None
    # 媒体信息
    media_info: Optional[MediaInfo] = None
    # 种子信息
    torrent_info: Optional[TorrentInfo] = None


class MediaPerson(BaseModel):
    """
    媒体人物信息
    """
    # 来源：themoviedb、douban、bangumi
    source: Optional[str] = None
    # 公共
    id: Optional[int] = None
    type: Optional[Union[str, int]] = 1
    name: Optional[str] = None
    character: Optional[str] = None
    images: Optional[dict] = {}
    # themoviedb
    profile_path: Optional[str] = None
    gender: Optional[Union[str, int]] = None
    original_name: Optional[str] = None
    credit_id: Optional[str] = None
    also_known_as: Optional[list] = []
    birthday: Optional[str] = None
    deathday: Optional[str] = None
    imdb_id: Optional[str] = None
    known_for_department: Optional[str] = None
    place_of_birth: Optional[str] = None
    popularity: Optional[float] = None
    biography: Optional[str] = None
    # douban
    roles: Optional[list] = []
    title: Optional[str] = None
    url: Optional[str] = None
    avatar: Optional[Union[str, dict]] = None
    latin_name: Optional[str] = None
    # bangumi
    career: Optional[list] = []
    relation: Optional[str] = None
