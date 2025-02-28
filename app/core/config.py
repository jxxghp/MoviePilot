import copy
import os
import re
import secrets
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from dotenv import set_key
from pydantic import BaseModel, BaseSettings, validator, Field

from app.log import logger, log_settings, LogConfigModel
from app.utils.system import SystemUtils
from app.utils.url import UrlUtils


class ConfigModel(BaseModel):
    """
    Pydantic 配置模型，描述所有配置项及其类型和默认值
    """

    class Config:
        extra = "ignore"  # 忽略未定义的配置项

    # 项目名称
    PROJECT_NAME = "MoviePilot"
    # 域名 格式；https://movie-pilot.org
    APP_DOMAIN: str = ""
    # API路径
    API_V1_STR: str = "/api/v1"
    # 前端资源路径
    FRONTEND_PATH: str = "/public"
    # 密钥
    SECRET_KEY: str = secrets.token_urlsafe(32)
    # RESOURCE密钥
    RESOURCE_SECRET_KEY: str = secrets.token_urlsafe(32)
    # 允许的域名
    ALLOWED_HOSTS: list = Field(default_factory=lambda: ["*"])
    # TOKEN过期时间
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    # RESOURCE_TOKEN过期时间
    RESOURCE_ACCESS_TOKEN_EXPIRE_SECONDS: int = 60 * 30
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
    # 是否在控制台输出 SQL 语句，默认关闭
    DB_ECHO: bool = False
    # 数据库连接池类型，QueuePool, NullPool
    DB_POOL_TYPE: str = "QueuePool"
    # 是否在获取连接时进行预先 ping 操作，默认关闭
    DB_POOL_PRE_PING: bool = False
    # 数据库连接池的大小，默认 100
    DB_POOL_SIZE: int = 100
    # 数据库连接的回收时间（秒），默认 1800 秒
    DB_POOL_RECYCLE: int = 1800
    # 数据库连接池获取连接的超时时间（秒），默认 60 秒
    DB_POOL_TIMEOUT: int = 60
    # 数据库连接池最大溢出连接数，默认 500
    DB_MAX_OVERFLOW: int = 500
    # SQLite 的 busy_timeout 参数，默认为 60 秒
    DB_TIMEOUT: int = 60
    # SQLite 是否启用 WAL 模式，默认关闭
    DB_WAL_ENABLE: bool = False
    # 缓存类型，支持 cachetools 和 redis，默认使用 cachetools
    CACHE_BACKEND_TYPE: str = "cachetools"
    # 缓存连接字符串，仅外部缓存（如 Redis、Memcached）需要
    CACHE_BACKEND_URL: Optional[str] = None
    # Redis 缓存最大内存限制，未配置时，如开启大内存模式时为 "1024mb"，未开启时为 "256mb"
    CACHE_REDIS_MAXMEMORY: Optional[str] = None
    # 配置文件目录
    CONFIG_DIR: Optional[str] = None
    # 超级管理员
    SUPERUSER: str = "admin"
    # 辅助认证，允许通过外部服务进行认证、单点登录以及自动创建用户
    AUXILIARY_AUTH_ENABLE: bool = False
    # API密钥，需要更换
    API_TOKEN: Optional[str] = None
    # 网络代理 IP:PORT
    PROXY_HOST: Optional[str] = None
    # 登录页面电影海报,tmdb/bing/mediaserver
    WALLPAPER: str = "tmdb"
    # 媒体搜索来源 themoviedb/douban/bangumi，多个用,分隔
    SEARCH_SOURCE: str = "themoviedb,douban,bangumi"
    # 媒体识别来源 themoviedb/douban
    RECOGNIZE_SOURCE: str = "themoviedb"
    # 刮削来源 themoviedb/douban
    SCRAP_SOURCE: str = "themoviedb"
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
    # Fanart开关
    FANART_ENABLE: bool = True
    # Fanart API Key
    FANART_API_KEY: str = "d2d31f9ecabea050fc7d68aa3146015f"
    # 元数据识别缓存过期时间（小时）
    META_CACHE_EXPIRE: int = 0
    # 电视剧动漫的分类genre_ids
    ANIME_GENREIDS = [16]
    # 用户认证站点
    AUTH_SITE: str = ""
    # 自动检查和更新站点资源包（站点索引、认证等）
    AUTO_UPDATE_RESOURCE: bool = True
    # 是否启用DOH解析域名
    DOH_ENABLE: bool = False
    # 使用 DOH 解析的域名列表
    DOH_DOMAINS: str = ("api.themoviedb.org,"
                        "api.tmdb.org,"
                        "webservice.fanart.tv,"
                        "api.github.com,"
                        "github.com,"
                        "raw.githubusercontent.com,"
                        "api.telegram.org")
    # DOH 解析服务器列表
    DOH_RESOLVERS: str = "1.0.0.1,1.1.1.1,9.9.9.9,149.112.112.112"
    # 支持的后缀格式
    RMT_MEDIAEXT: list = Field(
        default_factory=lambda: ['.mp4', '.mkv', '.ts', '.iso',
                                 '.rmvb', '.avi', '.mov', '.mpeg',
                                 '.mpg', '.wmv', '.3gp', '.asf',
                                 '.m4v', '.flv', '.m2ts', '.strm',
                                 '.tp', '.f4v']
    )
    # 支持的字幕文件后缀格式
    RMT_SUBEXT: list = Field(default_factory=lambda: ['.srt', '.ass', '.ssa', '.sup'])
    # 支持的音轨文件后缀格式
    RMT_AUDIO_TRACK_EXT: list = Field(default_factory=lambda: ['.mka'])
    # 音轨文件后缀格式
    RMT_AUDIOEXT: list = Field(
        default_factory=lambda: ['.aac', '.ac3', '.amr', '.caf', '.cda', '.dsf',
                                 '.dff', '.kar', '.m4a', '.mp1', '.mp2', '.mp3',
                                 '.mid', '.mod', '.mka', '.mpc', '.nsf', '.ogg',
                                 '.pcm', '.rmi', '.s3m', '.snd', '.spx', '.tak',
                                 '.tta', '.vqf', '.wav', '.wma',
                                 '.aifc', '.aiff', '.alac', '.adif', '.adts',
                                 '.flac', '.midi', '.opus', '.sfalc']
    )
    # 下载器临时文件后缀
    DOWNLOAD_TMPEXT: list = Field(default_factory=lambda: ['.!qb', '.part'])
    # 媒体服务器同步间隔（小时）
    MEDIASERVER_SYNC_INTERVAL: int = 6
    # 订阅模式
    SUBSCRIBE_MODE: str = "spider"
    # RSS订阅模式刷新时间间隔（分钟）
    SUBSCRIBE_RSS_INTERVAL: int = 30
    # 订阅数据共享
    SUBSCRIBE_STATISTIC_SHARE: bool = True
    # 订阅搜索开关
    SUBSCRIBE_SEARCH: bool = False
    # 检查本地媒体库是否存在资源开关
    LOCAL_EXISTS_SEARCH: bool = False
    # 搜索多个名称
    SEARCH_MULTIPLE_NAME: bool = False
    # 站点数据刷新间隔（小时）
    SITEDATA_REFRESH_INTERVAL: int = 6
    # 读取和发送站点消息
    SITE_MESSAGE: bool = True
    # 种子标签
    TORRENT_TAG: str = "MOVIEPILOT"
    # 下载站点字幕
    DOWNLOAD_SUBTITLE: bool = True
    # 交互搜索自动下载用户ID，使用,分割
    AUTO_DOWNLOAD_USER: Optional[str] = None
    # CookieCloud是否启动本地服务
    COOKIECLOUD_ENABLE_LOCAL: Optional[bool] = False
    # CookieCloud服务器地址
    COOKIECLOUD_HOST: str = "https://movie-pilot.org/cookiecloud"
    # CookieCloud用户KEY
    COOKIECLOUD_KEY: Optional[str] = None
    # CookieCloud端对端加密密码
    COOKIECLOUD_PASSWORD: Optional[str] = None
    # CookieCloud同步间隔（分钟）
    COOKIECLOUD_INTERVAL: Optional[int] = 60 * 24
    # CookieCloud同步黑名单，多个域名,分割
    COOKIECLOUD_BLACKLIST: Optional[str] = None
    # CookieCloud对应的浏览器UA
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36 Edg/113.0.1774.57"
    # 电影重命名格式
    MOVIE_RENAME_FORMAT: str = "{{title}}{% if year %} ({{year}}){% endif %}" \
                               "/{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}" \
                               "{{fileExt}}"
    # 电视剧重命名格式
    TV_RENAME_FORMAT: str = "{{title}}{% if year %} ({{year}}){% endif %}" \
                            "/Season {{season}}" \
                            "/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}" \
                            "{{fileExt}}"
    # OCR服务器地址
    OCR_HOST: str = "https://movie-pilot.org"
    # 服务器地址，对应 https://github.com/jxxghp/MoviePilot-Server 项目
    MP_SERVER_HOST: str = "https://movie-pilot.org"
    # 插件市场仓库地址，多个地址使用,分隔，地址以/结尾
    PLUGIN_MARKET: str = ("https://github.com/jxxghp/MoviePilot-Plugins,"
                          "https://github.com/thsrite/MoviePilot-Plugins,"
                          "https://github.com/honue/MoviePilot-Plugins,"
                          "https://github.com/InfinityPacer/MoviePilot-Plugins")
    # 插件安装数据共享
    PLUGIN_STATISTIC_SHARE: bool = True
    # 是否开启插件热加载
    PLUGIN_AUTO_RELOAD: bool = False
    # Github token，提高请求api限流阈值 ghp_****
    GITHUB_TOKEN: Optional[str] = None
    # Github代理服务器，格式：https://mirror.ghproxy.com/
    GITHUB_PROXY: Optional[str] = ''
    # pip镜像站点，格式：https://pypi.tuna.tsinghua.edu.cn/simple
    PIP_PROXY: Optional[str] = ''
    # 指定的仓库Github token，多个仓库使用,分隔，格式：{user1}/{repo1}:ghp_****,{user2}/{repo2}:github_pat_****
    REPO_GITHUB_TOKEN: Optional[str] = None
    # 大内存模式
    BIG_MEMORY_MODE: bool = False
    # 全局图片缓存，将媒体图片缓存到本地
    GLOBAL_IMAGE_CACHE: bool = False
    # 是否启用编码探测的性能模式
    ENCODING_DETECTION_PERFORMANCE_MODE: bool = True
    # 编码探测的最低置信度阈值
    ENCODING_DETECTION_MIN_CONFIDENCE: float = 0.8
    # 允许的图片缓存域名
    SECURITY_IMAGE_DOMAINS: List[str] = Field(
        default_factory=lambda: ["image.tmdb.org",
                                 "static-mdb.v.geilijiasu.com",
                                 "doubanio.com",
                                 "lain.bgm.tv",
                                 "raw.githubusercontent.com",
                                 "github.com",
                                 "thetvdb.com",
                                 "cctvpic.com",
                                 "iqiyipic.com",
                                 "hdslb.com",
                                 "cmvideo.cn",
                                 "ykimg.com",
                                 "qpic.cn"]
    )
    # 允许的图片文件后缀格式
    SECURITY_IMAGE_SUFFIXES: List[str] = Field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif"]
    )
    # 重命名时支持的S0别名
    RENAME_FORMAT_S0_NAMES: List[str] = Field(
        default_factory=lambda: ["Specials", "SPs"]
    )
    # 启用分词搜索
    TOKENIZED_SEARCH: bool = False
    # 为指定默认字幕添加.default后缀
    DEFAULT_SUB: Optional[str] = "zh-cn"


class Settings(BaseSettings, ConfigModel, LogConfigModel):
    """
    系统配置类
    """

    class Config:
        case_sensitive = True
        env_file = SystemUtils.get_env_path()
        env_file_encoding = "utf-8"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 初始化配置目录及子目录
        for path in [self.CONFIG_PATH, self.TEMP_PATH, self.LOG_PATH, self.COOKIE_PATH]:
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
        # 如果是二进制程序，确保配置文件存在
        if SystemUtils.is_frozen():
            app_env_path = self.CONFIG_PATH / "app.env"
            if not app_env_path.exists():
                SystemUtils.copy(self.INNER_CONFIG_PATH / "app.env", app_env_path)

    @staticmethod
    def validate_api_token(value: Any, original_value: Any) -> Tuple[Any, bool]:
        """
        校验 API_TOKEN
        """
        if isinstance(value, (list, dict, set)):
            value = copy.deepcopy(value)
        value = value.strip() if isinstance(value, str) else None
        if not value or len(value) < 16:
            new_token = secrets.token_urlsafe(16)
            if not value:
                logger.info(f"'API_TOKEN' 未设置，已随机生成新的【API_TOKEN】{new_token}")
            else:
                logger.warning(f"'API_TOKEN' 长度不足 16 个字符，存在安全隐患，已随机生成新的【API_TOKEN】{new_token}")
            return new_token, True
        return value, str(value) != str(original_value)

    @staticmethod
    def generic_type_converter(value: Any, original_value: Any, expected_type: Type, default: Any, field_name: str,
                               raise_exception: bool = False) -> Tuple[Any, bool]:
        """
        通用类型转换函数，根据预期类型转换值。如果转换失败，返回默认值
        """
        if isinstance(value, (list, dict, set)):
            value = copy.deepcopy(value)
        # 如果 value 是 None，仍需要检查与 original_value 是否不一致
        if value is None:
            return default, str(value) != str(original_value)

        if isinstance(value, str):
            value = value.strip()

        try:
            if expected_type is bool:
                if isinstance(value, bool):
                    return value, str(value).lower() != str(original_value).lower()
                if isinstance(value, str):
                    value_clean = value.lower()
                    bool_map = {
                        "false": False, "no": False, "0": False, "off": False,
                        "true": True, "yes": True, "1": True, "on": True
                    }
                    if value_clean in bool_map:
                        converted = bool_map[value_clean]
                        return converted, str(converted).lower() != str(original_value).lower()
                elif isinstance(value, (int, float)):
                    converted = bool(value)
                    return converted, str(converted).lower() != str(original_value).lower()
                return default, True
            elif expected_type is int:
                if isinstance(value, int):
                    return value, str(value) != str(original_value)
                if isinstance(value, str):
                    converted = int(value)
                    return converted, str(converted) != str(original_value)
            elif expected_type is float:
                if isinstance(value, float):
                    return value, str(value) != str(original_value)
                if isinstance(value, str):
                    converted = float(value)
                    return converted, str(converted) != str(original_value)
            elif expected_type is str:
                # 清理 value 中所有空白字符的字段
                fields_not_keep_spaces = {"AUTO_DOWNLOAD_USER", "REPO_GITHUB_TOKEN", "PLUGIN_MARKET"}
                if field_name in fields_not_keep_spaces:
                    value = re.sub(r"\s+", "", value)
                return value, str(value) != str(original_value)
            # # 后续考虑支持 list 类型的处理
            # elif expected_type is list:
            #     if isinstance(value, list):
            #         return value, False
            #     if isinstance(value, str):
            #         items = [item.strip() for item in value.split(",") if item.strip()]
            #         return items, items != original_value.split(",")
            # 可根据需要添加更多类型处理
            else:
                return value, str(value) != str(original_value)
        except (ValueError, TypeError) as e:
            if raise_exception:
                raise ValueError(f"配置项 '{field_name}' 的值 '{value}' 无法转换成正确的类型") from e
            logger.error(
                f"配置项 '{field_name}' 的值 '{value}' 无法转换成正确的类型，使用默认值 '{default}'，错误信息: {e}")
            return default, True

    @validator('*', pre=True, always=True)
    def generic_type_validator(cls, value: Any, field):  # noqa
        """
        通用校验器，尝试将配置值转换为期望的类型
        """
        if field.name == "API_TOKEN":
            converted_value, needs_update = cls.validate_api_token(value, value)
        else:
            converted_value, needs_update = cls.generic_type_converter(value, value, field.type_, field.default,
                                                                       field.name)
        if needs_update:
            cls.update_env_config(field, value, converted_value)
        return converted_value

    @staticmethod
    def update_env_config(field: Any, original_value: Any, converted_value: Any) -> Tuple[bool, str]:
        """
        更新 env 配置
        """
        message = None
        is_converted = original_value is not None and str(original_value) != str(converted_value)
        if is_converted:
            message = f"配置项 '{field.name}' 的值 '{original_value}' 无效，已替换为 '{converted_value}'"
            logger.warning(message)

        if field.name in os.environ:
            message = f"配置项 '{field.name}' 已在环境变量中设置，请手动更新以保持一致性"
            logger.warning(message)
            return False, message
        else:
            set_key(SystemUtils.get_env_path(), field.name, str(converted_value) if converted_value is not None else "")
            if is_converted:
                logger.info(f"配置项 '{field.name}' 已自动修正并写入到 'app.env' 文件")
        return True, message

    def update_setting(self, key: str, value: Any) -> Tuple[bool, str]:
        """
        更新单个配置项
        """
        if not hasattr(self, key):
            return False, f"配置项 '{key}' 不存在"

        try:
            field = self.__fields__[key]
            original_value = getattr(self, key)
            if field.name == "API_TOKEN":
                converted_value, needs_update = self.validate_api_token(value, original_value)
            else:
                converted_value, needs_update = self.generic_type_converter(value, original_value, field.type_,
                                                                            field.default, key)
            # 如果没有抛出异常，则统一使用 converted_value 进行更新
            if needs_update or str(value) != str(converted_value):
                success, message = self.update_env_config(field, value, converted_value)
                # 仅成功更新配置时，才更新内存
                if success:
                    setattr(self, key, converted_value)
                    if hasattr(log_settings, key):
                        setattr(log_settings, key, converted_value)
                return success, message
            return True, ""
        except Exception as e:
            return False, str(e)

    def update_settings(self, env: Dict[str, Any]) -> Dict[str, Tuple[bool, str]]:
        """
        更新多个配置项
        """
        results = {}
        log_updated, plugin_monitor_updated = False, False
        for k, v in env.items():
            results[k] = self.update_setting(k, v)
            if hasattr(log_settings, k):
                log_updated = True
            if k in ["PLUGIN_AUTO_RELOAD", "DEV"]:
                plugin_monitor_updated = True
        # 本次更新存在日志配置项更新，需要重新加载日志配置
        if log_updated:
            logger.update_loggers()
        # 本次更新存在插件监控配置项更新，需要重新加载插件监控
        if plugin_monitor_updated:
            # 解决顶层循环导入问题
            from app.core.plugin import PluginManager
            PluginManager().reload_monitor()
        return results

    @property
    def VERSION_FLAG(self) -> str:
        """
        版本标识，用来区分重大版本，为空则为v1，不允许外部修改
        """
        return "v2"

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
    def CACHE_PATH(self):
        return self.CONFIG_PATH / "cache"

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
    def COOKIE_PATH(self):
        return self.CONFIG_PATH / "cookies"

    @property
    def CACHE_CONF(self):
        """
        {
            "torrents": "缓存种子数量",
            "refresh": "订阅刷新处理数量",
            "tmdb": "TMDB请求缓存数量",
            "douban": "豆瓣请求缓存数量",
            "fanart": "Fanart请求缓存数量",
            "meta": "元数据缓存过期时间（秒）"
        }
        """
        if self.BIG_MEMORY_MODE:
            return {
                "torrents": 200,
                "refresh": 100,
                "tmdb": 1024,
                "douban": 512,
                "bangumi": 512,
                "fanart": 512,
                "meta": (self.META_CACHE_EXPIRE or 24) * 3600
            }
        return {
            "torrents": 100,
            "refresh": 50,
            "tmdb": 256,
            "douban": 256,
            "bangumi": 256,
            "fanart": 128,
            "meta": (self.META_CACHE_EXPIRE or 2) * 3600
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
    def GITHUB_HEADERS(self):
        """
        Github请求头
        """
        if self.GITHUB_TOKEN:
            return {
                "Authorization": f"Bearer {self.GITHUB_TOKEN}"
            }
        return {}

    def REPO_GITHUB_HEADERS(self, repo: str = None):
        """
        Github指定的仓库请求头
        :param repo: 指定的仓库名称，格式为 "user/repo"。如果为空，或者没有找到指定仓库请求头，则返回默认的请求头信息
        :return: Github请求头
        """
        # 如果没有传入指定的仓库名称，或没有配置指定的仓库Token，则返回默认的请求头信息
        if not repo or not self.REPO_GITHUB_TOKEN:
            return self.GITHUB_HEADERS
        headers = {}
        # 格式：{user1}/{repo1}:ghp_****,{user2}/{repo2}:github_pat_****
        token_pairs = self.REPO_GITHUB_TOKEN.split(",")
        for token_pair in token_pairs:
            try:
                parts = token_pair.split(":")
                if len(parts) != 2:
                    print(f"无效的令牌格式: {token_pair}")
                    continue
                repo_info = parts[0].strip()
                token = parts[1].strip()
                if not repo_info or not token:
                    print(f"无效的令牌或仓库信息: {token_pair}")
                    continue
                headers[repo_info] = {
                    "Authorization": f"Bearer {token}"
                }
            except Exception as e:
                print(f"处理令牌对 '{token_pair}' 时出错: {e}")
        # 如果传入了指定的仓库名称，则返回该仓库的请求头信息，否则返回默认请求头
        return headers.get(repo, self.GITHUB_HEADERS)

    @property
    def VAPID(self):
        return {
            "subject": f"mailto:{self.SUPERUSER}@movie-pilot.org",
            "publicKey": "BH3w49sZA6jXUnE-yt4jO6VKh73lsdsvwoJ6Hx7fmPIDKoqGiUl2GEoZzy-iJfn4SfQQcx7yQdHf9RknwrL_lSM",
            "privateKey": "JTixnYY0vEw97t9uukfO3UWKfHKJdT5kCQDiv3gu894"
        }

    def MP_DOMAIN(self, url: str = None):
        if not self.APP_DOMAIN:
            return None
        return UrlUtils.combine_url(host=self.APP_DOMAIN, path=url)


class GlobalVar(object):
    """
    全局标识
    """
    # 系统停止事件
    STOP_EVENT: threading.Event = threading.Event()
    # webpush订阅
    SUBSCRIPTIONS: List[dict] = []
    # 需应急停止的工作流
    EMERGENCY_STOP_WORKFLOWS: List[str] = []

    def stop_system(self):
        """
        停止系统
        """
        self.STOP_EVENT.set()

    @property
    def is_system_stopped(self):
        """
        是否停止
        """
        return self.STOP_EVENT.is_set()

    def get_subscriptions(self):
        """
        获取webpush订阅
        """
        return self.SUBSCRIPTIONS

    def push_subscription(self, subscription: dict):
        """
        添加webpush订阅
        """
        self.SUBSCRIPTIONS.append(subscription)

    def stop_workflow(self, workflow_id: str):
        """
        停止工作流
        """
        if workflow_id not in self.EMERGENCY_STOP_WORKFLOWS:
            self.EMERGENCY_STOP_WORKFLOWS.append(workflow_id)

    def workflow_resume(self, workflow_id: str):
        """
        恢复工作流
        """
        if workflow_id in self.EMERGENCY_STOP_WORKFLOWS:
            self.EMERGENCY_STOP_WORKFLOWS.remove(workflow_id)

    def is_workflow_stopped(self, workflow_id: str):
        """
        是否停止工作流
        """
        return self.is_system_stopped or workflow_id in self.EMERGENCY_STOP_WORKFLOWS


# 实例化配置
settings = Settings()

# 全局标识
global_vars = GlobalVar()
