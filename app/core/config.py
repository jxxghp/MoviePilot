import secrets
import sys
from pathlib import Path
from typing import List, Optional

from pydantic import BaseSettings

from app.utils.system import SystemUtils


class Settings(BaseSettings):
    # 项目名称
    PROJECT_NAME = "MoviePilot"
    # API路径
    API_V1_STR: str = "/api/v1"
    # 密钥
    SECRET_KEY: str = secrets.token_urlsafe(32)
    # 允许的域名
    ALLOWED_HOSTS: list = ["*"]
    # TOKEN过期时间
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    # 时区
    TZ: str = "Asia/Shanghai"
    # API监听地址
    HOST: str = "0.0.0.0"
    # API监听端口
    PORT: int = 3001
    # 前端监听端口
    NGINX_PORT: int = 3000
    # 是否调试模式
    DEBUG: bool = False
    # 是否开发模式
    DEV: bool = False
    # 配置文件目录
    CONFIG_DIR: str = None
    # 超级管理员
    SUPERUSER: str = "admin"
    # 超级管理员初始密码
    SUPERUSER_PASSWORD: str = "password"
    # API密钥，需要更换
    API_TOKEN: str = "moviepilot"
    # 登录页面电影海报,tmdb/bing
    WALLPAPER: str = "tmdb"
    # 网络代理 IP:PORT
    PROXY_HOST: str = None
    # 媒体识别来源 themoviedb/douban
    RECOGNIZE_SOURCE: str = "themoviedb"
    # 刮削来源 themoviedb/douban
    SCRAP_SOURCE: str = "themoviedb"
    # 刮削入库的媒体文件
    SCRAP_METADATA: bool = True
    # 新增已入库媒体是否跟随TMDB信息变化
    SCRAP_FOLLOW_TMDB: bool = True
    # TMDB图片地址
    TMDB_IMAGE_DOMAIN: str = "image.tmdb.org"
    # TMDB API地址
    TMDB_API_DOMAIN: str = "api.themoviedb.org"
    # TMDB API Key
    TMDB_API_KEY: str = "db55323b8d3e4154498498a75642b381"
    # TVDB API Key
    TVDB_API_KEY: str = "6b481081-10aa-440c-99f2-21d17717ee02"
    # Fanart API Key
    FANART_API_KEY: str = "d2d31f9ecabea050fc7d68aa3146015f"
    # 支持的后缀格式
    RMT_MEDIAEXT: list = ['.mp4', '.mkv', '.ts', '.iso',
                          '.rmvb', '.avi', '.mov', '.mpeg',
                          '.mpg', '.wmv', '.3gp', '.asf',
                          '.m4v', '.flv', '.m2ts', '.strm',
                          '.tp']
    # 支持的字幕文件后缀格式
    RMT_SUBEXT: list = ['.srt', '.ass', '.ssa']
    # 支持的音轨文件后缀格式
    RMT_AUDIO_TRACK_EXT: list = ['.mka']
    # 索引器
    INDEXER: str = "builtin"
    # 订阅模式
    SUBSCRIBE_MODE: str = "spider"
    # RSS订阅模式刷新时间间隔（分钟）
    SUBSCRIBE_RSS_INTERVAL: int = 30
    # 订阅搜索开关
    SUBSCRIBE_SEARCH: bool = False
    # 用户认证站点
    AUTH_SITE: str = ""
    # 交互搜索自动下载用户ID，使用,分割
    AUTO_DOWNLOAD_USER: str = None
    # 消息通知渠道 telegram/wechat/slack，多个通知渠道用,分隔
    MESSAGER: str = "telegram"
    # WeChat企业ID
    WECHAT_CORPID: str = None
    # WeChat应用Secret
    WECHAT_APP_SECRET: str = None
    # WeChat应用ID
    WECHAT_APP_ID: str = None
    # WeChat代理服务器
    WECHAT_PROXY: str = "https://qyapi.weixin.qq.com"
    # WeChat Token
    WECHAT_TOKEN: str = None
    # WeChat EncodingAESKey
    WECHAT_ENCODING_AESKEY: str = None
    # WeChat 管理员
    WECHAT_ADMINS: str = None
    # Telegram Bot Token
    TELEGRAM_TOKEN: str = None
    # Telegram Chat ID
    TELEGRAM_CHAT_ID: str = None
    # Telegram 用户ID，使用,分隔
    TELEGRAM_USERS: str = ""
    # Telegram 管理员ID，使用,分隔
    TELEGRAM_ADMINS: str = ""
    # Slack Bot User OAuth Token
    SLACK_OAUTH_TOKEN: str = ""
    # Slack App-Level Token
    SLACK_APP_TOKEN: str = ""
    # Slack 频道名称
    SLACK_CHANNEL: str = ""
    # SynologyChat Webhook
    SYNOLOGYCHAT_WEBHOOK: str = ""
    # SynologyChat Token
    SYNOLOGYCHAT_TOKEN: str = ""
    # 下载器 qbittorrent/transmission
    DOWNLOADER: str = "qbittorrent"
    # 下载器监控开关
    DOWNLOADER_MONITOR: bool = True
    # Qbittorrent地址，IP:PORT
    QB_HOST: str = None
    # Qbittorrent用户名
    QB_USER: str = None
    # Qbittorrent密码
    QB_PASSWORD: str = None
    # Qbittorrent分类自动管理
    QB_CATEGORY: bool = False
    # Qbittorrent按顺序下载
    QB_SEQUENTIAL: bool = True
    # Qbittorrent忽略队列限制，强制继续
    QB_FORCE_RESUME: bool = False
    # Transmission地址，IP:PORT
    TR_HOST: str = None
    # Transmission用户名
    TR_USER: str = None
    # Transmission密码
    TR_PASSWORD: str = None
    # 种子标签
    TORRENT_TAG: str = "MOVIEPILOT"
    # 下载保存目录，容器内映射路径需要一致
    DOWNLOAD_PATH: str = None
    # 电影下载保存目录，容器内映射路径需要一致
    DOWNLOAD_MOVIE_PATH: str = None
    # 电视剧下载保存目录，容器内映射路径需要一致
    DOWNLOAD_TV_PATH: str = None
    # 动漫下载保存目录，容器内映射路径需要一致
    DOWNLOAD_ANIME_PATH: str = None
    # 下载目录二级分类
    DOWNLOAD_CATEGORY: bool = False
    # 下载站点字幕
    DOWNLOAD_SUBTITLE: bool = True
    # 媒体服务器 emby/jellyfin/plex，多个媒体服务器,分割
    MEDIASERVER: str = "emby"
    # 媒体服务器同步间隔（小时）
    MEDIASERVER_SYNC_INTERVAL: Optional[int] = 6
    # 媒体服务器同步黑名单，多个媒体库名称,分割
    MEDIASERVER_SYNC_BLACKLIST: str = None
    # EMBY服务器地址，IP:PORT
    EMBY_HOST: str = None
    # EMBY Api Key
    EMBY_API_KEY: str = None
    # Jellyfin服务器地址，IP:PORT
    JELLYFIN_HOST: str = None
    # Jellyfin Api Key
    JELLYFIN_API_KEY: str = None
    # Plex服务器地址，IP:PORT
    PLEX_HOST: str = None
    # Plex Token
    PLEX_TOKEN: str = None
    # 转移方式 link/copy/move/softlink
    TRANSFER_TYPE: str = "copy"
    # CookieCloud服务器地址
    COOKIECLOUD_HOST: str = "https://movie-pilot.org/cookiecloud"
    # CookieCloud用户KEY
    COOKIECLOUD_KEY: str = None
    # CookieCloud端对端加密密码
    COOKIECLOUD_PASSWORD: str = None
    # CookieCloud同步间隔（分钟）
    COOKIECLOUD_INTERVAL: Optional[int] = 60 * 24
    # OCR服务器地址
    OCR_HOST: str = "https://movie-pilot.org"
    # CookieCloud对应的浏览器UA
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36 Edg/113.0.1774.57"
    # 媒体库目录，多个目录使用,分隔
    LIBRARY_PATH: str = None
    # 电影媒体库目录名，默认"电影"
    LIBRARY_MOVIE_NAME: str = None
    # 电视剧媒体库目录名，默认"电视剧"
    LIBRARY_TV_NAME: str = None
    # 动漫媒体库目录名，默认"电视剧/动漫"
    LIBRARY_ANIME_NAME: str = None
    # 二级分类
    LIBRARY_CATEGORY: bool = True
    # 电视剧动漫的分类genre_ids
    ANIME_GENREIDS = [16]
    # 电影重命名格式
    MOVIE_RENAME_FORMAT: str = "{{title}}{% if year %} ({{year}}){% endif %}" \
                               "/{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}" \
                               "{{fileExt}}"
    # 电视剧重命名格式
    TV_RENAME_FORMAT: str = "{{title}}{% if year %} ({{year}}){% endif %}" \
                            "/Season {{season}}" \
                            "/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}" \
                            "{{fileExt}}"
    # 转移时覆盖模式
    OVERWRITE_MODE: str = "size"
    # 大内存模式
    BIG_MEMORY_MODE: bool = False
    # 插件市场仓库地址，多个地址使用,分隔，地址以/结尾
    PLUGIN_MARKET: str = "https://github.com/jxxghp/MoviePilot-Plugins"
    # Github token，提高请求api限流阈值 ghp_****
    GITHUB_TOKEN: str = None
    # 自动检查和更新站点资源包（站点索引、认证等）
    AUTO_UPDATE_RESOURCE: bool = True

    @property
    def INNER_CONFIG_PATH(self):
        return self.ROOT_PATH / "config"

    @property
    def CONFIG_PATH(self):
        if self.CONFIG_DIR:
            return Path(self.CONFIG_DIR)
        elif SystemUtils.is_docker():
            return Path("/config")
        elif SystemUtils.is_frozen():
            return Path(sys.executable).parent / "config"
        return self.ROOT_PATH / "config"

    @property
    def TEMP_PATH(self):
        return self.CONFIG_PATH / "temp"

    @property
    def ROOT_PATH(self):
        return Path(__file__).parents[2]

    @property
    def PLUGIN_DATA_PATH(self):
        return self.CONFIG_PATH / "plugins"

    @property
    def LOG_PATH(self):
        return self.CONFIG_PATH / "logs"

    @property
    def CACHE_CONF(self):
        if self.BIG_MEMORY_MODE:
            return {
                "tmdb": 1024,
                "refresh": 50,
                "torrents": 100,
                "douban": 512,
                "fanart": 512,
                "meta": 15 * 24 * 3600
            }
        return {
            "tmdb": 256,
            "refresh": 30,
            "torrents": 50,
            "douban": 256,
            "fanart": 128,
            "meta": 7 * 24 * 3600
        }

    @property
    def PROXY(self):
        if self.PROXY_HOST:
            return {
                "http": self.PROXY_HOST,
                "https": self.PROXY_HOST,
            }
        return None

    @property
    def PROXY_SERVER(self):
        if self.PROXY_HOST:
            return {
                "server": self.PROXY_HOST
            }

    @property
    def LIBRARY_PATHS(self) -> List[Path]:
        if self.LIBRARY_PATH:
            return [Path(path) for path in self.LIBRARY_PATH.split(",")]
        return [self.CONFIG_PATH / "library"]

    @property
    def SAVE_PATH(self) -> Path:
        """
        获取下载保存目录
        """
        if self.DOWNLOAD_PATH:
            return Path(self.DOWNLOAD_PATH)
        return self.CONFIG_PATH / "downloads"

    @property
    def SAVE_MOVIE_PATH(self) -> Path:
        """
        获取电影下载保存目录
        """
        if self.DOWNLOAD_MOVIE_PATH:
            return Path(self.DOWNLOAD_MOVIE_PATH)
        return self.SAVE_PATH

    @property
    def SAVE_TV_PATH(self) -> Path:
        """
        获取电视剧下载保存目录
        """
        if self.DOWNLOAD_TV_PATH:
            return Path(self.DOWNLOAD_TV_PATH)
        return self.SAVE_PATH

    @property
    def SAVE_ANIME_PATH(self) -> Path:
        """
        获取动漫下载保存目录
        """
        if self.DOWNLOAD_ANIME_PATH:
            return Path(self.DOWNLOAD_ANIME_PATH)
        return self.SAVE_TV_PATH

    @property
    def GITHUB_HEADERS(self):
        """
        Github请求头
        """
        if self.GITHUB_TOKEN:
            return {
                "Authorization": f"Bearer {self.GITHUB_TOKEN}"
            }
        return {}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        with self.CONFIG_PATH as p:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
            if SystemUtils.is_frozen():
                if not (p / "app.env").exists():
                    SystemUtils.copy(self.INNER_CONFIG_PATH / "app.env", p / "app.env")
        with self.TEMP_PATH as p:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
        with self.LOG_PATH as p:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)

    class Config:
        case_sensitive = True


settings = Settings(
    _env_file=Settings().CONFIG_PATH / "app.env",
    _env_file_encoding="utf-8"
)
