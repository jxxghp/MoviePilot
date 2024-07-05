import re
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Tuple

from app.core.config import settings
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.schemas.types import MediaType
from app.utils.string import StringUtils


@dataclass
class TorrentInfo:
    # 站点ID
    site: int = None
    # 站点名称
    site_name: str = None
    # 站点Cookie
    site_cookie: str = None
    # 站点UA
    site_ua: str = None
    # 站点是否使用代理
    site_proxy: bool = False
    # 站点优先级
    site_order: int = 0
    # 种子名称
    title: str = None
    # 种子副标题
    description: str = None
    # IMDB ID
    imdbid: str = None
    # 种子链接
    enclosure: str = None
    # 详情页面
    page_url: str = None
    # 种子大小
    size: float = 0
    # 做种者
    seeders: int = 0
    # 下载者
    peers: int = 0
    # 完成者
    grabs: int = 0
    # 发布时间
    pubdate: str = None
    # 已过时间
    date_elapsed: str = None
    # 免费截止时间
    freedate: str = None
    # 上传因子
    uploadvolumefactor: float = None
    # 下载因子
    downloadvolumefactor: float = None
    # HR
    hit_and_run: bool = False
    # 种子标签
    labels: list = field(default_factory=list)
    # 种子优先级
    pri_order: int = 0
    # 种子分类 电影/电视剧
    category: str = None

    def __setattr__(self, name: str, value: Any):
        self.__dict__[name] = value

    def __get_properties(self):
        """
        获取属性列表
        """
        property_names = []
        for member_name in dir(self.__class__):
            member = getattr(self.__class__, member_name)
            if isinstance(member, property):
                property_names.append(member_name)
        return property_names

    def from_dict(self, data: dict):
        """
        从字典中初始化
        """
        properties = self.__get_properties()
        for key, value in data.items():
            if key in properties:
                continue
            setattr(self, key, value)

    @staticmethod
    def get_free_string(upload_volume_factor: float, download_volume_factor: float) -> str:
        """
        计算促销类型
        """
        if upload_volume_factor is None or download_volume_factor is None:
            return "未知"
        free_strs = {
            "1.0 1.0": "普通",
            "1.0 0.0": "免费",
            "2.0 1.0": "2X",
            "4.0 1.0": "4X",
            "2.0 0.0": "2X免费",
            "4.0 0.0": "4X免费",
            "1.0 0.5": "50%",
            "2.0 0.5": "2X 50%",
            "1.0 0.7": "70%",
            "1.0 0.3": "30%"
        }
        return free_strs.get('%.1f %.1f' % (upload_volume_factor, download_volume_factor), "未知")

    @property
    def volume_factor(self):
        """
        返回促销信息
        """
        return self.get_free_string(self.uploadvolumefactor, self.downloadvolumefactor)

    @property
    def freedate_diff(self):
        """
        返回免费剩余时间
        """
        if not self.freedate:
            return ""
        return StringUtils.diff_time_str(self.freedate)

    def to_dict(self):
        """
        返回字典
        """
        dicts = asdict(self)
        dicts["volume_factor"] = self.volume_factor
        dicts["freedate_diff"] = self.freedate_diff
        return dicts


@dataclass
class MediaInfo:
    # 来源：themoviedb、douban、bangumi
    source: str = None
    # 类型 电影、电视剧
    type: MediaType = None
    # 媒体标题
    title: str = None
    # 英文标题
    en_title: str = None
    # 新加坡标题
    sg_title: str = None
    # 年份
    year: str = None
    # 季
    season: int = None
    # TMDB ID
    tmdb_id: int = None
    # IMDB ID
    imdb_id: str = None
    # TVDB ID
    tvdb_id: int = None
    # 豆瓣ID
    douban_id: str = None
    # Bangumi ID
    bangumi_id: int = None
    # 媒体原语种
    original_language: str = None
    # 媒体原发行标题
    original_title: str = None
    # 媒体发行日期
    release_date: str = None
    # 背景图片
    backdrop_path: str = None
    # 海报图片
    poster_path: str = None
    # LOGO
    logo_path: str = None
    # 评分
    vote_average: float = 0
    # 描述
    overview: str = None
    # 风格ID
    genre_ids: list = field(default_factory=list)
    # 所有别名和译名
    names: list = field(default_factory=list)
    # 各季的剧集清单信息
    seasons: Dict[int, list] = field(default_factory=dict)
    # 各季详情
    season_info: List[dict] = field(default_factory=list)
    # 各季的年份
    season_years: dict = field(default_factory=dict)
    # 二级分类
    category: str = ""
    # TMDB INFO
    tmdb_info: dict = field(default_factory=dict)
    # 豆瓣 INFO
    douban_info: dict = field(default_factory=dict)
    # Bangumi INFO
    bangumi_info: dict = field(default_factory=dict)
    # 导演
    directors: List[dict] = field(default_factory=list)
    # 演员
    actors: List[dict] = field(default_factory=list)
    # 是否成人内容
    adult: bool = False
    # 创建人
    created_by: list = field(default_factory=list)
    # 集时长
    episode_run_time: list = field(default_factory=list)
    # 风格
    genres: List[dict] = field(default_factory=list)
    # 首播日期
    first_air_date: str = None
    # 首页
    homepage: str = None
    # 语种
    languages: list = field(default_factory=list)
    # 最后上映日期
    last_air_date: str = None
    # 流媒体平台
    networks: list = field(default_factory=list)
    # 集数
    number_of_episodes: int = 0
    # 季数
    number_of_seasons: int = 0
    # 原产国
    origin_country: list = field(default_factory=list)
    # 原名
    original_name: str = None
    # 出品公司
    production_companies: list = field(default_factory=list)
    # 出品国
    production_countries: list = field(default_factory=list)
    # 语种
    spoken_languages: list = field(default_factory=list)
    # 状态
    status: str = None
    # 标签
    tagline: str = None
    # 评价数量
    vote_count: int = 0
    # 流行度
    popularity: int = 0
    # 时长
    runtime: int = None
    # 下一集
    next_episode_to_air: dict = field(default_factory=dict)

    def __post_init__(self):
        # 设置媒体信息
        if self.tmdb_info:
            self.set_tmdb_info(self.tmdb_info)
        if self.douban_info:
            self.set_douban_info(self.douban_info)
        if self.bangumi_info:
            self.set_bangumi_info(self.bangumi_info)

    def __setattr__(self, name: str, value: Any):
        self.__dict__[name] = value

    def __get_properties(self):
        """
        获取属性列表
        """
        property_names = []
        for member_name in dir(self.__class__):
            member = getattr(self.__class__, member_name)
            if isinstance(member, property):
                property_names.append(member_name)
        return property_names

    def from_dict(self, data: dict):
        """
        从字典中初始化
        """
        properties = self.__get_properties()
        for key, value in data.items():
            if key in properties:
                continue
            setattr(self, key, value)
        if isinstance(self.type, str):
            self.type = MediaType(self.type)

    def set_image(self, name: str, image: str):
        """
        设置图片地址
        """
        setattr(self, f"{name}_path", image)

    def get_image(self, name: str):
        """
        获取图片地址
        """
        try:
            return getattr(self, f"{name}_path")
        except AttributeError:
            return None

    def set_category(self, cat: str):
        """
        设置二级分类
        """
        self.category = cat or ""

    def set_tmdb_info(self, info: dict):
        """
        初始化媒信息
        """

        def __directors_actors(tmdbinfo: dict) -> Tuple[List[dict], List[dict]]:
            """
            查询导演和演员
            :param tmdbinfo: TMDB元数据
            :return: 导演列表，演员列表
            """
            """
            "cast": [
              {
                "adult": false,
                "gender": 2,
                "id": 3131,
                "known_for_department": "Acting",
                "name": "Antonio Banderas",
                "original_name": "Antonio Banderas",
                "popularity": 60.896,
                "profile_path": "/iWIUEwgn2KW50MssR7tdPeFoRGW.jpg",
                "cast_id": 2,
                "character": "Puss in Boots (voice)",
                "credit_id": "6052480e197de4006bb47b9a",
                "order": 0
              }
            ],
            "crew": [
              {
                "adult": false,
                "gender": 2,
                "id": 5524,
                "known_for_department": "Production",
                "name": "Andrew Adamson",
                "original_name": "Andrew Adamson",
                "popularity": 9.322,
                "profile_path": "/qqIAVKAe5LHRbPyZUlptsqlo4Kb.jpg",
                "credit_id": "63b86b2224b33300a0585bf1",
                "department": "Production",
                "job": "Executive Producer"
              }
            ]
            """
            if not tmdbinfo:
                return [], []
            _credits = tmdbinfo.get("credits")
            if not _credits:
                return [], []
            directors = []
            actors = []
            for cast in _credits.get("cast") or []:
                if cast.get("known_for_department") == "Acting":
                    actors.append(cast)
            for crew in _credits.get("crew") or []:
                if crew.get("job") in ["Director", "Writer", "Editor", "Producer"]:
                    directors.append(crew)
            return directors, actors

        if not info:
            return
        # 来源
        self.source = "themoviedb"
        # 本体
        self.tmdb_info = info
        # 类型
        if isinstance(info.get('media_type'), MediaType):
            self.type = info.get('media_type')
        elif info.get('media_type'):
            self.type = MediaType.MOVIE if info.get("media_type") == "movie" else MediaType.TV
        else:
            self.type = MediaType.MOVIE if info.get("title") else MediaType.TV
        # TMDBID
        self.tmdb_id = info.get('id')
        if not self.tmdb_id:
            return
        # 额外ID
        if info.get("external_ids"):
            self.tvdb_id = info.get("external_ids", {}).get("tvdb_id")
            self.imdb_id = info.get("external_ids", {}).get("imdb_id")
        # 评分
        self.vote_average = round(float(info.get('vote_average')), 1) if info.get('vote_average') else 0
        # 描述
        self.overview = info.get('overview')
        # 风格
        self.genre_ids = info.get('genre_ids') or []
        # 原语种
        self.original_language = info.get('original_language')
        # 英文标题
        self.en_title = info.get('en_title')
        # 新加坡标题
        self.sg_title = info.get('sg_title')
        if self.type == MediaType.MOVIE:
            # 标题
            self.title = info.get('title')
            # 原标题
            self.original_title = info.get('original_title')
            # 发行日期
            self.release_date = info.get('release_date')
            if self.release_date:
                # 年份
                self.year = self.release_date[:4]
        else:
            # 电视剧
            self.title = info.get('name')
            # 原标题
            self.original_title = info.get('original_name')
            # 发行日期
            self.release_date = info.get('first_air_date')
            if self.release_date:
                # 年份
                self.year = self.release_date[:4]
            # 季集信息
            if info.get('seasons'):
                self.season_info = info.get('seasons')
                for seainfo in info.get('seasons'):
                    # 季
                    season = seainfo.get("season_number")
                    if not season:
                        continue
                    # 集
                    episode_count = seainfo.get("episode_count")
                    self.seasons[season] = list(range(1, episode_count + 1))
                    # 年份
                    air_date = seainfo.get("air_date")
                    if air_date:
                        self.season_years[season] = air_date[:4]
        # 海报
        if info.get('poster_path'):
            self.poster_path = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{info.get('poster_path')}"
        # 背景
        if info.get('backdrop_path'):
            self.backdrop_path = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{info.get('backdrop_path')}"
        # 导演和演员
        self.directors, self.actors = __directors_actors(info)
        # 别名和译名
        self.names = info.get('names') or []
        # 剩余属性赋值
        for key, value in info.items():
            if hasattr(self, key) and not getattr(self, key):
                setattr(self, key, value)

    def set_douban_info(self, info: dict):
        """
        初始化豆瓣信息
        """
        if not info:
            return
        # 来源
        self.source = "douban"
        # 本体
        self.douban_info = info
        # 豆瓣ID
        self.douban_id = str(info.get("id"))
        # 类型
        if not self.type:
            if isinstance(info.get('media_type'), MediaType):
                self.type = info.get('media_type')
            elif info.get("subtype"):
                self.type = MediaType.MOVIE if info.get("subtype") == "movie" else MediaType.TV
            elif info.get("target_type"):
                self.type = MediaType.MOVIE if info.get("target_type") == "movie" else MediaType.TV
            elif info.get("type_name"):
                self.type = MediaType(info.get("type_name"))
            elif info.get("uri"):
                self.type = MediaType.MOVIE if "/movie/" in info.get("uri") else MediaType.TV
            elif info.get("type") and info.get("type") in ["movie", "tv"]:
                self.type = MediaType.MOVIE if info.get("type") == "movie" else MediaType.TV
        # 标题
        if not self.title:
            self.title = info.get("title")
        # 英文标题，暂时不支持
        if not self.en_title:
            self.en_title = info.get('original_title')
        # 原语种标题
        if not self.original_title:
            self.original_title = info.get("original_title")
        # 年份
        if not self.year:
            self.year = info.get("year")[:4] if info.get("year") else None
            if not self.year and info.get("extra"):
                self.year = info.get("extra").get("year")
        # 识别标题中的季
        meta = MetaInfo(info.get("title"))
        # 季
        if not self.season:
            self.season = meta.begin_season
            if self.season:
                self.type = MediaType.TV
            elif not self.type:
                self.type = MediaType.MOVIE
        # 评分
        if not self.vote_average:
            rating = info.get("rating")
            if rating:
                vote_average = float(rating.get("value"))
            else:
                vote_average = 0
            self.vote_average = vote_average
        # 发行日期
        if not self.release_date:
            if info.get("release_date"):
                self.release_date = info.get("release_date")
            elif info.get("pubdate") and isinstance(info.get("pubdate"), list):
                release_date = info.get("pubdate")[0]
                if release_date:
                    match = re.search(r'\d{4}-\d{2}-\d{2}', release_date)
                    if match:
                        self.release_date = match.group()
        # 海报
        if not self.poster_path:
            if info.get("pic"):
                self.poster_path = info.get("pic", {}).get("large")
            if not self.poster_path and info.get("cover_url"):
                # imageView2/0/q/80/w/9999/h/120/format/webp ->  imageView2/1/w/500/h/750/format/webp
                self.poster_path = re.sub(r'imageView2/\d/q/\d+/w/\d+/h/\d+/format/webp', 'imageView2/1/w/500/h/750/format/webp', info.get("cover_url"))
            if not self.poster_path and info.get("cover"):
                if info.get("cover").get("url"):
                    self.poster_path = info.get("cover").get("url")
                else:
                    self.poster_path = info.get("cover").get("large", {}).get("url")
        # 简介
        if not self.overview:
            self.overview = info.get("intro") or info.get("card_subtitle") or ""
            if not self.overview:
                if info.get("extra", {}).get("info"):
                    extra_info = info.get("extra").get("info")
                    if extra_info:
                        self.overview = "，".join(["：".join(item) for item in extra_info])
        # 从简介中提取年份
        if self.overview and not self.year:
            match = re.search(r'\d{4}', self.overview)
            if match:
                self.year = match.group()
        # 导演和演员
        if not self.directors:
            self.directors = info.get("directors") or []
        if not self.actors:
            self.actors = info.get("actors") or []
        # 别名
        if not self.names:
            akas = info.get("aka")
            if akas:
                self.names = [re.sub(r'\([港台豆友译名]+\)', "", aka) for aka in akas]
        # 剧集
        if self.type == MediaType.TV and not self.seasons:
            meta = MetaInfo(info.get("title"))
            season = meta.begin_season or 1
            episodes_count = info.get("episodes_count")
            if episodes_count:
                self.seasons[season] = list(range(1, episodes_count + 1))
        # 季年份
        if self.type == MediaType.TV and not self.season_years:
            season = self.season or 1
            self.season_years = {
                season: self.year
            }
        # 风格
        if not self.genres:
            self.genres = [{"id": genre, "name": genre} for genre in info.get("genres") or []]
        # 时长
        if not self.runtime and info.get("durations"):
            # 查找数字
            match = re.search(r'\d+', info.get("durations")[0])
            if match:
                self.runtime = int(match.group())
        # 国家
        if not self.production_countries:
            self.production_countries = [{"id": country, "name": country} for country in info.get("countries") or []]
        # 剩余属性赋值
        for key, value in info.items():
            if not hasattr(self, key):
                setattr(self, key, value)

    def set_bangumi_info(self, info: dict):
        """
        初始化Bangumi信息
        """
        if not info:
            return
        # 来源
        self.source = "bangumi"
        # 本体
        self.bangumi_info = info
        # 豆瓣ID
        self.bangumi_id = info.get("id")
        # 类型
        if not self.type:
            self.type = MediaType.TV
        # 标题
        if not self.title:
            self.title = info.get("name_cn") or info.get("name")
        # 原语种标题
        if not self.original_title:
            self.original_title = info.get("name")
        # 识别标题中的季
        meta = MetaInfo(self.title)
        # 季
        if not self.season:
            self.season = meta.begin_season
        # 评分
        if not self.vote_average:
            rating = info.get("rating")
            if rating:
                vote_average = float(rating.get("score"))
            else:
                vote_average = 0
            self.vote_average = vote_average
        # 发行日期
        if not self.release_date:
            self.release_date = info.get("date") or info.get("air_date")
            # 年份
            if not self.year:
                self.year = self.release_date[:4] if self.release_date else None
        # 海报
        if not self.poster_path:
            if info.get("images"):
                self.poster_path = info.get("images", {}).get("large")
            if not self.poster_path and info.get("image"):
                self.poster_path = info.get("image")
        # 简介
        if not self.overview:
            self.overview = info.get("summary")
        # 别名
        if not self.names:
            infobox = info.get("infobox")
            if infobox:
                akas = [item.get("value") for item in infobox if item.get("key") == "别名"]
                if akas:
                    self.names = [aka.get("v") for aka in akas[0]]

        # 剧集
        if self.type == MediaType.TV and not self.seasons:
            meta = MetaInfo(self.title)
            season = meta.begin_season or 1
            episodes_count = info.get("total_episodes")
            if episodes_count:
                self.seasons[season] = list(range(1, episodes_count + 1))
        # 演员
        if not self.actors:
            self.actors = info.get("actors") or []

    @property
    def title_year(self):
        if self.title:
            return "%s (%s)" % (self.title, self.year) if self.year else self.title
        return ""

    @property
    def detail_link(self):
        """
        TMDB媒体详情页地址
        """
        if self.tmdb_id:
            if self.type == MediaType.MOVIE:
                return "https://www.themoviedb.org/movie/%s" % self.tmdb_id
            else:
                return "https://www.themoviedb.org/tv/%s" % self.tmdb_id
        elif self.douban_id:
            return "https://movie.douban.com/subject/%s" % self.douban_id
        elif self.bangumi_id:
            return "http://bgm.tv/subject/%s" % self.bangumi_id
        return ""

    @property
    def stars(self):
        """
        返回评分星星个数
        """
        if not self.vote_average:
            return ""
        return "".rjust(int(self.vote_average), "★")

    @property
    def vote_star(self):
        if self.vote_average:
            return "评分：%s" % self.stars
        return ""

    def get_backdrop_image(self, default: bool = False):
        """
        返回背景图片地址
        """
        if self.backdrop_path:
            return self.backdrop_path.replace("original", "w500")
        return default or ""

    def get_message_image(self, default: bool = None):
        """
        返回消息图片地址
        """
        if self.backdrop_path:
            return self.backdrop_path.replace("original", "w500")
        return self.get_poster_image(default=default)

    def get_poster_image(self, default: bool = None):
        """
        返回海报图片地址
        """
        if self.poster_path:
            return self.poster_path.replace("original", "w500")
        return default or ""

    def get_overview_string(self, max_len: int = 140):
        """
        返回带限定长度的简介信息
        :param max_len: 内容长度
        :return:
        """
        overview = str(self.overview).strip()
        placeholder = ' ...'
        max_len = max(len(placeholder), max_len - len(placeholder))
        overview = (overview[:max_len] + placeholder) if len(overview) > max_len else overview
        return overview

    def to_dict(self):
        """
        返回字典
        """
        dicts = asdict(self)
        dicts["type"] = self.type.value if self.type else None
        dicts["detail_link"] = self.detail_link
        dicts["title_year"] = self.title_year
        dicts["tmdb_info"] = None
        dicts["douban_info"] = None
        dicts["bangumi_info"] = None
        return dicts

    def clear(self):
        """
        去除多余数据，减小体积
        """
        self.tmdb_info = {}
        self.douban_info = {}
        self.bangumi_info = {}
        self.seasons = {}
        self.genres = []
        self.season_info = []
        self.names = []
        self.actors = []
        self.directors = []
        self.production_companies = []
        self.production_countries = []
        self.spoken_languages = []
        self.networks = []
        self.next_episode_to_air = {}


@dataclass
class Context:
    """
    上下文对象
    """

    # 识别信息
    meta_info: MetaBase = None
    # 媒体信息
    media_info: MediaInfo = None
    # 种子信息
    torrent_info: TorrentInfo = None

    def to_dict(self):
        """
        转换为字典
        """
        return {
            "meta_info": self.meta_info.to_dict() if self.meta_info else None,
            "torrent_info": self.torrent_info.to_dict() if self.torrent_info else None,
            "media_info": self.media_info.to_dict() if self.media_info else None
        }
