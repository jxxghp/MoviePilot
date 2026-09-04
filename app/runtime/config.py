import copy
import json
import os
import platform
import re
import secrets
import shutil
import sys
import threading
from asyncio import AbstractEventLoop
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union, get_args, get_origin
from urllib.parse import quote, urlencode, urlparse

from dotenv import set_key, unset_key
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.foundation.environment import (
    cpu_arch,
    get_env_path,
    is_docker,
    is_free_threaded_runtime,
    is_frozen,
)
from app.foundation.url import UrlUtils
from app.runtime.log import (
    LogConfigModel,
    configure_log_settings,
    log_settings,
    logger,
)
from app.runtime.loop import MainLoopRegistry, main_loop_registry
from app.runtime.stop import runtime_stop_state
from app.runtime.version import get_app_version
from app.runtime.webpush import WebPushRegistry, webpush_registry
from app.schemas.types import MediaType


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
    # AniList请求缓存数量
    anilist: int = 0
    # IMDb请求缓存数量
    imdb: int = 0
    # Fanart请求缓存数量
    fanart: int = 0
    # MusicBrainz请求缓存数量
    musicbrainz: int = 0
    # TheAudioDB请求缓存数量
    theaudiodb: int = 0
    # ListenBrainz请求缓存数量
    listenbrainz: int = 0
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
    # 是否启用 Btrfs FSID 子卷容量去重（仅 Linux amd64/arm64）
    BTRFS_FSID_DEDUP: bool = False
    # 是否调试模式
    DEBUG: bool = False
    # 是否开发模式
    DEV: bool = False
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
    # 超级管理员用户名；V3 首次启动时为空，由初始化页面设置
    SUPERUSER: str = ""
    # 超级管理员密码不再通过部署配置初始化，始终由数据库存储哈希
    SUPERUSER_PASSWORD: str = ""
    # 辅助认证，允许通过外部服务进行认证、单点登录以及自动创建用户
    AUXILIARY_AUTH_ENABLE: bool = False
    # API密钥，需要更换
    API_TOKEN: Optional[str] = None
    # 用户认证站点
    AUTH_SITE: str = ""

    # ==================== 数据库配置 ====================
    # 数据库类型，支持 sqlite 和 postgresql，默认使用 sqlite
    # API 服务的 worker 进程数。连接池是进程级的，每个 worker 各持一份，
    # 数据库连接额度校验按它换算总用量。注意：当前主程序以单进程方式启动
    # （uvicorn.Config 的 workers 仅在多进程 supervisor 路径下生效），
    # 调大此项前需先解决调度器会在每个 worker 内重复执行的问题
    API_WORKERS: int = Field(default=1, ge=1)
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
    # 异步连接池类型：QueuePool / NullPool。
    # NullPool 下每个异步会话独占一条物理连接、零复用且无上限，突发并发会直接顶穿
    # PostgreSQL 的 max_connections（表现为 TooManyConnectionsError），在 SQLite 上
    # 则表现为 WAL 写争用导致的长时间卡顿。默认 QueuePool：仅对常驻主事件循环池化，
    # 其余事件循环自动回退 NullPool，避免跨循环复用连接。遇到兼容问题可设为 NullPool
    # 回到旧行为。
    DB_ASYNC_POOL_TYPE: str = "QueuePool"
    # 异步连接池大小（每个被池化的事件循环）
    DB_ASYNC_POOL_SIZE: int = 5
    # 异步连接池溢出数量（每个被池化的事件循环）
    DB_ASYNC_MAX_OVERFLOW: int = 10
    # 未被池化的事件循环（临时循环）共享的全局并发连接配额。
    # 池化路径由连接池自身限流，这里只为 NullPool 兜底路径补上背压，
    # 防止临时循环上的突发并发再次无界增长。
    # 实测常驻的 feishu/discord 循环不访问数据库，走此路径的只有插件与调度器
    # 兜底分支的零星调用，因此取值不必大——它直接计入连接总额度
    DB_ASYNC_FALLBACK_LIMIT: int = 10
    # 驱动级连接参数，透传给 create_engine/create_async_engine 的 connect_args。
    # 例如经 PgBouncer 事务模式接入时 asyncpg 需要 {"statement_cache_size": 0}
    DB_CONNECT_ARGS: dict = Field(default_factory=dict)

    # ==================== 数据库备份配置 ====================
    # 是否启用主程序数据库自动备份
    DB_BACKUP_ENABLE: bool = False
    # 定时备份的 Cron 表达式，留空时不注册定时任务
    DB_BACKUP_CRON: str = "0 3 * * *"
    # 检测到现有数据库需要迁移时，在结构变更前创建恢复点
    DB_BACKUP_ON_UPGRADE: bool = True
    # 备份根目录；未配置时使用 CONFIG_PATH/database_backup
    DB_BACKUP_PATH: Optional[str] = None
    # 本地备份的保留天数，0 表示不按时间清理
    DB_BACKUP_RETENTION_DAYS: int = 30
    # 本地备份的最大保留份数，0 表示不按数量清理
    DB_BACKUP_MAX_COUNT: int = 30
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
    # 下载失败冷却记录保留天数，0为不清理
    DATA_CLEANUP_DOWNLOAD_FAILURE_DAYS: int = 7
    # 订阅完成历史保留天数，0为不清理
    DATA_CLEANUP_SUBSCRIBE_HISTORY_DAYS: int = 365 * 3
    # Agent 会话历史保留天数，0为不清理
    DATA_CLEANUP_AGENT_CHAT_DAYS: int = 180
    # Agent 定时任务运行历史保留天数，0为不清理
    DATA_CLEANUP_AGENT_TASK_RUN_DAYS: int = 180
    # Outbox 已完成记录保留天数，0为不清理
    DATA_CLEANUP_OUTBOX_COMPLETED_DAYS: int = 30
    # Outbox 死信记录保留天数，0为不清理
    DATA_CLEANUP_OUTBOX_DEAD_DAYS: int = 90

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
    # 单条元数据识别缓存有效期（小时），0为自动
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
    # 媒体搜索来源 themoviedb/douban/bangumi/anilist/imdb/musicbrainz/theaudiodb/doubanmusic，多个用,分隔
    SEARCH_SOURCE: str = "themoviedb"
    # 媒体识别来源 themoviedb/douban/bangumi/anilist/imdb/musicbrainz/theaudiodb/doubanmusic
    RECOGNIZE_SOURCE: str = "themoviedb"
    # 刮削来源 themoviedb/douban/bangumi/anilist/imdb/musicbrainz/theaudiodb/doubanmusic
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
    # TMDB空结果缓存独立过期时间（秒），故障期间产生的空响应快速过期，故障自愈后可自然恢复
    EMPTY_RESULT_CACHE_TTL: int = 30 * 60
    # 刮削使用TMDB原始语种图片
    TMDB_SCRAP_ORIGINAL_IMAGE: bool = False
    # TMDB API Key
    TMDB_API_KEY: str = "db55323b8d3e4154498498a75642b381"

    # ==================== 音乐配置 ====================
    # 音乐封面代理地址（用于解决 coverartarchive.org 无法访问导致的封面不显示问题，留空则使用官方地址）
    MUSIC_COVER_PROXY: str = ""
    # AcoustID 应用 API Key，用于查询本地音频的 Chromaprint 指纹
    ACOUSTID_API_KEY: str = "b1auxfOzAg"
    # 是否将识别到的音乐标题、艺术家、专辑等标准元数据转换为简体中文
    MUSIC_METADATA_TO_SIMPLIFIED: bool = True
    # TheAudioDB API Key，默认使用官方公开的免费 V1 Key，可通过环境变量覆盖
    THEAUDIODB_API_KEY: str = "123"
    # LRCLIB 服务地址，可指向兼容官方 API 的自建实例
    LRCLIB_BASE_URL: str = "https://lrclib.net"
    # Musixmatch 官方 API Key；留空时不加载该歌词来源
    MUSIXMATCH_API_KEY: str = ""
    # Musixmatch 官方或授权代理 API 根地址
    MUSIXMATCH_BASE_URL: str = "https://api.musixmatch.com/ws/1.1"
    # 单次音乐刮削批次用于在线歌词查询的总预算（秒）
    LYRICS_BATCH_TIMEOUT: int = 120
    # 供应商要求的重试等待超过该值时进入冷却，不阻塞整个批次
    LYRICS_PROVIDER_RETRY_MAX_WAIT: int = 5

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
    # 开发版仍可在启动时跟踪 v3 分支；Release 更新由后台更新服务管理。
    MOVIEPILOT_AUTO_UPDATE: str = "false"
    # 后台检查站点资源包，确认后由启动器在进程拉起前应用
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
            ".ape",
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

    # ==================== 目录监控配置 ====================
    # 允许网络文件系统使用快速模式（inotify）。部分 FUSE 实现（如 CloudDrive2）
    # 会正常下发内核通知，快速模式可用，且比每 N 秒 stat 全部目录的轮询对挂载后端
    # 的压力小得多，由用户确认后开启
    MONITOR_NETWORK_FAST_MODE: bool = False
    # 网络文件系统的轮询扫描间隔（毫秒），0 表示使用内置默认值
    MONITOR_POLL_DELAY_NETWORK: int = 0
    # 新增目录延迟重扫的轮次延迟秒数，多个使用,分隔，如 "30,120,600,1800"。
    # FUSE 挂载（如 CloudDrive2）上超大目录树呈现完整内容可能超过分钟级，
    # 默认值在常见的 30/120 秒窗口后追加两轮成本极低的长延迟轮次兜底；
    # 解析失败（如格式非法、包含非正整数）时回退默认值并记录 warn 日志
    MONITOR_RESCAN_DELAYS: str = "30,120,600,1800"

    # ==================== CookieCloud配置 ====================
    # CookieCloud是否启动本地服务
    COOKIECLOUD_ENABLE_LOCAL: Optional[bool] = False
    # CookieCloud服务器地址
    COOKIECLOUD_HOST: str = "https://movie-pilot.org/cookiecloud"
    # CookieCloud用户KEY
    COOKIECLOUD_KEY: Optional[str] = None
    # CookieCloud端对端加密密码
    COOKIECLOUD_PASSWORD: Optional[str] = None
    # CookieCloud本地上传接口的X-CookieCloud-Auth期望值，留空表示不校验
    COOKIECLOUD_AUTH_HEADER: Optional[str] = None
    # CookieCloud同步间隔（分钟）
    COOKIECLOUD_INTERVAL: Optional[int] = 60 * 24
    # CookieCloud同步黑名单，多个域名,分割
    COOKIECLOUD_BLACKLIST: Optional[str] = None

    # ==================== 整理配置 ====================
    # 文件整理线程数
    TRANSFER_THREADS: int = 1
    # 本地文件操作是否走可强杀的子进程代理。
    # FUSE/网络挂载进入「请求永不返回」状态时，stat/listdir 这类调用会永久悬挂，
    # 而 Python 既不能中断已发出的系统调用、也不能强杀线程，阻塞其上的线程无法回收。
    # 走子进程后超时可以 SIGKILL 真正回收，block 型故障被转换成各层已能处理的
    # crash 型故障（OSError）。出现兼容问题时可关闭回到直接调用。
    FS_PROXY_ENABLED: bool = True
    # 单次本地快操作（stat/listdir/删除/重命名）的超时秒数，
    # 超时即判定挂载无响应并回收代理进程
    FS_PROXY_TIMEOUT: int = 30
    # 复制大文件时两次进度上报之间的最长间隔秒数。代理每秒上报一次进度作为心跳，
    # 因此这个阈值判定的是「传输完全没有推进」而不是「传输很慢」，
    # 复制几小时的大文件也不会被误杀
    FS_PROXY_STALL_TIMEOUT: int = 120
    # 自动整理（目录监控、下载器轮询）遇到失败整理记录时，同一源路径允许自动重试的最大次数。
    # 一次瞬时故障（网络抖动、TMDB 瞬断、移动失败）不该让文件永久漏整理，因此必须重试；
    # 但永远识别不出的文件重试再多也不会成功，只会重复推送失败通知，批量导入时更会刷屏，
    # 因此必须有界。取值区间 1-10，越界或非法值会被钳制到边界并记录 warn：
    # 既不支持关闭重试，也不支持无限重试。整理成功或删除整理记录时计数清零。
    # 与本项无关的是同路径新版本：已成功整理的文件源大小变化时一律放行，
    # 由整理链的 overwrite_mode 决断是否覆盖
    TRANSFER_MAX_FAILED_RETRIES: int = 3
    # 外部接管的运行中整理任务无状态心跳超时（分钟），0 表示禁用
    TRANSFER_TASK_TIMEOUT: int = 120
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
    # 音乐重命名格式
    MUSIC_RENAME_FORMAT: str = (
        "{{album_artist or artist or 'Unknown Artist'}}"
        "/{{album or 'Unknown Album'}}{% if year %} ({{year}}){% endif %}"
        "{% if total_discs and total_discs > 1 %}/Disc {{disc_number or 1}}{% endif %}"
        "/{% if track %}{{track}} - {% endif %}{{title}}{{fileExt}}"
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
    # 登录页面壁纸来源：tmdb/bing/mediaserver/customize/static
    WALLPAPER: str = "tmdb"
    # 壁纸轮换间隔（秒），0 表示不轮换
    WALLPAPER_ROTATION_INTERVAL: int = 15
    # 静态壁纸地址，可使用前端可访问的本地路径或 URL
    WALLPAPER_IMAGE_URL: Optional[str] = None
    # 自定义壁纸api地址
    CUSTOMIZE_WALLPAPER_API_URL: Optional[str] = None

    # ==================== 插件配置 ====================
    # 插件市场仓库地址，多个地址使用,分隔，地址以/结尾
    PLUGIN_MARKET: str = (
        "https://github.com/jxxghp/MoviePilot-Plugins"
    )
    # 插件安装数据共享
    PLUGIN_STATISTIC_SHARE: bool = True
    # 安装版本统计上报
    USAGE_STATISTIC_SHARE: bool = True
    # 是否开启插件热加载
    PLUGIN_AUTO_RELOAD: bool = False
    # 临时放行的废弃标识，多个用,分隔；仅对已进入停用阶段的接口有效，用于观察真实依赖方
    DEPRECATION_ENABLED: Optional[str] = None
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
    # Rust 加速总开关，free-threaded 运行时固定启用
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
            "anilist.co",
            "coverartarchive.org",
            "archive.org",
            "theaudiodb.com",
            "commons.wikimedia.org",
            "upload.wikimedia.org",
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
    # OpenAI兼容接口API协议：auto（自动）/ chat_completions / responses
    LLM_API_PROTOCOL: str = "auto"
    # 联网搜索模式：local（本地）/ builtin（模型服务端）/ auto（自动）/ disabled（关闭）
    LLM_WEB_SEARCH_MODE: str = "local"
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
    # LLM最大上下文Token数量（K），用于目录缺失回退和未匹配兼容端点的保守上限
    LLM_MAX_CONTEXT_TOKENS: int = 256
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
    LLM_MAX_TOOLS: int = 0
    # AI智能体定时任务检查间隔（小时），0为不启用，默认24小时
    AI_AGENT_JOB_INTERVAL: int = 0
    # AI智能体啰嗦模式，开启后会回复工具调用过程
    AI_AGENT_VERBOSE: bool = False
    # AI智能体自动重试整理失败记录开关
    AI_AGENT_RETRY_TRANSFER: bool = False
    # 是否按媒体聚合整理失败通知，关闭时保持逐条发送
    TRANSFER_FAILURE_NOTIFICATION_AGGREGATION: bool = True

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
        env_file=get_env_path(),
        env_file_encoding="utf-8",
    )

    def __init__(self, **kwargs):
        """加载环境配置并确保运行时目录和内置配置可用。"""
        super().__init__(**kwargs)
        # 初始化配置目录及子目录
        for path in [self.CONFIG_PATH, self.TEMP_PATH, self.LOG_PATH, self.COOKIE_PATH]:
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
        # 如果是二进制程序，确保配置文件存在
        if is_frozen():
            app_env_path = self.CONFIG_PATH / "app.env"
            if not app_env_path.exists():
                shutil.copy2(self.INNER_CONFIG_PATH / "app.env", app_env_path)

    @staticmethod
    def validate_api_token(value: Any, original_value: Any) -> Tuple[Any, bool]:
        """
        校验 API_TOKEN
        """
        if isinstance(value, (list, dict, set)):
            value = copy.deepcopy(value)
        value = value.strip() if isinstance(value, str) else None
        if not value:
            return None, str(original_value) not in {"", "None"}
        if len(value) < 16:
            new_token = secrets.token_urlsafe(16)
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
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    converted = float(value)
                    return converted, str(converted) != str(original_value)
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

        # Release 已迁移到后台状态机，历史 release/true 不能继续启用启动时更新。
        if "MOVIEPILOT_AUTO_UPDATE" in data:
            original_update_mode = data["MOVIEPILOT_AUTO_UPDATE"]
            normalized_update_mode = (
                "dev"
                if str(original_update_mode or "").strip().lower() == "dev"
                else "false"
            )
            if normalized_update_mode != str(original_update_mode):
                cls.update_env_config(
                    "MOVIEPILOT_AUTO_UPDATE",
                    original_update_mode,
                    normalized_update_mode,
                )
                data["MOVIEPILOT_AUTO_UPDATE"] = normalized_update_mode

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
        # 成功且无提示时使用空字符串，保证与 Tuple[bool, str] 返回类型一致
        message = ""
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
                    dotenv_path=get_env_path(),
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
                dotenv_path=get_env_path(),
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
            if (
                key == "RUST_ACCEL"
                and is_free_threaded_runtime()
                and converted_value is not True
            ):
                return False, "free-threaded 运行时必须启用 Rust 加速"
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
        return "v3"

    @property
    def RESOURCE_VERSION_FLAG(self) -> str:
        """返回站点索引和认证资源使用的重大版本标识。"""
        return "v3"

    @property
    def USER_AGENT(self) -> str:
        """
        全局用户代理字符串
        """
        return (
            f"{self.PROJECT_NAME}/{get_app_version()[1:]} "
            f"({platform.system()} {platform.release()}; {cpu_arch()})"
        )

    @property
    def NORMAL_USER_AGENT(self) -> str:
        """
        默认浏览器用户代理字符串
        """
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

    @property
    def INNER_CONFIG_PATH(self):
        """返回随程序发布的内置配置目录。"""
        return self.ROOT_PATH / "config"

    @property
    def CONFIG_PATH(self):
        """按显式配置、容器和冻结运行环境确定配置目录。"""
        if self.CONFIG_DIR:
            return Path(self.CONFIG_DIR)
        elif is_docker():
            return Path("/config")
        elif is_frozen():
            return Path(sys.executable).parent / "config"
        return self.ROOT_PATH / "config"

    @property
    def TEMP_PATH(self):
        """返回运行时临时目录。"""
        return self.CONFIG_PATH / "temp"

    @property
    def CACHE_PATH(self):
        """返回业务缓存目录。"""
        return self.CONFIG_PATH / "cache"

    @property
    def PACKAGE_CACHE_PATH(self):
        """返回 pip 和 uv 等包工具使用的缓存根目录。"""
        if self.PACKAGE_CACHE_ROOT and self.PACKAGE_CACHE_ROOT.strip():
            return Path(self.PACKAGE_CACHE_ROOT).expanduser()
        return self.CONFIG_PATH / ".cache"

    @property
    def ROOT_PATH(self):
        """返回项目源码或冻结程序的根目录。"""
        return Path(__file__).parents[2]

    @property
    def PLUGIN_DATA_PATH(self):
        """返回插件持久化数据目录。"""
        return self.CONFIG_PATH / "plugins"

    @property
    def DATABASE_BACKUP_PATH(self) -> Path:
        """返回数据库备份根目录，允许相对当前配置目录进行配置。"""
        configured = str(self.DB_BACKUP_PATH or "").strip()
        if not configured:
            return self.CONFIG_PATH / "database_backup"
        path = Path(configured).expanduser()
        return path if path.is_absolute() else self.CONFIG_PATH / path

    @property
    def LOG_PATH(self):
        """返回应用日志目录。"""
        return self.CONFIG_PATH / "logs"

    @property
    def COOKIE_PATH(self):
        """返回站点 Cookie 文件目录。"""
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
                imdb=512,
                fanart=512,
                musicbrainz=512,
                theaudiodb=512,
                listenbrainz=256,
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
            imdb=256,
            fanart=128,
            musicbrainz=256,
            theaudiodb=256,
            listenbrainz=128,
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
        """判断 PostgreSQL 主机配置是否为 Unix Socket 路径。"""
        host = (self.DB_POSTGRESQL_HOST or "").strip()
        return host.startswith("/")

    @property
    def DB_POSTGRESQL_TARGET(self) -> str:
        """返回适合日志展示的 PostgreSQL 连接目标。"""
        if self.DB_POSTGRESQL_SOCKET_MODE:
            target = f"socket {self.DB_POSTGRESQL_HOST}"
            if self.DB_POSTGRESQL_PORT:
                target = f"{target} (port {self.DB_POSTGRESQL_PORT})"
            return target
        if self.DB_POSTGRESQL_PORT:
            return f"{self.DB_POSTGRESQL_HOST}:{self.DB_POSTGRESQL_PORT}"
        return self.DB_POSTGRESQL_HOST

    def DB_SQLITE_URL(self, driver: Optional[str] = None) -> str:
        """
        SQLite 连接串。与 DB_POSTGRESQL_URL 对称，避免各调用点各自拼接后悄悄漂移
        ——迁移与应用连到不同的库文件是不会报错的。
        :param driver: 驱动名，如 aiosqlite；留空为同步驱动
        """
        scheme = "sqlite" if not driver else f"sqlite+{driver}"
        return f"{scheme}:///{self.CONFIG_PATH}/user.db"

    def DB_POSTGRESQL_URL(self, driver: Optional[str] = None) -> str:
        """按同步或异步驱动构造 PostgreSQL SQLAlchemy URL。"""
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
        """返回浏览器自动化使用的代理服务器配置。"""
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
        """返回 Web Push 使用的 VAPID 配置。"""
        return {
            "subject": f"mailto:{self.SUPERUSER or 'moviepilot'}@movie-pilot.org",
            "publicKey": "BH3w49sZA6jXUnE-yt4jO6VKh73lsdsvwoJ6Hx7fmPIDKoqGiUl2GEoZzy-iJfn4SfQQcx7yQdHf9RknwrL_lSM",
            "privateKey": "JTixnYY0vEw97t9uukfO3UWKfHKJdT5kCQDiv3gu894",
        }

    def MP_DOMAIN(self, url: str = None):
        """将相对路径组合为当前 MoviePilot 对外访问地址。"""
        if not self.APP_DOMAIN:
            return None
        return UrlUtils.combine_url(host=self.APP_DOMAIN, path=url)

    def RENAME_FORMAT(self, media_type: MediaType):
        """
        获取指定类型的重命名格式

        :param media_type: 电影、电视剧或音乐媒体类型
        :return: 重命名格式
        """
        if media_type == MediaType.TV:
            rename_format = self.TV_RENAME_FORMAT
        elif media_type == MediaType.MUSIC:
            rename_format = self.MUSIC_RENAME_FORMAT
        else:
            rename_format = self.MOVIE_RENAME_FORMAT
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
configure_log_settings(settings)


class GlobalVar(object):
    """
    全局标识
    """

    # 需应急停止的工作流
    EMERGENCY_STOP_WORKFLOWS: List[int] = []
    # 需应急停止文件整理
    EMERGENCY_STOP_TRANSFER: List[str] = []

    def __init__(
        self,
        *,
        loop_registry: Optional[MainLoopRegistry] = None,
        push_registry: Optional[WebPushRegistry] = None,
    ) -> None:
        """绑定兼容门面背后的显式 owner；普通实例保持循环状态隔离。"""
        self._loop_registry = loop_registry or MainLoopRegistry()
        self._push_registry = push_registry or webpush_registry

    @property
    def CURRENT_EVENT_LOOP(self) -> Optional[AbstractEventLoop]:
        """兼容旧代码读取未经可用性校验的主循环。"""
        return self._loop_registry.current

    @CURRENT_EVENT_LOOP.setter
    def CURRENT_EVENT_LOOP(self, loop: Optional[AbstractEventLoop]) -> None:
        """兼容旧测试和插件直接替换主循环投递目标。"""
        self._loop_registry.replace_compat(loop)

    @CURRENT_EVENT_LOOP.deleter
    def CURRENT_EVENT_LOOP(self) -> None:
        """兼容属性 patch 清理，删除时仅清空当前投递目标。"""
        self._loop_registry.replace_compat(None)

    @property
    def SUBSCRIPTIONS(self) -> List[dict]:
        """兼容旧代码在持锁后直接访问订阅列表。"""
        return self._push_registry.compat_items

    @property
    def SUBSCRIPTIONS_LOCK(self) -> threading.Lock:
        """兼容旧代码保护原始订阅列表的互斥锁。"""
        return self._push_registry.compat_lock

    @property
    def STOP_EVENT(self) -> threading.Event:
        """兼容旧代码读取进程停止事件。"""
        return runtime_stop_state.system_event

    @STOP_EVENT.setter
    def STOP_EVENT(self, event: threading.Event) -> None:
        """兼容旧测试替换事件，同时保持新 StopState 为唯一状态源。"""
        runtime_stop_state.replace_system_event(event)

    def stop_system(self):
        """
        停止系统
        """
        runtime_stop_state.stop_system()

    @property
    def is_system_stopped(self):
        """
        是否停止
        """
        return runtime_stop_state.is_system_stopped

    def get_subscriptions(self):
        """
        获取webpush订阅
        """
        return self._push_registry.list()

    def push_subscription(self, subscription: dict):
        """
        添加或更新webpush订阅。
        """
        self._push_registry.upsert(subscription)

    def remove_subscription(self, subscription: dict) -> bool:
        """
        根据 endpoint 移除webpush订阅，返回是否实际删除。
        """
        return self._push_registry.remove(subscription)

    def stop_workflow(self, workflow_id: int):
        """
        停止工作流
        """
        runtime_stop_state.stop_workflow(workflow_id)

    def workflow_resume(self, workflow_id: int):
        """
        恢复工作流
        """
        runtime_stop_state.resume_workflow(workflow_id)

    def is_workflow_stopped(self, workflow_id: int) -> bool:
        """
        是否停止工作流
        """
        return runtime_stop_state.is_workflow_stopped(workflow_id)

    def stop_transfer(self, path: str):
        """
        停止文件整理
        """
        runtime_stop_state.stop_transfer(path)

    def is_transfer_stopped(self, path: str) -> bool:
        """
        是否停止文件整理
        """
        return runtime_stop_state.consume_transfer_stop(path)

    @property
    def loop(self) -> AbstractEventLoop:
        """返回由应用生命周期登记的主事件循环。"""
        return self._loop_registry.require()

    def set_loop(self, loop: AbstractEventLoop) -> object:
        """登记主事件循环，并返回仅供当前生命周期释放的 owner。"""
        return self._loop_registry.register(loop)

    def clear_loop(self, owner: object) -> None:
        """释放指定 owner，保留仍然有效的其他生命周期登记。"""
        self._loop_registry.release(owner)


# 全局标识
global_vars = GlobalVar(
    loop_registry=main_loop_registry,
    push_registry=webpush_registry,
)
