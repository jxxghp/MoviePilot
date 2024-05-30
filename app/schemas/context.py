
from pydantic import BaseModel


class MetaInfo(BaseModel):
    """
    识别元数据
    """
    # 是否处理的文件
    isfile: bool | None = False
    # 原字符串
    org_string: str | None = None
    # 原标题
    title: str | None = None
    # 副标题
    subtitle: str | None = None
    # 类型 电影、电视剧
    type: str | None = None
    # 名称
    name: str | None = None
    # 识别的中文名
    cn_name: str | None = None
    # 识别的英文名
    en_name: str | None = None
    # 年份
    year: str | None = None
    # 总季数
    total_season: int | None = 0
    # 识别的开始季 数字
    begin_season: int | None = None
    # 识别的结束季 数字
    end_season: int | None = None
    # 总集数
    total_episode: int | None = 0
    # 识别的开始集
    begin_episode: int | None = None
    # 识别的结束集
    end_episode: int | None = None
    # SxxExx
    season_episode: str | None = None
    # Partx Cd Dvd Disk Disc
    part: str | None = None
    # 识别的资源类型
    resource_type: str | None = None
    # 识别的效果
    resource_effect: str | None = None
    # 识别的分辨率
    resource_pix: str | None = None
    # 识别的制作组/字幕组
    resource_team: str | None = None
    # 视频编码
    video_encode: str | None = None
    # 音频编码
    audio_encode: str | None = None
    # 资源类型
    edition: str | None = None
    # 应用的识别词信息
    apply_words: list[str] | None = None


class MediaInfo(BaseModel):
    """
    识别媒体信息
    """
    # 来源：themoviedb、douban、bangumi
    source: str | None = None
    # 类型 电影、电视剧
    type: str | None = None
    # 媒体标题
    title: str | None = None
    # 英文标题
    en_title: str | None = None
    # 年份
    year: str | None = None
    # 标题（年份）
    title_year: str | None = None
    # 当前指定季，如有
    season: int | None = None
    # TMDB ID
    tmdb_id: int | None = None
    # IMDB ID
    imdb_id: str | None = None
    # TVDB ID
    tvdb_id: str | None = None
    # 豆瓣ID
    douban_id: str | None = None
    # Bangumi ID
    bangumi_id: int | None = None
    # 媒体原语种
    original_language: str | None = None
    # 媒体原发行标题
    original_title: str | None = None
    # 媒体发行日期
    release_date: str | None = None
    # 背景图片
    backdrop_path: str | None = None
    # 海报图片
    poster_path: str | None = None
    # 评分
    vote_average: float | None = 0
    # 描述
    overview: str | None = None
    # 二级分类
    category: str | None = ""
    # 季季集清单
    seasons: dict[int, list] | None = {}
    # 季详情
    season_info: list[dict] | None = []
    # 别名和译名
    names: list | None = []
    # 演员
    actors: list | None = []
    # 导演
    directors: list | None = []
    # 详情链接
    detail_link: str | None = None
    # 其它TMDB属性
    # 是否成人内容
    adult: bool | None = False
    # 创建人
    created_by: list | None = []
    # 集时长
    episode_run_time: list | None = []
    # 风格
    genres: list[dict] | None = []
    # 首播日期
    first_air_date: str | None = None
    # 首页
    homepage: str | None = None
    # 语种
    languages: list | None = []
    # 最后上映日期
    last_air_date: str | None = None
    # 流媒体平台
    networks: list | None = []
    # 集数
    number_of_episodes: int | None = 0
    # 季数
    number_of_seasons: int | None = 0
    # 原产国
    origin_country: list | None = []
    # 原名
    original_name: str | None = None
    # 出品公司
    production_companies: list | None = []
    # 出品国
    production_countries: list | None = []
    # 语种
    spoken_languages: list | None = []
    # 状态
    status: str | None = None
    # 标签
    tagline: str | None = None
    # 风格ID
    genre_ids: list | None = []
    # 评价数量
    vote_count: int | None = 0
    # 流行度
    popularity: int | None = 0
    # 时长
    runtime: int | None = None
    # 下一集
    next_episode_to_air: dict | None = {}


class TorrentInfo(BaseModel):
    """
    搜索种子信息
    """
    # 站点ID
    site: int | None = None
    # 站点名称
    site_name: str | None = None
    # 站点Cookie
    site_cookie: str | None = None
    # 站点UA
    site_ua: str | None = None
    # 站点是否使用代理
    site_proxy: bool | None = False
    # 站点优先级
    site_order: int | None = 0
    # 种子名称
    title: str | None = None
    # 种子副标题
    description: str | None = None
    # IMDB ID
    imdbid: str | None = None
    # 种子链接
    enclosure: str | None = None
    # 详情页面
    page_url: str | None = None
    # 种子大小
    size: float | None = 0
    # 做种者
    seeders: int | None = 0
    # 下载者
    peers: int | None = 0
    # 完成者
    grabs: int | None = 0
    # 发布时间
    pubdate: str | None = None
    # 已过时间
    date_elapsed: str | None = None
    # 免费截止时间
    freedate: str | None = None
    # 上传因子
    uploadvolumefactor: float | None = None
    # 下载因子
    downloadvolumefactor: float | None = None
    # HR
    hit_and_run: bool | None = False
    # 种子标签
    labels: list | None = []
    # 种子优先级
    pri_order: int | None = 0
    # 促销
    volume_factor: str | None = None
    # 剩余免费时间
    freedate_diff: str | None = None


class Context(BaseModel):
    """
    上下文
    """
    # 元数据
    meta_info: MetaInfo | None = None
    # 媒体信息
    media_info: MediaInfo | None = None
    # 种子信息
    torrent_info: TorrentInfo | None = None


class MediaPerson(BaseModel):
    """
    媒体人物信息
    """
    # 来源：themoviedb、douban、bangumi
    source: str | None = None
    # 公共
    id: int | None = None
    type: str | int | None = 1
    name: str | None = None
    character: str | None = None
    images: dict | None = {}
    # themoviedb
    profile_path: str | None = None
    gender: str | int | None = None
    original_name: str | None = None
    credit_id: str | None = None
    also_known_as: list | None = []
    birthday: str | None = None
    deathday: str | None = None
    imdb_id: str | None = None
    known_for_department: str | None = None
    place_of_birth: str | None = None
    popularity: float | None = None
    biography: str | None = None
    # douban
    roles: list | None = []
    title: str | None = None
    url: str | None = None
    avatar: str | dict | None = None
    latin_name: str | None = None
    # bangumi
    career: list | None = []
    relation: str | None = None
