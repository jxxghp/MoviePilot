import secrets
from pathlib import Path

from pydantic import BaseSettings


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
    # 网络代理 IP:PORT
    PROXY_HOST: str = None
    # 媒体信息搜索来源
    SEARCH_SOURCE: str = "themoviedb"
    # 刮削入库的媒体文件
    SCRAP_METADATA: bool = True
    # 刮削来源
    SCRAP_SOURCE: str = "themoviedb"
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
    # 用户认证站点 hhclub/audiences/hddolby/zmpt/freefarm/hdfans/wintersakura/leaves/1ptba/icc2022/iyuu
    AUTH_SITE: str = ""
    # 消息通知渠道 telegram/wechat/slack
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
    # 下载器 qbittorrent/transmission
    DOWNLOADER: str = "qbittorrent"
    # Qbittorrent地址，IP:PORT
    QB_HOST: str = None
    # Qbittorrent用户名
    QB_USER: str = None
    # Qbittorrent密码
    QB_PASSWORD: str = None
    # Transmission地址，IP:PORT
    TR_HOST: str = None
    # Transmission用户名
    TR_USER: str = None
    # Transmission密码
    TR_PASSWORD: str = None
    # 种子标签
    TORRENT_TAG: str = "MOVIEPILOT"
    # 下载保存目录，容器内映射路径需要一致
    DOWNLOAD_PATH: str = "/downloads"
    # 电影下载保存目录，容器内映射路径需要一致
    DOWNLOAD_MOVIE_PATH: str = None
    # 电视剧下载保存目录，容器内映射路径需要一致
    DOWNLOAD_TV_PATH: str = None
    # 下载目录二级分类
    DOWNLOAD_CATEGORY: bool = False
    # 下载站点字幕
    DOWNLOAD_SUBTITLE: bool = True
    # 媒体服务器 emby/jellyfin/plex
    MEDIASERVER: str = "emby"
    # 入库刷新媒体库
    REFRESH_MEDIASERVER: bool = True
    # 媒体服务器同步间隔（小时）
    MEDIASERVER_SYNC_INTERVAL: int = 6
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
    COOKIECLOUD_HOST: str = "https://nastool.org/cookiecloud"
    # CookieCloud用户KEY
    COOKIECLOUD_KEY: str = None
    # CookieCloud端对端加密密码
    COOKIECLOUD_PASSWORD: str = None
    # CookieCloud同步间隔（分钟）
    COOKIECLOUD_INTERVAL: int = 60 * 24
    # CookieCloud对应的浏览器UA
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36 Edg/113.0.1774.57"
    # 媒体库目录
    LIBRARY_PATH: str = None
    # 电影媒体库目录名，默认"电影"
    LIBRARY_MOVIE_NAME: str = None
    # 电视剧媒体库目录名，默认"电视剧"
    LIBRARY_TV_NAME: str = None
    # 二级分类
    LIBRARY_CATEGORY: bool = True
    # 电影重命名格式
    MOVIE_RENAME_FORMAT: str = "{{title}}{% if year %} ({{year}}){% endif %}" \
                               "/{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}" \
                               "{{fileExt}}"
    # 电视剧重命名格式
    TV_RENAME_FORMAT: str = "{{title}}{% if year %} ({{year}}){% endif %}" \
                            "/Season {{season}}" \
                            "/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}" \
                            "{{fileExt}}"
    # 大内存模式
    BIG_MEMORY_MODE: bool = False

    @property
    def INNER_CONFIG_PATH(self):
        return self.ROOT_PATH / "config"

    @property
    def CONFIG_PATH(self):
        if self.CONFIG_DIR:
            return Path(self.CONFIG_DIR)
        return self.INNER_CONFIG_PATH

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

    def __init__(self):
        super().__init__()
        with self.CONFIG_PATH as p:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
        with self.TEMP_PATH as p:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
        with self.LOG_PATH as p:
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)

    class Config:
        case_sensitive = True


settings = Settings()
