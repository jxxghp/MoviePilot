from typing import Optional, Any, List

from app.core.config import settings
from app.core.meta import MetaBase
from app.core.meta_info import MetaInfo
from app.utils.types import MediaType


class TorrentInfo(object):
    # 站点ID
    site: int = None
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

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key) and value is not None:
                setattr(self, key, value)

    def __getattr__(self, attribute):
        return None

    def __setattr__(self, name: str, value: Any):
        self.__dict__[name] = value

    @staticmethod
    def get_free_string(upload_volume_factor, download_volume_factor):
        """
        计算促销类型
        """
        if upload_volume_factor is None or download_volume_factor is None:
            return "未知"
        free_strs = {
            "1.0 1.0": "普通",
            "1.0 0.0": "免费",
            "2.0 1.0": "2X",
            "2.0 0.0": "2X免费",
            "1.0 0.5": "50%",
            "2.0 0.5": "2X 50%",
            "1.0 0.7": "70%",
            "1.0 0.3": "30%"
        }
        return free_strs.get('%.1f %.1f' % (upload_volume_factor, download_volume_factor), "未知")

    def get_volume_factor_string(self):
        """
        返回促销信息
        """
        return self.get_free_string(self.uploadvolumefactor, self.downloadvolumefactor)


class MediaInfo(object):
    # 类型 电影、电视剧
    type: MediaType = None
    # 媒体标题
    title: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # TMDB ID
    tmdb_id: Optional[str] = None
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
    # 各季的剧集清单信息
    seasons: Optional[dict] = {}
    # 二级分类
    category: str = ""
    # TMDB INFO
    tmdb_info: Optional[dict] = {}
    # 豆瓣 INFO
    douban_info: Optional[dict] = {}
    # 导演
    directors: List[dict] = []
    # 演员
    actors: List[dict] = []

    def __init__(self, tmdb_info: dict = None, douban_info: dict = None):
        if tmdb_info:
            self.set_tmdb_info(tmdb_info)
        if douban_info:
            self.set_douban_info(douban_info)

    def __getattr__(self, attribute):
        return None

    def __setattr__(self, name: str, value: Any):
        self.__dict__[name] = value

    def set_image(self, name: str, image: str):
        """
        设置图片地址
        """
        setattr(self, f"{name}_path", image)

    def set_category(self, cat: str):
        """
        设置二级分类
        """
        self.category = cat

    def set_tmdb_info(self, info: dict):
        """
        初始化媒信息
        """

        def __directors_actors(tmdbinfo: dict):
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
            for cast in _credits.get("cast"):
                if cast.get("known_for_department") == "Acting":
                    actors.append(cast)
            for crew in _credits.get("crew"):
                if crew.get("job") == "Director":
                    directors.append(crew)
            return directors, actors

        if not info:
            return
        # 本体
        self.tmdb_info = info
        # 类型
        self.type = info.get('media_type')
        # TMDBID
        self.tmdb_id = str(info.get('id'))
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
        # 原语种
        self.original_language = info.get('original_language')
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
                for season_info in info.get('seasons'):
                    if not season_info.get("season_number"):
                        continue
                    episode_count = season_info.get("episode_count")
                    self.seasons[season_info.get("season_number")] = list(range(1, episode_count + 1))
        # 海报
        if info.get('poster_path'):
            self.poster_path = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{info.get('poster_path')}"
        # 背景
        if info.get('backdrop_path'):
            self.backdrop_path = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{info.get('backdrop_path')}"
        # 导演和演员
        self.directors, self.actors = __directors_actors(info)

    def set_douban_info(self, info: dict):
        """
        初始化豆瓣信息
        """
        if not info:
            return
        # 本体
        self.douban_info = info
        # 豆瓣ID
        self.douban_id = str(info.get("id"))
        # 评分
        if not self.vote_average:
            rating = info.get('rating')
            if rating:
                vote_average = float(rating.get("value"))
            else:
                vote_average = 0
            self.vote_average = vote_average
        # 标题
        if not self.title:
            self.title = info.get('title')
        # 年份
        if not self.year:
            self.year = info.get('year')[:4] if info.get('year') else None
        # 原语种标题
        if not self.original_title:
            self.original_title = info.get("original_title")
        # 类型
        if not self.type:
            self.type = MediaType.MOVIE if info.get("type") == "movie" else MediaType.TV
        if not self.poster_path:
            if self.type == MediaType.MOVIE:
                # 海报
                poster_path = info.get('cover', {}).get("url")
                if not poster_path:
                    poster_path = info.get('cover_url')
                if not poster_path:
                    poster_path = info.get('pic', {}).get("large")
            else:
                # 海报
                poster_path = info.get('pic', {}).get("normal")
            self.poster_path = poster_path
        # 简介
        if not self.overview:
            overview = info.get("card_subtitle") or ""
            if not self.year and overview:
                if overview.split("/")[0].strip().isdigit():
                    self.year = overview.split("/")[0].strip()

    def get_detail_url(self):
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
        return ""

    def get_stars(self):
        """
        返回评分星星个数
        """
        if not self.vote_average:
            return ""
        return "".rjust(int(self.vote_average), "★")

    def get_star_string(self):
        if self.vote_average:
            return "评分：%s" % self.get_stars()
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

    def get_title_string(self):
        if self.title:
            return "%s (%s)" % (self.title, self.year) if self.year else self.title
        return ""

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

    def get_season_episodes(self, sea: int) -> list:
        """
        返回指定季度的剧集信息
        """
        if not self.seasons:
            return []
        return self.seasons.get(sea) or []


class Context(object):
    """
    上下文对象
    """
    # 识别前的信息
    title: Optional[str] = None
    subtitle: Optional[str] = None

    # 用户信息
    userid: Optional[str] = None
    username: Optional[str] = None

    # 操作类型
    action: Optional[str] = None

    # 识别信息
    _meta_info: Optional[MetaBase] = None
    # 种子信息
    _torrent_info: Optional[TorrentInfo] = None
    # 媒体信息
    _media_info: Optional[MediaInfo] = None

    def __init__(self,
                 meta: MetaBase = None,
                 mediainfo: MediaInfo = None,
                 torrentinfo: TorrentInfo = None,
                 **kwargs):
        if meta:
            self._meta_info = meta
        if mediainfo:
            self._media_info = mediainfo
        if torrentinfo:
            self._torrent_info = torrentinfo
        if kwargs:
            for k, v in kwargs.items():
                setattr(self, k, v)

    @property
    def meta_info(self):
        return self._meta_info

    def set_meta_info(self, title: str, subtitle: str = None):
        self._meta_info = MetaInfo(title, subtitle)

    @property
    def media_info(self):
        return self._media_info

    def set_media_info(self,
                       tmdb_info: dict = None,
                       douban_info: dict = None):
        self._media_info = MediaInfo(tmdb_info, douban_info)

    @property
    def torrent_info(self):
        return self._torrent_info

    def set_torrent_info(self, info: dict):
        self._torrent_info = TorrentInfo(**info)

    def __getattr__(self, attribute):
        return None

    def __setattr__(self, name: str, value: Any):
        self.__dict__[name] = value

    def to_dict(self):
        """
        转换为字典
        """
        def object_to_dict(obj):
            attributes = [
                attr for attr in dir(obj)
                if not callable(getattr(obj, attr)) and not attr.startswith("_")
            ]
            return {
                attr: getattr(obj, attr).value
                if isinstance(getattr(obj, attr), MediaType)
                else getattr(obj, attr) for attr in attributes
            }

        return {
            "meta_info": object_to_dict(self.meta_info),
            "media_info": object_to_dict(self.media_info)
        }
