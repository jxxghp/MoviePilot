import asyncio
import copy
import json
import os
import platform
import re
import secrets
import sys
import threading
from asyncio import AbstractEventLoop
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union, get_origin, get_args
from urllib.parse import quote, urlencode, urlparse

from dotenv import set_key, unset_key
from pydantic import BaseModel, Field, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.log import logger, log_settings, LogConfigModel
from app.schemas import MediaType
from app.utils.system import SystemUtils
from app.utils.url import UrlUtils
from version import APP_VERSION


class SystemConfModel(BaseModel):
    """
    系统关键资源大小配置
    """

    # 缓存种子数量
    torrents: int = 0
    # 订阅刷新处理数量
    refresh: int = 0
    # TMDB请求缓存数量
    tmdb: int = 0
    # 豆瓣请求缓存数量
    douban: int = 0
    # Bangumi请求缓存数量
    bangumi: int = 0
    # Fanart请求缓存数量
    fanart: int = 0
    # 元数据缓存过期时间（秒）
    meta: int = 0
    # 调度器数量
    scheduler: int = 0
    # 线程池大小
    threadpool: int = 0


class ConfigModel(BaseModel):
    """
    Pydantic 配置模型，描述所有配置项及其类型和默认值
    """

    model_config = ConfigDict(extra="ignore")  # 忽略未定义的配置项

    # ==================== 基础应用配置 ====================
    # 项目名称
    PROJECT_NAME: str = "MoviePilot"
    # 域名 格式；https://movie-pilot.org
    APP_DOMAIN: str = ""
    # API路径
    API_V1_STR: str = "/api/v1"
    # 前端资源路径
    FRONTEND_PATH: str = "/public"
    # 时区
    TZ: str = "Asia/Shanghai"
    # API监听地址
    HOST: str = "0.0.0.0"
    # API监听端口
    PORT: int = 3001
    # 前端监听端口
    NGINX_PORT: int = 3000
    # 配置文件目录
    CONFIG_DIR: Optional[str] = None
    # 安全模式，仅保留核心 API，跳过插件、调度器、监控、命令和工作流等扩展启动项
    MOVIEPILOT_SAFE_MODE: bool = False
    # 是否调试模式
    DEBUG: bool = False
    # 是否开发模式
    DEV: bool = False
    # 高级设置模式
    ADVANCED_MODE: bool = True

    # ==================== 安全认证配置 ====================
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
    # 超级管理员初始用户名
    SUPERUSER: str = "admin"
    # 超级管理员初始密码
    SUPERUSER_PASSWORD: Optional[str] = None
    # 辅助认证，允许通过外部服务进行认证、单点登录以及自动创建用户
    AUXILIARY_AUTH_ENABLE: bool = False
    # API密钥，需要更换
    API_TOKEN: Optional[str] = None
    # 用户认证站点
    AUTH_SITE: str = ""

    # ==================== 数据库配置 ====================
    # 数据库类型，支持 sqlite 和 postgresql，默认使用 sqlite
    DB_TYPE: str = "sqlite"
    # 是否在控制台输出 SQL 语句，默认关闭
    DB_ECHO: bool = False
    # 数据库连接超时时间（秒），默认为 60 秒
    DB_TIMEOUT: int = 60
    # 是否启用 WAL 模式，仅适用于SQLite，默认开启
    DB_WAL_ENABLE: bool = True
    # 数据库连接池类型，QueuePool, NullPool
    DB_POOL_TYPE: str = "QueuePool"
    # 是否在获取连接时进行预先 ping 操作
    DB_POOL_PRE_PING: bool = True
    # 数据库连接的回收时间（秒）
    DB_POOL_RECYCLE: int = 300
    # 数据库连接池获取连接的超时时间（秒）
    DB_POOL_TIMEOUT: int = 30
    # SQLite 连接池大小
    DB_SQLITE_POOL_SIZE: int = 10
    # SQLite 连接池溢出数量
    DB_SQLITE_MAX_OVERFLOW: int = 50
    # PostgreSQL 主机地址
    DB_POSTGRESQL_HOST: str = "localhost"
    # PostgreSQL 端口；使用 Unix Socket 时可留空
    DB_POSTGRESQL_PORT: str = "5432"
    # PostgreSQL 数据库名
    DB_POSTGRESQL_DATABASE: str = "moviepilot"
    # PostgreSQL 用户名
    DB_POSTGRESQL_USERNAME: str = "moviepilot"
    # PostgreSQL 密码
    DB_POSTGRESQL_PASSWORD: str = "moviepilot"
    # PostgreSQL 连接池大小
    DB_POSTGRESQL_POOL_SIZE: int = 10
    # PostgreSQL 连接池溢出数量
    DB_POSTGRESQL_MAX_OVERFLOW: int = 50

    # ==================== 数据清理配置 ====================
    # 是否启用数据表定时清理
    DATA_CLEANUP_ENABLE: bool = False
    # 消息表保留天数，0为不清理
    DATA_CLEANUP_MESSAGE_DAYS: int = 90
    # 下载历史表保留天数，0为不清理
    DATA_CLEANUP_DOWNLOAD_HISTORY_DAYS: int = 180
    # 站点用户数据表保留天数，0为不清理
    DATA_CLEANUP_SITE_USERDATA_DAYS: int = 180
    # 整理历史表保留天数，0为不清理
    DATA_CLEANUP_TRANSFER_HISTORY_DAYS: int = 365 * 3

    # ==================== 缓存配置 ====================
    # 缓存类型，支持 cachetools 和 redis，默认使用 cachetools
    CACHE_BACKEND_TYPE: str = "cachetools"
    # 缓存连接字符串，仅外部缓存（如 Redis、Memcached）需要，支持 Redis Unix Socket URL
    CACHE_BACKEND_URL: Optional[str] = "redis://localhost:6379"
    # Redis 缓存最大内存限制，未配置时，如开启大内存模式时为 "1024mb"，未开启时为 "256mb"
    CACHE_REDIS_MAXMEMORY: Optional[str] = None
    # Redis 连接池最大连接数
    CACHE_REDIS_MAX_CONNECTIONS: int = 256
    # Redis 连接池耗尽时等待可用连接的时间（秒）
    CACHE_REDIS_POOL_TIMEOUT: int = 3
    # 全局图片缓存，将媒体图片缓存到本地
    GLOBAL_IMAGE_CACHE: bool = False
    # 全局图片缓存保留天数
    GLOBAL_IMAGE_CACHE_DAYS: int = 7
    # 临时文件保留天数
    TEMP_FILE_DAYS: int = 3
    # pip/uv 包下载缓存保留天数
    PACKAGE_CACHE_DAYS: int = 90
    # pip/uv 包下载缓存根目录，留空时使用配置目录下的 .cache
    PACKAGE_CACHE_ROOT: Optional[str] = None
    # 元数据识别缓存过期时间（小时），0为自动
    META_CACHE_EXPIRE: int = 0

    # ==================== 网络代理配置 ====================
    # 网络代理服务器地址
    PROXY_HOST: Optional[str] = None
    # 是否启用DOH解析域名
    DOH_ENABLE: bool = False
    # 使用 DOH 解析的域名列表
    DOH_DOMAINS: str = (
        "api.themoviedb.org,"
        "api.tmdb.org,"
        "webservice.fanart.tv,"
        "api.github.com,"
        "github.com,"
        "raw.githubusercontent.com,"
        "codeload.github.com,"
        "api.telegram.org"
    )
    # DOH 解析服务器列表
    DOH_RESOLVERS: str = "1.0.0.1,1.1.1.1,9.9.9.9,149.112.112.112"

    # ==================== 媒体元数据配置 ====================
    # 媒体搜索来源 themoviedb/douban/bangumi，多个用,分隔
    SEARCH_SOURCE: str = "themoviedb"
    # 媒体识别来源 themoviedb/douban
    RECOGNIZE_SOURCE: str = "themoviedb"
    # 刮削来源 themoviedb/douban
    SCRAP_SOURCE: str = "themoviedb"
    # 电视剧动漫的分类genre_ids
    ANIME_GENREIDS: List[int] = Field(default=[16])

    # ==================== TMDB配置 ====================
    # TMDB图片地址
    TMDB_IMAGE_DOMAIN: str = "image.tmdb.org"
    # TMDB API地址
    TMDB_API_DOMAIN: str = "api.themoviedb.org"
    # TMDB元数据语言
    TMDB_LOCALE: str = "zh"
    # 刮削使用TMDB原始语种图片
    TMDB_SCRAP_ORIGINAL_IMAGE: bool = False
    # TMDB API Key
    TMDB_API_KEY: str = "db55323b8d3e4154498498a75642b381"

    # ==================== TVDB配置 ====================
    # TVDB API Key
    TVDB_V4_API_KEY: str = "ed2aa66b-7899-4677-92a7-67bc9ce3d93a"
    TVDB_V4_API_PIN: str = ""

    # ==================== Fanart配置 ====================
    # Fanart开关
    FANART_ENABLE: bool = True
    # Fanart语言
    FANART_LANG: str = "zh,en"
    # Fanart API Key
    FANART_API_KEY: str = "d2d31f9ecabea050fc7d68aa3146015f"

    # ==================== 云盘配置 ====================
    # 115 AppId
    U115_APP_ID: str = "100197847"
    # 115 OAuth2 Server 地址
    U115_AUTH_SERVER: str = "https://movie-pilot.org"
    # Alipan AppId
    ALIPAN_APP_ID: str = "ac1bf04dc9fd4d9aaabb65b4a668d403"

    # ==================== 系统升级配置 ====================
    # 重启自动升级
    MOVIEPILOT_AUTO_UPDATE: str = "release"
    # 自动检查和更新站点资源包（站点索引、认证等）
    AUTO_UPDATE_RESOURCE: bool = True

    # ==================== 媒体文件格式配置 ====================
    # 支持的视频文件后缀格式
    RMT_MEDIAEXT: list = Field(
        default_factory=lambda: [
            ".mp4",
            ".mkv",
            ".ts",
            ".iso",
            ".rmvb",
            ".avi",
            ".mov",
            ".mpeg",
            ".mpg",
            ".wmv",
            ".3gp",
            ".asf",
            ".m4v",
            ".flv",
            ".m2ts",
            ".strm",
            ".tp",
            ".f4v",
        ]
    )
    # 支持的字幕文件后缀格式
    RMT_SUBEXT: list = Field(default_factory=lambda: [".srt", ".ass", ".ssa", ".sup"])
    # 支持的音轨文件后缀格式
    RMT_AUDIOEXT: list = Field(
        default_factory=lambda: [
            ".aac",
            ".ac3",
            ".amr",
            ".caf",
            ".cda",
            ".dsf",
            ".dff",
            ".kar",
            ".m4a",
            ".mp1",
            ".mp2",
            ".mp3",
            ".mid",
            ".mod",
            ".mka",
            ".mpc",
            ".nsf",
            ".ogg",
            ".pcm",
            ".rmi",
            ".s3m",
            ".snd",
            ".spx",
            ".tak",
            ".tta",
            ".vqf",
            ".wav",
            ".wma",
            ".aifc",
            ".aiff",
            ".alac",
            ".adif",
            ".adts",
            ".flac",
            ".midi",
            ".opus",
            ".sfalc",
        ]
    )

    # ==================== 媒体服务器配置 ====================
    # 媒体服务器同步间隔（小时）
    MEDIASERVER_SYNC_INTERVAL: int = 6

    # ==================== 订阅配置 ====================
    # 订阅模式
    SUBSCRIBE_MODE: str = "spider"
    # RSS订阅模式刷新时间间隔（分钟）
    SUBSCRIBE_RSS_INTERVAL: int = 30
    # 订阅数据共享
    SUBSCRIBE_STATISTIC_SHARE: bool = True
    # 订阅搜索开关
    SUBSCRIBE_SEARCH: bool = False
    # 订阅搜索时间间隔（小时）
    SUBSCRIBE_SEARCH_INTERVAL: int = 24
    # 检查本地媒体库是否存在资源开关
    LOCAL_EXISTS_SEARCH: bool = True

    # ==================== 站点配置 ====================
    # 站点数据刷新间隔（小时）
    SITEDATA_REFRESH_INTERVAL: int = 6
    # 读取和发送站点消息
    SITE_MESSAGE: bool = True
    # 不能缓存站点资源的站点域名，多个使用,分隔
    NO_CACHE_SITE_KEY: str = "m-team"
    # OCR服务器地址，用于识别站点验证码
    OCR_HOST: str = "https://movie-pilot.org"
    # 仿真类型：cloakbrowser 或 flaresolverr，其他值按 cloakbrowser 处理
    BROWSER_EMULATION: str = "cloakbrowser"
    # CloakBrowser 是否启用拟人化输入
    CLOAKBROWSER_HUMANIZE: bool = True
    # CloakBrowser 拟人化输入预设：default 或 careful
    CLOAKBROWSER_HUMAN_PRESET: str = "default"
    # FlareSolverr 服务地址，例如 http://127.0.0.1:8191
    FLARESOLVERR_URL: Optional[str] = None

    # ==================== 搜索配置 ====================
    # 搜索多个名称
    SEARCH_MULTIPLE_NAME: bool = False
    # 最大搜索名称数量
    MAX_SEARCH_NAME_LIMIT: int = 3
    # 搜索资源获取页数
    SEARCH_RESOURCE_PAGES: int = 1

    # ==================== 下载配置 ====================
    # 种子标签
    TORRENT_TAG: str = "MOVIEPILOT"
    # 下载站点字幕
    DOWNLOAD_SUBTITLE: bool = True
    # 交互搜索自动下载用户ID，使用,分割
    AUTO_DOWNLOAD_USER: Optional[str] = None
    # 下载器临时文件后缀
    DOWNLOAD_TMPEXT: list = Field(default_factory=lambda: [".!qb", ".part"])

    # ==================== CookieCloud配置 ====================
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

    # ==================== 整理配置 ====================
    # 文件整理线程数
    TRANSFER_THREADS: int = 1
    # 电影重命名格式
    MOVIE_RENAME_FORMAT: str = (
        "{{title}}{% if year %} ({{year}}){% endif %}"
        "/{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}"
        "{{fileExt}}"
    )
    # 电视剧重命名格式
    TV_RENAME_FORMAT: str = (
        "{{title}}{% if year %} ({{year}}){% endif %}"
        "/Season {{season}}"
        "/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}"
        "{{fileExt}}"
    )
    # 重命名时支持的S0别名
    RENAME_FORMAT_S0_NAMES: list = Field(default=["Specials", "SPs"])
    # 为指定默认字幕添加.default后缀
    DEFAULT_SUB: Optional[str] = "zh-cn"
    # 新增已入库媒体是否跟随TMDB信息变化
    SCRAP_FOLLOW_TMDB: bool = True
    # 优先使用辅助识别
    RECOGNIZE_PLUGIN_FIRST: bool = False
    # 共享使用媒体识别数据
    MEDIA_RECOGNIZE_SHARE: bool = True

    # ==================== 服务地址配置 ====================
    # 服务器地址，对应 https://github.com/jxxghp/MoviePilot-Server 项目
    MP_SERVER_HOST: str = "https://movie-pilot.org"
    # 共享媒体识别API地址，留空时默认拼接为 MP_SERVER_HOST + /recognize/share
    MEDIA_RECOGNIZE_SHARE_API: Optional[str] = None

    # ==================== 个性化 ====================
    # 登录页面电影海报,tmdb/bing/mediaserver
    WALLPAPER: str = "tmdb"
    # 自定义壁纸api地址
    CUSTOMIZE_WALLPAPER_API_URL: Optional[str] = None

    # ==================== 插件配置 ====================
    # 插件市场仓库地址，多个地址使用,分隔，地址以/结尾
    PLUGIN_MARKET: str = (
        "https://github.com/jxxghp/MoviePilot-Plugins,"
        "https://github.com/thsrite/MoviePilot-Plugins,"
        "https://github.com/honue/MoviePilot-Plugins,"
        "https://github.com/InfinityPacer/MoviePilot-Plugins,"
        "https://github.com/DDSRem-Dev/MoviePilot-Plugins,"
        "https://github.com/madrays/MoviePilot-Plugins,"
        "https://github.com/justzerock/MoviePilot-Plugins,"
        "https://github.com/KoWming/MoviePilot-Plugins,"
        "https://github.com/wikrin/MoviePilot-Plugins,"
        "https://github.com/HankunYu/MoviePilot-Plugins,"
        "https://github.com/baozaodetudou/MoviePilot-Plugins,"
        "https://github.com/Aqr-K/MoviePilot-Plugins,"
        "https://github.com/hotlcc/MoviePilot-Plugins-Third,"
        "https://github.com/gxterry/MoviePilot-Plugins,"
        "https://github.com/DzAvril/MoviePilot-Plugins,"
        "https://github.com/mrtian2016/MoviePilot-Plugins,"
        "https://github.com/Hqyel/MoviePilot-Plugins-Third,"
        "https://github.com/xijin285/MoviePilot-Plugins,"
        "https://github.com/Seed680/MoviePilot-Plugins,"
        "https://github.com/imaliang/MoviePilot-Plugins"
    )
    # 插件安装数据共享
    PLUGIN_STATISTIC_SHARE: bool = True
    # 安装版本统计上报
    USAGE_STATISTIC_SHARE: bool = True
    # 是否开启插件热加载
    PLUGIN_AUTO_RELOAD: bool = False
    # 本地插件仓库目录，多个地址使用,分隔
    PLUGIN_LOCAL_REPO_PATHS: Optional[str] = None

    # ==================== 技能配置 ====================
    # 技能市场仓库地址，多个地址使用,分隔
    SKILL_MARKET: str = (
        "https://clawhub.ai,"
        "https://github.com/openai/skills,"
        "https://github.com/anthropics/skills,"
        "https://github.com/vercel-labs/agent-skills"
    )

    # ==================== Github & PIP ====================
    # Github token，提高请求api限流阈值 ghp_****
    GITHUB_TOKEN: Optional[str] = None
    # Github代理服务器，格式：https://mirror.ghproxy.com/
    GITHUB_PROXY: Optional[str] = ""
    # pip镜像站点，格式：https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
    PIP_PROXY: Optional[str] = ""
    # 指定的仓库Github token，多个仓库使用,分隔，格式：{user1}/{repo1}:ghp_****,{user2}/{repo2}:github_pat_****
    REPO_GITHUB_TOKEN: Optional[str] = None

    # ==================== 飞书通知配置 ====================
    # 飞书应用 App ID
    FEISHU_APP_ID: Optional[str] = None
    # 飞书应用 App Secret
    FEISHU_APP_SECRET: Optional[str] = None
    # 飞书默认接收用户 Open ID
    FEISHU_OPEN_ID: Optional[str] = None
    # 飞书默认接收群聊 Chat ID
    FEISHU_CHAT_ID: Optional[str] = None
    # 飞书管理员 Open ID 列表，多个使用 , 分隔
    FEISHU_ADMINS: Optional[str] = None
    # 飞书事件校验 Token
    FEISHU_VERIFICATION_TOKEN: Optional[str] = None
    # 飞书事件加密 Key
    FEISHU_ENCRYPT_KEY: Optional[str] = None

    # ==================== 性能配置 ====================
    # 大内存模式
    BIG_MEMORY_MODE: bool = False
    # Rust 加速总开关，关闭时所有 Rust 快路径回退到 Python 实现
    RUST_ACCEL: bool = True
    # 是否启用编码探测的性能模式
    ENCODING_DETECTION_PERFORMANCE_MODE: bool = True
    # 编码探测的最低置信度阈值
    ENCODING_DETECTION_MIN_CONFIDENCE: float = 0.8
    # 主动内存回收时间间隔（分钟），0为不启用
    MEMORY_GC_INTERVAL: int = 30

    # ==================== 安全配置 ====================
    # 允许的图片缓存域名
    SECURITY_IMAGE_DOMAINS: list = Field(
        default=[
            "image.tmdb.org",
            "images.tmdb.org",
            "static-mdb.v.geilijiasu.com",
            "bing.com",
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
            "qpic.cn",
        ]
    )
    # 图片代理允许访问的非公网 IP/CIDR，默认不放行任何非公网解析结果
    IMAGE_PROXY_ALLOWED_PRIVATE_RANGES: list = Field(default=[])
    # 允许的图片文件后缀格式
    SECURITY_IMAGE_SUFFIXES: list = Field(
        default=[".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif"]
    )
    # PassKey 是否强制用户验证（生物识别等）
    PASSKEY_REQUIRE_UV: bool = True
    # 允许在未启用 OTP 时直接注册 PassKey
    PASSKEY_ALLOW_REGISTER_WITHOUT_OTP: bool = False

    # ==================== 工作流配置 ====================
    # 工作流数据共享
    WORKFLOW_STATISTIC_SHARE: bool = True

    # ==================== 存储配置 ====================
    # 对rclone进行快照对比时，是否检查文件夹的修改时间
    RCLONE_SNAPSHOT_CHECK_FOLDER_MODTIME: bool = True
    # 对OpenList进行快照对比时，是否检查文件夹的修改时间
    OPENLIST_SNAPSHOT_CHECK_FOLDER_MODTIME: bool = True
    # 对阿里云盘进行快照对比时，是否检查文件夹的修改时间（默认关闭，因为阿里云盘目录时间不随子文件变更而更新）
    ALIPAN_SNAPSHOT_CHECK_FOLDER_MODTIME: bool = False

    # ==================== Docker配置 ====================
    # Docker Client API地址
    DOCKER_CLIENT_API: Optional[str] = "tcp://127.0.0.1:38379"
    # Playwright浏览器类型，供智能体浏览器工具和插件直接使用 Playwright 时读取
    PLAYWRIGHT_BROWSER_TYPE: str = "chromium"

    # ==================== AI智能体配置 ====================
    # AI智能体开关
    AI_AGENT_ENABLE: bool = False
    # 合局AI智能体
    AI_AGENT_GLOBAL: bool = False
    # 是否隐藏前端全局智能体入口
    AI_AGENT_HIDE_ENTRY: bool = False
    # LLM提供商（支持内置 provider，以及从 models.dev 动态补充的平台）
    LLM_PROVIDER: str = "deepseek"
    # LLM模型名称
    LLM_MODEL: str = "deepseek-chat"
    # 思考模式/深度配置：off/auto/minimal/low/medium/high/max/xhigh
    LLM_THINKING_LEVEL: Optional[str] = "off"
    # LLM是否支持图片输入，开启后消息图片会按多模态输入发送给模型
    LLM_SUPPORT_IMAGE_INPUT: bool = True
    # 是否启用音频输入，开启后用户语音会先转写为文本再进入 Agent
    LLM_SUPPORT_AUDIO_INPUT: bool = False
    # 是否启用音频输出，开启后 Agent 可在支持渠道发送语音回复
    LLM_SUPPORT_AUDIO_OUTPUT: bool = False
    # LLM API密钥
    LLM_API_KEY: Optional[str] = None
    # LLM基础URL（用于自定义API端点）
    LLM_BASE_URL: Optional[str] = "https://api.deepseek.com"
    # LLM调用是否使用系统代理
    LLM_USE_PROXY: bool = True
    # LLM Base URL 预设标识，用于区分同一 Base URL 下的不同模型目录
    LLM_BASE_URL_PRESET: Optional[str] = None
    # LLM最大上下文Token数量（K）
    LLM_MAX_CONTEXT_TOKENS: int = 128
    # LLM OpenAI兼容接口请求User-Agent
    LLM_USER_AGENT: Optional[str] = None
    # LLM温度参数
    LLM_TEMPERATURE: float = 0.3
    # LLM最大迭代次数
    LLM_MAX_ITERATIONS: int = 512
    # LLM工具调用超时时间（秒）
    LLM_TOOL_TIMEOUT: int = 300
    # 是否启用详细日志
    LLM_VERBOSE: bool = False
    # 内存记忆保留天数
    LLM_MEMORY_RETENTION_DAYS: int = 1
    # 是否启用AI推荐
    AI_RECOMMEND_ENABLED: bool = False
    # AI推荐用户偏好
    AI_RECOMMEND_USER_PREFERENCE: str = ""

    # AI推荐条目数量限制
    AI_RECOMMEND_MAX_ITEMS: int = 50
    # LLM工具选择中间件最大工具数量，0为不启用工具选择中间件
    LLM_MAX_TOOLS: int = 20
    # AI智能体定时任务检查间隔（小时），0为不启用，默认24小时
    AI_AGENT_JOB_INTERVAL: int = 0
    # AI智能体啰嗦模式，开启后会回复工具调用过程
    AI_AGENT_VERBOSE: bool = False
    # AI智能体自动重试整理失败记录开关
    AI_AGENT_RETRY_TRANSFER: bool = False

    # 音频输入提供商：openai/openai_chat_audio/mimo/minimax
    AUDIO_INPUT_PROVIDER: str = "openai"
    # 音频输入 API 密钥
    AUDIO_INPUT_API_KEY: Optional[str] = None
    # 音频输入基础URL
    AUDIO_INPUT_BASE_URL: Optional[str] = None
    # 音频输入模型
    AUDIO_INPUT_MODEL: str = "gpt-4o-mini-transcribe"
    # 音频输入识别语言
    AUDIO_INPUT_LANGUAGE: str = "zh"
    # 音频输出提供商：openai/openai_chat_audio/mimo/minimax
    AUDIO_OUTPUT_PROVIDER: str = "openai"
    # 音频输出 API 密钥
    AUDIO_OUTPUT_API_KEY: Optional[str] = None
    # 音频输出基础URL
    AUDIO_OUTPUT_BASE_URL: Optional[str] = None
    # 音频输出模型
    AUDIO_OUTPUT_MODEL: str = "gpt-4o-mini-tts"
    # 音频输出音色/发音人
    AUDIO_OUTPUT_VOICE: str = "alloy"
    # 回复语音时是否同时附带文字说明
    AUDIO_OUTPUT_INCLUDE_TEXT: bool = False


class Settings(BaseSettings, ConfigModel, LogConfigModel):
    """
    系统配置类
    """

    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=SystemUtils.get_env_path(),
        env_file_encoding="utf-8",
    )

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
                logger.info(
                    f"'API_TOKEN' 未设置，已随机生成新的【API_TOKEN】{new_token}"
                )
            else:
                logger.warning(
                    f"'API_TOKEN' 长度不足 16 个字符，存在安全隐患，已随机生成新的【API_TOKEN】{new_token}"
                )
            return new_token, True
        return value, str(value) != str(original_value)

    @staticmethod
    def generic_type_converter(
        value: Any,
        original_value: Any,
        expected_type: Type,
        default: Any,
        field_name: str,
        raise_exception: bool = False,
    ) -> Tuple[Any, bool]:
        """
        通用类型转换函数，根据预期类型转换值。如果转换失败，返回默认值
        :return: 元组 (转换后的值, 是否需要更新)
        """
        if isinstance(value, (list, dict, set)):
            value = copy.deepcopy(value)
        # 如果 value 是 None，仍需要检查与 original_value 是否不一致
        if value is None:
            return default, str(value) != str(original_value)

        if isinstance(value, str):
            value = value.strip()

        # 处理 Optional 类型：当值为空字符串且类型允许 None 时，转为 None
        # 兼容 typing.Union (Python 3.9) 与 types.UnionType (Python 3.10+ PEP 604)
        origin = get_origin(expected_type)
        is_union = origin is Union or getattr(origin, "__name__", None) == "UnionType"
        if (
            is_union
            and type(None) in get_args(expected_type)
            and isinstance(value, str)
            and not value
        ):
            return default, str(default) != str(original_value)

        try:
            if expected_type is bool:
                if isinstance(value, bool):
                    return value, str(value).lower() != str(original_value).lower()
                if isinstance(value, str):
                    value_clean = value.lower()
                    bool_map = {
                        "false": False,
                        "no": False,
                        "0": False,
                        "off": False,
                        "true": True,
                        "yes": True,
                        "1": True,
                        "on": True,
                    }
                    if value_clean in bool_map:
                        converted = bool_map[value_clean]
                        return converted, str(converted).lower() != str(
                            original_value
                        ).lower()
                elif isinstance(value, (int, float)):
                    converted = bool(value)
                    return converted, str(converted).lower() != str(
                        original_value
                    ).lower()
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
                converted = str(value).strip()
                return converted, converted != str(original_value)
            elif expected_type is list:
                if isinstance(value, list):
                    return value, str(value) != str(original_value)
                if isinstance(value, str):
                    items = json.loads(value)
                    if isinstance(original_value, list):
                        return items, items != original_value
                    else:
                        return items, str(items) != str(original_value)
            else:
                return value, str(value) != str(original_value)
        except (ValueError, TypeError) as e:
            if raise_exception:
                raise ValueError(
                    f"配置项 '{field_name}' 的值 '{value}' 无法转换成正确的类型"
                ) from e
            logger.error(
                f"配置项 '{field_name}' 的值 '{value}' 无法转换成正确的类型，使用默认值 '{default}'，错误信息: {e}"
            )
        return default, True

    @model_validator(mode="before")
    @classmethod
    def generic_type_validator(cls, data: Any):  # noqa
        """
        通用校验器，尝试将配置值转换为期望的类型
        """
        if not isinstance(data, dict):
            return data

        # 处理 API_TOKEN 特殊验证
        if "API_TOKEN" in data:
            converted_value, needs_update = cls.validate_api_token(
                data["API_TOKEN"], data["API_TOKEN"]
            )
            if needs_update:
                cls.update_env_config("API_TOKEN", data["API_TOKEN"], converted_value)
                data["API_TOKEN"] = converted_value

        # 对其他字段进行类型转换
        for field_name, field_info in cls.model_fields.items():
            if field_name not in data:
                continue
            value = data[field_name]
            if value is None:
                continue

            field = cls.model_fields.get(field_name)
            if field:
                converted_value, needs_update = cls.generic_type_converter(
                    value, value, field.annotation, field.default, field_name
                )
                if needs_update:
                    cls.update_env_config(field_name, value, converted_value)
                    data[field_name] = converted_value

        return data

    @staticmethod
    def update_env_config(
        field_name: str, original_value: Any, converted_value: Any
    ) -> Tuple[bool, str]:
        """
        更新 env 配置
        """
        message = None
        is_converted = original_value is not None and str(original_value) != str(
            converted_value
        )
        if is_converted:
            message = f"配置项 '{field_name}' 的值 '{original_value}' 无效，已替换为 '{converted_value}'"
            logger.warning(message)

        if field_name in os.environ:
            message = (
                f"配置项 '{field_name}' 已在环境变量中设置，请手动更新以保持一致性"
            )
            logger.warning(message)
            return False, message
        else:
            # 当值为 None 时，从 env 文件中删除该键，恢复为默认值
            if converted_value is None:
                unset_key(
                    dotenv_path=SystemUtils.get_env_path(),
                    key_to_unset=field_name,
                )
                logger.info(f"配置项 '{field_name}' 已清空，从 'app.env' 中移除")
                return True, message
            # 如果是列表、字典或集合类型，将其转换为JSON字符串
            if isinstance(converted_value, (list, dict, set)):
                value_to_write = json.dumps(converted_value)
            else:
                value_to_write = str(converted_value)

            set_key(
                dotenv_path=SystemUtils.get_env_path(),
                key_to_set=field_name,
                value_to_set=value_to_write,
                quote_mode="always",
            )
            if is_converted:
                logger.info(f"配置项 '{field_name}' 已自动修正并写入到 'app.env' 文件")
        return True, message

    def update_setting(self, key: str, value: Any) -> Tuple[Optional[bool], str]:
        """
        更新单个配置项
        :param key: 配置项的名称
        :param value: 配置项的新值
        :return: (是否成功 True 成功/False 失败/None 无需更新, 错误信息)
        """
        if not hasattr(self, key):
            return False, f"配置项 '{key}' 不存在"

        try:
            field = Settings.model_fields.get(key)
            if not field:
                return False, f"配置项 '{key}' 不存在"
            original_value = getattr(self, key)
            if key == "API_TOKEN":
                converted_value, needs_update = self.validate_api_token(
                    value, original_value
                )
            else:
                converted_value, needs_update = self.generic_type_converter(
                    value, original_value, field.annotation, field.default, key
                )
            # 如果没有抛出异常，则统一使用 converted_value 进行更新
            if needs_update or str(value) != str(converted_value):
                success, message = self.update_env_config(key, value, converted_value)
                # 仅成功更新配置时，才更新内存
                if success:
                    setattr(self, key, converted_value)
                    if hasattr(log_settings, key):
                        setattr(log_settings, key, converted_value)
                return success, message
            return None, ""
        except Exception as e:
            return False, str(e)

    def update_settings(
        self, env: Dict[str, Any]
    ) -> Dict[str, Tuple[Optional[bool], str]]:
        """
        更新多个配置项
        """
        results = {}
        for k, v in env.items():
            results[k] = self.update_setting(k, v)
        return results

    @property
    def VERSION_FLAG(self) -> str:
        """
        版本标识，用来区分重大版本，为空则为v1，不允许外部修改
        """
        return "v2"

    @property
    def USER_AGENT(self) -> str:
        """
        全局用户代理字符串
        """
        return f"{self.PROJECT_NAME}/{APP_VERSION[1:]} ({platform.system()} {platform.release()}; {SystemUtils.cpu_arch()})"

    @property
    def NORMAL_USER_AGENT(self) -> str:
        """
        默认浏览器用户代理字符串
        """
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

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
    def PACKAGE_CACHE_PATH(self):
        if self.PACKAGE_CACHE_ROOT and self.PACKAGE_CACHE_ROOT.strip():
            return Path(self.PACKAGE_CACHE_ROOT).expanduser()
        return self.CONFIG_PATH / ".cache"

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
    def CONF(self) -> SystemConfModel:
        """
        根据内存模式返回系统配置
        """
        if self.BIG_MEMORY_MODE:
            return SystemConfModel(
                torrents=200,
                refresh=100,
                tmdb=1024,
                douban=512,
                bangumi=512,
                fanart=512,
                meta=(self.META_CACHE_EXPIRE or 72) * 3600,
                scheduler=100,
                threadpool=100,
            )
        return SystemConfModel(
            torrents=100,
            refresh=50,
            tmdb=256,
            douban=256,
            bangumi=256,
            fanart=128,
            meta=(self.META_CACHE_EXPIRE or 24) * 3600,
            scheduler=50,
            threadpool=50,
        )

    @property
    def PROXY(self) -> Optional[Dict[str, str]]:
        """
        获取 requests 兼容的系统代理配置。
        """
        if self.PROXY_HOST and self.PROXY_HOST.strip():
            proxy_host = self.PROXY_HOST.strip()
            return {
                "http": proxy_host,
                "https": proxy_host,
            }
        https_proxy = self._get_env_proxy("HTTPS_PROXY", "https_proxy")
        http_proxy = self._get_env_proxy("HTTP_PROXY", "http_proxy")
        proxy_host = https_proxy or http_proxy
        if proxy_host:
            return {
                "http": http_proxy or proxy_host,
                "https": https_proxy or proxy_host,
            }
        return None

    @staticmethod
    def _get_env_proxy(*names: str) -> Optional[str]:
        """
        按顺序读取非空代理环境变量。
        """
        for name in names:
            proxy_host = os.environ.get(name)
            if proxy_host and proxy_host.strip():
                return proxy_host.strip()
        return None

    @property
    def DB_POSTGRESQL_SOCKET_MODE(self) -> bool:
        host = (self.DB_POSTGRESQL_HOST or "").strip()
        return host.startswith("/")

    @property
    def DB_POSTGRESQL_TARGET(self) -> str:
        if self.DB_POSTGRESQL_SOCKET_MODE:
            target = f"socket {self.DB_POSTGRESQL_HOST}"
            if self.DB_POSTGRESQL_PORT:
                target = f"{target} (port {self.DB_POSTGRESQL_PORT})"
            return target
        if self.DB_POSTGRESQL_PORT:
            return f"{self.DB_POSTGRESQL_HOST}:{self.DB_POSTGRESQL_PORT}"
        return self.DB_POSTGRESQL_HOST

    def DB_POSTGRESQL_URL(self, driver: Optional[str] = None) -> str:
        scheme = "postgresql" if not driver else f"postgresql+{driver}"
        username = quote(str(self.DB_POSTGRESQL_USERNAME), safe="")
        database = quote(str(self.DB_POSTGRESQL_DATABASE), safe="")
        auth = username
        if self.DB_POSTGRESQL_PASSWORD:
            auth = f"{auth}:{quote(str(self.DB_POSTGRESQL_PASSWORD), safe='')}"

        if self.DB_POSTGRESQL_SOCKET_MODE:
            query = {"host": self.DB_POSTGRESQL_HOST}
            if self.DB_POSTGRESQL_PORT:
                query["port"] = self.DB_POSTGRESQL_PORT
            return f"{scheme}://{auth}@/{database}?{urlencode(query)}"

        port = f":{self.DB_POSTGRESQL_PORT}" if self.DB_POSTGRESQL_PORT else ""
        return f"{scheme}://{auth}@{self.DB_POSTGRESQL_HOST}{port}/{database}"

    @property
    def PROXY_SERVER(self):
        if self.PROXY_HOST and self.PROXY_HOST.strip():
            try:
                parsed = urlparse(self.PROXY_HOST)
                if not parsed.scheme:
                    return {"server": self.PROXY_HOST}
                host = parsed.hostname or ""
                port = f":{parsed.port}" if parsed.port else ""
                server = f"{parsed.scheme}://{host}{port}"
                proxy = {"server": server}
                if parsed.username:
                    proxy["username"] = parsed.username
                if parsed.password:
                    proxy["password"] = parsed.password
                return proxy
            except Exception as err:
                logger.error(f"解析代理服务器地址 '{self.PROXY_HOST}' 时出错: {err}")
                return {"server": self.PROXY_HOST}
        return None

    @property
    def GITHUB_HEADERS(self):
        """
        Github请求头
        """
        if self.GITHUB_TOKEN:
            return {
                "Authorization": f"Bearer {self.GITHUB_TOKEN}",
                "User-Agent": self.NORMAL_USER_AGENT,
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
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self.NORMAL_USER_AGENT,
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
            "privateKey": "JTixnYY0vEw97t9uukfO3UWKfHKJdT5kCQDiv3gu894",
        }

    def MP_DOMAIN(self, url: str = None):
        if not self.APP_DOMAIN:
            return None
        return UrlUtils.combine_url(host=self.APP_DOMAIN, path=url)

    def RENAME_FORMAT(self, media_type: MediaType):
        """
        获取指定类型的重命名格式

        :param media_type: MediaType.TV 或 MediaType.Movie
        :return: 重命名格式
        """
        rename_format = (
            self.TV_RENAME_FORMAT
            if media_type == MediaType.TV
            else self.MOVIE_RENAME_FORMAT
        )
        # 规范重命名格式
        rename_format = rename_format.replace("\\", "/")
        rename_format = re.sub(r"/+", "/", rename_format)
        return rename_format.strip("/")

    def TMDB_IMAGE_URL(
        self, file_path: Optional[str], file_size: str = "original"
    ) -> Optional[str]:
        """
        获取TMDB图片网址

        :param file_path: TMDB API返回的xxx_path
        :param file_size: 图片大小，例如：'original', 'w500' 等
        :return: 图片的完整URL，如果 file_path 为空则返回 None
        """
        if not file_path:
            return None
        return f"https://{self.TMDB_IMAGE_DOMAIN}/t/p/{file_size}/{file_path.removeprefix('/')}"


# 实例化配置
settings = Settings()


class GlobalVar(object):
    """
    全局标识
    """

    # 系统停止事件
    STOP_EVENT: threading.Event = threading.Event()
    # webpush订阅
    SUBSCRIPTIONS: List[dict] = []
    # webpush订阅读写锁
    SUBSCRIPTIONS_LOCK: threading.Lock = threading.Lock()
    # 需应急停止的工作流
    EMERGENCY_STOP_WORKFLOWS: List[int] = []
    # 需应急停止文件整理
    EMERGENCY_STOP_TRANSFER: List[str] = []
    # 当前事件循环
    CURRENT_EVENT_LOOP: AbstractEventLoop = None

    @classmethod
    def _get_event_loop(cls) -> AbstractEventLoop:
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def stop_system(self):
        """
        停止系统
        """
        self.STOP_EVENT.set()

    def resume_system(self):
        """
        恢复系统运行标记。
        """
        self.STOP_EVENT.clear()

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
        with self.SUBSCRIPTIONS_LOCK:
            return list(self.SUBSCRIPTIONS)

    def push_subscription(self, subscription: dict):
        """
        添加或更新webpush订阅。
        """
        endpoint = subscription.get("endpoint") if subscription else None
        if not endpoint:
            return
        with self.SUBSCRIPTIONS_LOCK:
            for index, current in enumerate(self.SUBSCRIPTIONS):
                if current.get("endpoint") == endpoint:
                    self.SUBSCRIPTIONS[index] = subscription
                    return
            self.SUBSCRIPTIONS.append(subscription)

    def remove_subscription(self, subscription: dict) -> bool:
        """
        根据 endpoint 移除webpush订阅，返回是否实际删除。
        """
        endpoint = subscription.get("endpoint") if subscription else None
        if not endpoint:
            return False
        with self.SUBSCRIPTIONS_LOCK:
            before_count = len(self.SUBSCRIPTIONS)
            self.SUBSCRIPTIONS[:] = [
                current for current in self.SUBSCRIPTIONS
                if current.get("endpoint") != endpoint
            ]
            return len(self.SUBSCRIPTIONS) != before_count

    def stop_workflow(self, workflow_id: int):
        """
        停止工作流
        """
        if workflow_id not in self.EMERGENCY_STOP_WORKFLOWS:
            self.EMERGENCY_STOP_WORKFLOWS.append(workflow_id)

    def workflow_resume(self, workflow_id: int):
        """
        恢复工作流
        """
        if workflow_id in self.EMERGENCY_STOP_WORKFLOWS:
            self.EMERGENCY_STOP_WORKFLOWS.remove(workflow_id)

    def is_workflow_stopped(self, workflow_id: int) -> bool:
        """
        是否停止工作流
        """
        return self.is_system_stopped or workflow_id in self.EMERGENCY_STOP_WORKFLOWS

    def stop_transfer(self, path: str):
        """
        停止文件整理
        """
        if path not in self.EMERGENCY_STOP_TRANSFER:
            self.EMERGENCY_STOP_TRANSFER.append(path)

    def is_transfer_stopped(self, path: str) -> bool:
        """
        是否停止文件整理
        """
        if self.is_system_stopped:
            return True
        if path in self.EMERGENCY_STOP_TRANSFER:
            self.EMERGENCY_STOP_TRANSFER.remove(path)
            return True
        return False

    @property
    def loop(self) -> AbstractEventLoop:
        """
        当前循环
        """
        if self.CURRENT_EVENT_LOOP is None:
            self.CURRENT_EVENT_LOOP = self._get_event_loop()
        return self.CURRENT_EVENT_LOOP

    def set_loop(self, loop: AbstractEventLoop):
        """
        设置循环
        """
        self.CURRENT_EVENT_LOOP = loop


# 全局标识
global_vars = GlobalVar()
