"""
部署配置声明、环境文件更新与兼容迁移，提供进程运行所需的配置视图。
"""

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
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
    cast,
    get_args,
    get_origin,
)
from urllib.parse import quote, urlencode, urlparse

from dotenv import set_key, unset_key
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
)

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

SettingConverter = Callable[[Any, Any], Tuple[Any, bool]]
SettingMigration = Callable[[Dict[str, Any]], Dict[str, Tuple[Any, Any]]]
SettingValidator = Callable[[Any], Optional[str]]
SettingSerializer = Callable[[Any], str]


@dataclass(frozen=True, slots=True)
class SettingPolicy:
    """
    部署配置字段的专属处理策略，供通用更新流程按字段声明执行。

    :param before_convert: 通用类型转换前的值归一化函数；为 None 时不预处理
    :param converter: 替代通用转换的专用函数，返回转换值及是否需要更新；为 None 时使用通用规则
    :param migrate: 旧配置迁移函数，可修改输入字典并返回字段变更；为 None 时不迁移
    :param validate: 字段校验函数，返回错误说明或 None；为 None 时不额外校验
    :param serialize: 写入环境文件前的序列化函数；为 None 时使用通用格式
    :param sensitive: 是否在变更日志中隐藏字段原值
    """

    before_convert: Optional[Callable[[Any], Any]] = None
    converter: Optional[SettingConverter] = None
    migrate: Optional[SettingMigration] = None
    validate: Optional[SettingValidator] = None
    serialize: Optional[SettingSerializer] = None
    sensitive: bool = False


def _get_setting_policy(field_info: Any) -> Optional[SettingPolicy]:
    """
    读取 Pydantic 字段声明上的部署配置策略。
    """
    return next(
        (
            metadata
            for metadata in getattr(field_info, "metadata", ())
            if isinstance(metadata, SettingPolicy)
        ),
        None,
    )


def _normalize_legacy_update_mode(value: Any) -> Any:
    """
    将旧自动更新模式转换为布尔开关。
    """
    if isinstance(value, str) and value.strip().lower() in {"dev", "release"}:
        return True
    return value


def _migrate_legacy_update_mode(
    data: Dict[str, Any],
) -> Dict[str, Tuple[Any, Any]]:
    """
    迁移旧自动更新配置中的开发分支跟踪偏好。
    """
    if (
        str(data.get("MOVIEPILOT_AUTO_UPDATE", "")).strip().lower() != "dev"
        or "MOVIEPILOT_UPDATE_DEV" in data
    ):
        return {}

    data["MOVIEPILOT_UPDATE_DEV"] = True
    return {"MOVIEPILOT_UPDATE_DEV": (None, True)}


def _normalize_api_token(value: Any, original_value: Any) -> Tuple[Any, bool]:
    """
    校验并规范化 API_TOKEN，避免把令牌原文写入日志。
    """
    if isinstance(value, (list, dict, set)):
        value = copy.deepcopy(value)
    value = value.strip() if isinstance(value, str) else None
    if not value:
        return None, str(original_value) not in {"", "None"}
    if len(value) < 16:
        new_token = secrets.token_urlsafe(16)
        logger.warning(
            "'API_TOKEN' 长度不足 16 个字符，存在安全隐患，已随机生成新的安全令牌"
        )
        return new_token, True
    return value, str(value) != str(original_value)


def _validate_rust_accel(value: Any) -> Optional[str]:
    """
    校验 free-threaded 运行时的 Rust 加速约束。
    """
    if is_free_threaded_runtime() and value is not True:
        return "free-threaded 运行时必须启用 Rust 加速"
    return None


def _serialize_bool(value: Any) -> str:
    """
    将布尔配置按启动脚本兼容的形式持久化。
    """
    return str(value).lower()


class SystemConfModel(BaseModel):
    """
    系统关键资源大小配置。
    """

    torrents: int = Field(default=0, description='缓存种子数量')
    refresh: int = Field(default=0, description='订阅刷新处理数量')
    tmdb: int = Field(default=0, description='TMDB请求缓存数量')
    douban: int = Field(default=0, description='豆瓣请求缓存数量')
    bangumi: int = Field(default=0, description='Bangumi请求缓存数量')
    anilist: int = Field(default=0, description='AniList请求缓存数量')
    imdb: int = Field(default=0, description='IMDb请求缓存数量')
    fanart: int = Field(default=0, description='Fanart请求缓存数量')
    musicbrainz: int = Field(default=0, description='MusicBrainz请求缓存数量')
    theaudiodb: int = Field(default=0, description='TheAudioDB请求缓存数量')
    listenbrainz: int = Field(default=0, description='ListenBrainz请求缓存数量')
    meta: int = Field(default=0, description='元数据缓存过期时间（秒）')
    scheduler: int = Field(default=0, description='调度器数量')
    threadpool: int = Field(default=0, description='线程池大小')


class ConfigModel(BaseModel):
    """
    Pydantic 配置模型，描述所有配置项及其类型和默认值。

    :cvar model_config: 模型解析策略，忽略未声明的配置项
    """

    model_config = ConfigDict(extra="ignore")

    # 基础应用配置
    PROJECT_NAME: str = Field(default="MoviePilot", description='项目名称')
    APP_DOMAIN: str = Field(default="", description='域名 格式；https://movie-pilot.org')
    API_V1_STR: str = Field(default="/api/v1", description='API路径')
    FRONTEND_PATH: str = Field(default="/public", description='前端资源路径')
    TZ: str = Field(default="Asia/Shanghai", description='时区')
    HOST: str = Field(default="0.0.0.0", description='API监听地址')
    PORT: int = Field(default=3001, description='API监听端口')
    NGINX_PORT: int = Field(default=3000, description='前端监听端口')
    CONFIG_DIR: Optional[str] = Field(default=None, description='配置文件目录')
    MOVIEPILOT_SAFE_MODE: bool = Field(default=False, description='安全模式，仅保留核心 API，跳过插件、调度器、监控、命令和工作流等扩展启动项')
    BTRFS_FSID_DEDUP: bool = Field(default=False, description='是否启用 Btrfs FSID 子卷容量去重（仅 Linux amd64/arm64）')
    DEBUG: bool = Field(default=False, description='是否调试模式')
    DEV: bool = Field(default=False, description='是否开发模式')
    # 安全认证配置
    SECRET_KEY: str = Field(default=secrets.token_urlsafe(32), description='密钥')
    RESOURCE_SECRET_KEY: str = Field(default=secrets.token_urlsafe(32), description='RESOURCE密钥')
    ALLOWED_HOSTS: list[str] = Field(
        default_factory=lambda: ["*"],
        description='允许的跨域来源；为 "*" 时浏览器跨域请求不携带凭据，需要跨域带 Cookie 请列出显式来源',
    )
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60 * 24 * 8, description='TOKEN过期时间')
    RESOURCE_ACCESS_TOKEN_EXPIRE_SECONDS: int = Field(default=60 * 30, description='RESOURCE_TOKEN过期时间')
    SUPERUSER: str = Field(default="", description='超级管理员用户名；V3 首次启动时为空，由初始化页面设置')
    SUPERUSER_PASSWORD: str = Field(default="", description='超级管理员密码不再通过部署配置初始化，始终由数据库存储哈希')
    AUXILIARY_AUTH_ENABLE: bool = Field(default=False, description='辅助认证，允许通过外部服务进行认证、单点登录以及自动创建用户')
    API_TOKEN: Annotated[
        Optional[str],
        SettingPolicy(converter=_normalize_api_token, sensitive=True),
    ] = Field(default=None, description='API密钥，需要更换')
    AUTH_SITE: str = Field(default="", description='用户认证站点')

    # 数据库配置
    API_WORKERS: int = Field(
        default=1,
        ge=1,
        description=(
            'API 服务的 worker 进程数。连接池是进程级的，每个 worker 各持一份，数据库连接额度校验按它换算总用量。注意：当前主程序以单进程方式启动（uvicorn.Config 的 '
            'workers 仅在多进程 supervisor 路径下生效），调大此项前需先解决调度器会在每个 worker 内重复执行的问题'
        ),
    )
    DB_TYPE: str = Field(default="sqlite", description='数据库类型，支持 sqlite 和 postgresql，默认使用 sqlite')
    DB_ECHO: bool = Field(default=False, description='是否在控制台输出 SQL 语句，默认关闭')
    DB_TIMEOUT: int = Field(default=60, description='数据库连接超时时间（秒），默认为 60 秒')
    DB_WAL_ENABLE: bool = Field(default=True, description='是否启用 WAL 模式，仅适用于SQLite，默认开启')
    DB_POOL_TYPE: str = Field(default="QueuePool", description='数据库连接池类型，QueuePool, NullPool')
    DB_POOL_PRE_PING: bool = Field(default=True, description='是否在获取连接时进行预先 ping 操作')
    DB_POOL_RECYCLE: int = Field(default=300, description='数据库连接的回收时间（秒）')
    DB_POOL_TIMEOUT: int = Field(default=30, description='数据库连接池获取连接的超时时间（秒）')
    DB_SQLITE_POOL_SIZE: int = Field(default=10, description='SQLite 连接池大小')
    DB_SQLITE_MAX_OVERFLOW: int = Field(default=50, description='SQLite 连接池溢出数量')
    DB_POSTGRESQL_HOST: str = Field(default="localhost", description='PostgreSQL 主机地址')
    DB_POSTGRESQL_PORT: str = Field(default="5432", description='PostgreSQL 端口；使用 Unix Socket 时可留空')
    DB_POSTGRESQL_DATABASE: str = Field(default="moviepilot", description='PostgreSQL 数据库名')
    DB_POSTGRESQL_USERNAME: str = Field(default="moviepilot", description='PostgreSQL 用户名')
    DB_POSTGRESQL_PASSWORD: str = Field(default="moviepilot", description='PostgreSQL 密码')
    DB_POSTGRESQL_POOL_SIZE: int = Field(default=10, description='PostgreSQL 连接池大小')
    DB_POSTGRESQL_MAX_OVERFLOW: int = Field(default=50, description='PostgreSQL 连接池溢出数量')
    DB_ASYNC_POOL_TYPE: str = Field(
        default="QueuePool",
        description=(
            '异步连接池类型：QueuePool / NullPool。NullPool 下每个异步会话独占一条物理连接、零复用且无上限，突发并发会直接顶穿PostgreSQL 的 '
            'max_connections（表现为 TooManyConnectionsError），在 SQLite 上则表现为 WAL 写争用导致的长时间卡顿。默认 '
            'QueuePool：仅对常驻主事件循环池化，其余事件循环自动回退 NullPool，避免跨循环复用连接。遇到兼容问题可设为 NullPool回到旧行为。'
        ),
    )
    DB_ASYNC_POOL_SIZE: int = Field(default=5, description='异步连接池大小（每个被池化的事件循环）')
    DB_ASYNC_MAX_OVERFLOW: int = Field(default=10, description='异步连接池溢出数量（每个被池化的事件循环）')
    DB_ASYNC_FALLBACK_LIMIT: int = Field(
        default=10,
        description=(
            '未被池化的事件循环（临时循环）共享的全局并发连接配额。池化路径由连接池自身限流，这里只为 NullPool 兜底路径补上背压，防止临时循环上的突发并发再次无界增长。实测常驻的 '
            'feishu/discord 循环不访问数据库，走此路径的只有插件与调度器兜底分支的零星调用，因此取值不必大——它直接计入连接总额度'
        ),
    )
    DB_CONNECT_ARGS: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            '驱动级连接参数，透传给 create_engine/create_async_engine 的 connect_args。例如经 PgBouncer 事务模式接入时 asyncpg 需要 '
            '{"statement_cache_size": 0}'
        ),
    )

    # 数据库备份配置
    DB_BACKUP_ENABLE: bool = Field(default=False, description='是否启用主程序数据库自动备份')
    DB_BACKUP_CRON: str = Field(default="0 3 * * *", description='定时备份的 Cron 表达式，留空时不注册定时任务')
    DB_BACKUP_ON_UPGRADE: bool = Field(default=True, description='检测到现有数据库需要迁移时，在结构变更前创建恢复点')
    DB_BACKUP_PATH: Optional[str] = Field(default=None, description='备份根目录；未配置时使用 CONFIG_PATH/database_backup')
    DB_BACKUP_RETENTION_DAYS: int = Field(default=30, description='本地备份的保留天数，0 表示不按时间清理')
    DB_BACKUP_MAX_COUNT: int = Field(default=30, description='本地备份的最大保留份数，0 表示不按数量清理')
    # 数据清理配置
    DATA_CLEANUP_ENABLE: bool = Field(default=False, description='是否启用数据表定时清理')
    DATA_CLEANUP_MESSAGE_DAYS: int = Field(default=90, description='消息表保留天数，0为不清理')
    DATA_CLEANUP_DOWNLOAD_HISTORY_DAYS: int = Field(default=180, description='下载历史表保留天数，0为不清理')
    DATA_CLEANUP_SITE_USERDATA_DAYS: int = Field(default=180, description='站点用户数据表保留天数，0为不清理')
    DATA_CLEANUP_TRANSFER_HISTORY_DAYS: int = Field(default=365 * 3, description='整理历史表保留天数，0为不清理')
    DATA_CLEANUP_DOWNLOAD_FAILURE_DAYS: int = Field(default=7, description='下载失败冷却记录保留天数，0为不清理')
    DATA_CLEANUP_SUBSCRIBE_HISTORY_DAYS: int = Field(default=365 * 3, description='订阅完成历史保留天数，0为不清理')
    DATA_CLEANUP_AGENT_CHAT_DAYS: int = Field(default=180, description='Agent 会话历史保留天数，0为不清理')
    DATA_CLEANUP_AGENT_TASK_RUN_DAYS: int = Field(default=180, description='Agent 定时任务运行历史保留天数，0为不清理')
    DATA_CLEANUP_OUTBOX_COMPLETED_DAYS: int = Field(default=30, description='Outbox 已完成记录保留天数，0为不清理')
    DATA_CLEANUP_OUTBOX_DEAD_DAYS: int = Field(default=90, description='Outbox 死信记录保留天数，0为不清理')

    # 缓存配置
    CACHE_BACKEND_TYPE: str = Field(default="cachetools", description='缓存类型，支持 cachetools 和 redis，默认使用 cachetools')
    CACHE_BACKEND_URL: Optional[str] = Field(
        default="redis://localhost:6379",
        description='缓存连接字符串，仅外部缓存（如 Redis、Memcached）需要，支持 Redis Unix Socket URL',
    )
    CACHE_REDIS_MAXMEMORY: Optional[str] = Field(
        default=None,
        description='Redis 缓存最大内存限制，未配置时，如开启大内存模式时为 "1024mb"，未开启时为 "256mb"',
    )
    CACHE_REDIS_MAX_CONNECTIONS: int = Field(default=512, description='Redis 单个连接池最大连接数；异步连接池按事件循环分别应用该上限')
    CACHE_REDIS_POOL_TIMEOUT: int = Field(default=10, description='Redis 连接池耗尽时等待可用连接的时间（秒）')
    GLOBAL_IMAGE_CACHE: bool = Field(default=False, description='全局图片缓存，将媒体图片缓存到本地')
    GLOBAL_IMAGE_CACHE_DAYS: int = Field(default=7, description='全局图片缓存保留天数')
    TEMP_FILE_DAYS: int = Field(default=3, description='临时文件保留天数')
    PACKAGE_CACHE_DAYS: int = Field(default=90, description='pip/uv 包下载缓存保留天数')
    PACKAGE_CACHE_ROOT: Optional[str] = Field(default=None, description='pip/uv 包下载缓存根目录，留空时使用配置目录下的 .cache')
    META_CACHE_EXPIRE: int = Field(default=0, description='单条元数据识别缓存有效期（小时），0为自动')

    # 网络代理配置
    PROXY_HOST: Optional[str] = Field(default=None, description='网络代理服务器地址')
    DOH_ENABLE: bool = Field(default=False, description='是否启用DOH解析域名')
    DOH_DOMAINS: str = Field(default="api.themoviedb.org,"
            "api.tmdb.org,"
            "webservice.fanart.tv,"
            "api.github.com,"
            "github.com,"
            "raw.githubusercontent.com,"
            "codeload.github.com,"
            "api.telegram.org", description='使用 DOH 解析的域名列表')
    DOH_RESOLVERS: str = Field(default="1.0.0.1,1.1.1.1,9.9.9.9,149.112.112.112", description='DOH 解析服务器列表')

    # 媒体元数据配置
    MODULE_ENABLE: Dict[str, bool] = Field(default_factory=dict, description='无独立配置项的内置模块开关；未列出的模块默认开启。')
    SEARCH_SOURCE: str = Field(
        default="themoviedb",
        description='媒体搜索来源 themoviedb/douban/bangumi/anilist/imdb/musicbrainz/theaudiodb/doubanmusic，多个用,分隔',
    )
    RECOGNIZE_SOURCE: str = Field(
        default="themoviedb",
        description='媒体识别来源 themoviedb/douban/bangumi/anilist/imdb/musicbrainz/theaudiodb/doubanmusic',
    )
    SCRAP_SOURCE: str = Field(
        default="themoviedb",
        description='刮削来源 themoviedb/douban/bangumi/anilist/imdb/musicbrainz/theaudiodb/doubanmusic',
    )
    SCRAP_SEASON_IMAGE_TO_ROOT: bool = Field(
        default=True,
        description='季图片是否同时写入剧集根目录；关闭后仅写季目录，不清理已有图片',
    )
    SCRAP_IMAGE_ALIAS: bool = Field(
        default=True,
        description='是否为背景图和缩略图写入兼容别名文件；关闭后每张图片只写入来源名称',
    )
    SCRAP_4K_BACKGROUND: bool = Field(
        default=True,
        description='是否获取 Fanart 的 show4kbackground 和 movie4kbackground 图片',
    )
    ANIME_GENREIDS: List[int] = Field(default=[16], description='电视剧动漫的分类genre_ids')

    # TMDB配置
    TMDB_IMAGE_DOMAIN: str = Field(default="image.tmdb.org", description='TMDB图片地址')
    TMDB_API_DOMAIN: str = Field(default="api.themoviedb.org", description='TMDB API地址')
    TMDB_LOCALE: str = Field(default="zh", description='TMDB元数据语言')
    EMPTY_RESULT_CACHE_TTL: int = Field(default=30 * 60, description='TMDB空结果缓存独立过期时间（秒），故障期间产生的空响应快速过期，故障自愈后可自然恢复')
    TMDB_SCRAP_ORIGINAL_IMAGE: bool = Field(default=False, description='刮削使用TMDB原始语种图片')
    TMDB_API_KEY: str = Field(default="db55323b8d3e4154498498a75642b381", description='TMDB 接口访问密钥')

    # Bangumi配置
    BANGUMI_PROXY_ENABLE: bool = Field(default=False, description='是否启用 Bangumi 数据与图片代理')
    BANGUMI_API_DOMAIN: str = Field(default="", description='Bangumi API代理地址，留空使用官方地址')
    BANGUMI_IMAGE_DOMAIN: str = Field(default="", description='Bangumi图片代理地址，留空时由后端代理端点直接拉取原始图片')

    # 音乐配置
    MUSIC_COVER_PROXY: str = Field(
        default="",
        description='音乐封面代理地址（用于解决 coverartarchive.org 无法访问导致的封面不显示问题，留空则使用官方地址）',
    )
    MUSIC_LOCAL_COVER_ENABLE: bool = Field(default=True, description='是否同步整理同目录音乐封面，并在在线封面不可用时用于内嵌回退')
    MUSIC_COVER_FILE_PATTERNS: str = Field(
        default="cover.jpg,cover.jpeg,cover.png,folder.jpg,folder.jpeg,folder.png,front.jpg,front.jpeg,front.png,*.png",
        description='同目录音乐封面匹配规则，按顺序选择，逗号分隔、大小写不敏感；留空禁用本地封面',
    )
    ACOUSTID_API_KEY: str = Field(default="b1auxfOzAg", description='AcoustID 应用 API Key，用于查询本地音频的 Chromaprint 指纹')
    MUSIC_METADATA_TO_SIMPLIFIED: bool = Field(default=True, description='是否将识别到的音乐标题、艺术家、专辑等标准元数据转换为简体中文')
    MUSIC_LYRICS_TO_SIMPLIFIED: bool = Field(
        default=False,
        description='是否将写入 LRC/TXT 的歌词正文转为简体中文；保留时间轴、LRC 标签和来源 Lyricsfile',
    )
    MUSIC_CUE_ENABLE: bool = Field(default=True, description='是否读取关联 CUE；已分轨音频可关闭以忽略错误索引，整轨专辑应保持开启')
    MUSIC_RELEASE_REGION_PRIORITY: str = Field(
        default="CN,TW,HK",
        description='MusicBrainz 发行地区优先级，按 ISO 3166-1 两位代码从高到低排列',
    )
    MUSIC_RELEASE_SCRIPT_PRIORITY: str = Field(
        default="Hans,Hant,Latn",
        description='MusicBrainz 发行文字字形优先级，按 ISO 15924 四位代码从高到低排列',
    )
    THEAUDIODB_API_KEY: str = Field(default="123", description='TheAudioDB API Key，默认使用官方公开的免费 V1 Key，可通过环境变量覆盖')
    LRCLIB_BASE_URL: str = Field(default="https://lrclib.net", description='LRCLIB 服务地址，可指向兼容官方 API 的自建实例')
    AMLL_BASE_URL: str = Field(default="https://api.amll.dev", description='AMLL TTML 歌词 API 地址，可指向兼容原生接口的自建实例')
    LYRICS_BATCH_TIMEOUT: int = Field(default=120, description='单次音乐刮削批次用于在线歌词查询的总预算（秒）')
    LYRICS_PROVIDER_RETRY_MAX_WAIT: int = Field(default=5, description='供应商要求的重试等待超过该值时进入冷却，不阻塞整个批次')

    # TVDB配置
    TVDB_V4_API_KEY: str = Field(default="ed2aa66b-7899-4677-92a7-67bc9ce3d93a", description='TVDB 接口访问密钥')
    TVDB_V4_API_PIN: str = Field(default="", description='TVDB V4 订阅 PIN')

    # Fanart配置
    FANART_ENABLE: bool = Field(default=True, description='Fanart开关')
    FANART_LANG: str = Field(default="zh,en", description='Fanart语言')
    FANART_API_KEY: str = Field(default="d2d31f9ecabea050fc7d68aa3146015f", description='Fanart 接口访问密钥')

    # 云盘配置
    U115_APP_ID: str = Field(default="100197847", description='115 云盘开放平台应用标识')
    U115_AUTH_SERVER: str = Field(default="https://movie-pilot.org", description='115 OAuth2 Server 地址')
    ALIPAN_APP_ID: str = Field(default="ac1bf04dc9fd4d9aaabb65b4a668d403", description='阿里云盘开放平台应用标识')

    # 系统升级配置
    MOVIEPILOT_AUTO_UPDATE: Annotated[
        bool,
        SettingPolicy(
            before_convert=_normalize_legacy_update_mode,
            migrate=_migrate_legacy_update_mode,
            serialize=_serialize_bool,
        ),
    ] = Field(default=False, description='自动检查稳定版本并提示升级，不自动下载或安装。')
    MOVIEPILOT_UPDATE_DEV: Annotated[
        bool, SettingPolicy(serialize=_serialize_bool)
    ] = Field(default=False, description='Docker 启动时跟踪开发分支；本地 CLI 仅在手动 update 时默认使用 DEV 模式。')
    AUTO_UPDATE_RESOURCE: bool = Field(default=True, description='后台检查站点资源包，确认后由启动器在进程拉起前应用')

    # 媒体文件格式配置
    RMT_MEDIAEXT: list[str] = Field(
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
            ], description='支持的视频文件后缀格式')
    RMT_SUBEXT: list[str] = Field(default_factory=lambda: [".srt", ".ass", ".ssa", ".sup"], description='支持的字幕文件后缀格式')
    RMT_AUDIOEXT: list[str] = Field(
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
            ], description='支持的音轨文件后缀格式')

    # 媒体服务器配置
    MEDIASERVER_SYNC_INTERVAL: int = Field(default=6, description='媒体服务器同步间隔（小时）')

    # 订阅配置
    SUBSCRIBE_MODE: str = Field(default="spider", description='订阅模式')
    SUBSCRIBE_RSS_INTERVAL: int = Field(default=30, description='RSS订阅模式刷新时间间隔（分钟）')
    SUBSCRIBE_STATISTIC_SHARE: bool = Field(default=True, description='订阅数据共享')
    SUBSCRIBE_SEARCH: bool = Field(default=False, description='订阅搜索开关')
    SUBSCRIBE_SEARCH_INTERVAL: int = Field(default=24, description='订阅搜索时间间隔（小时）')
    LOCAL_EXISTS_SEARCH: bool = Field(default=True, description='检查本地媒体库是否存在资源开关')

    # 站点配置
    SITEDATA_REFRESH_INTERVAL: int = Field(default=6, description='站点数据刷新间隔（小时）')
    SITE_MESSAGE: bool = Field(default=True, description='读取和发送站点消息')
    NO_CACHE_SITE_KEY: str = Field(default="", description='不缓存站点资源的域名关键字，多个使用,分隔；留空时所有站点使用缓存')
    OCR_HOST: str = Field(default="https://movie-pilot.org", description='OCR服务器地址，用于识别站点验证码')
    BROWSER_EMULATION: str = Field(
        default="cloakbrowser",
        description='仿真类型：cloakbrowser 或 flaresolverr，其他值按 cloakbrowser 处理',
    )
    CLOAKBROWSER_HUMANIZE: bool = Field(default=True, description='CloakBrowser 是否启用拟人化输入')
    CLOAKBROWSER_HUMAN_PRESET: str = Field(default="default", description='CloakBrowser 拟人化输入预设：default 或 careful')
    FLARESOLVERR_URL: Optional[str] = Field(default=None, description='FlareSolverr 服务地址，例如 http://127.0.0.1:8191')

    # 搜索配置
    SEARCH_MULTIPLE_NAME: bool = Field(default=False, description='搜索多个名称')
    MAX_SEARCH_NAME_LIMIT: int = Field(default=3, description='最大搜索名称数量')
    SEARCH_RESOURCE_PAGES: int = Field(default=1, description='搜索资源获取页数')

    # 下载配置
    TORRENT_TAG: str = Field(default="MOVIEPILOT", description='种子标签')
    DOWNLOAD_SUBTITLE: bool = Field(default=True, description='下载站点字幕')
    AUTO_DOWNLOAD_USER: Optional[str] = Field(default=None, description='交互搜索自动下载用户ID，使用,分割')
    DOWNLOAD_TMPEXT: list[str] = Field(default_factory=lambda: [".!qb", ".part"], description='下载器临时文件后缀')

    # 目录监控配置
    MONITOR_NETWORK_FAST_MODE: bool = Field(
        default=False,
        description=(
            '允许网络文件系统使用快速模式（inotify）。部分 FUSE 实现（如 CloudDrive2）会正常下发内核通知，快速模式可用，且比每 N 秒 stat '
            '全部目录的轮询对挂载后端的压力小得多，由用户确认后开启'
        ),
    )
    MONITOR_POLL_DELAY_NETWORK: int = Field(default=0, description='网络文件系统的轮询扫描间隔（毫秒），0 表示使用内置默认值')
    MONITOR_RESCAN_DELAYS: str = Field(
        default="30,120,600,1800",
        description=(
            '新增目录延迟重扫的轮次延迟秒数，多个使用,分隔，如 "30,120,600,1800"。FUSE 挂载（如 CloudDrive2）上超大目录树呈现完整内容可能超过分钟级，默认值在常见的 '
            '30/120 秒窗口后追加两轮成本极低的长延迟轮次兜底；解析失败（如格式非法、包含非正整数）时回退默认值并记录 warn 日志'
        ),
    )

    # CookieCloud配置
    COOKIECLOUD_ENABLE_LOCAL: Optional[bool] = Field(default=False, description='CookieCloud是否启动本地服务')
    COOKIECLOUD_HOST: str = Field(default="https://movie-pilot.org/cookiecloud", description='CookieCloud服务器地址')
    COOKIECLOUD_KEY: Optional[str] = Field(default=None, description='CookieCloud用户KEY')
    COOKIECLOUD_PASSWORD: Optional[str] = Field(default=None, description='CookieCloud端对端加密密码')
    COOKIECLOUD_AUTH_HEADER: Optional[str] = Field(
        default=None,
        description='CookieCloud本地上传接口的X-CookieCloud-Auth期望值，留空表示不校验',
    )
    COOKIECLOUD_INTERVAL: Optional[int] = Field(default=60 * 24, description='CookieCloud同步间隔（分钟）')
    COOKIECLOUD_BLACKLIST: Optional[str] = Field(default=None, description='CookieCloud同步黑名单，多个域名,分割')

    # 整理配置
    TRANSFER_THREADS: int = Field(default=1, description='文件整理线程数')
    TRANSFER_COPY_TRY_REFLINK: bool = Field(
        default=True,
        description='本地文件复制时，在同一磁盘且支持写时复制时优先尝试引用链接 (reflink / 克隆)。若明确知晓底层文件系统不支持 CoW，可关闭此选项跳过探测直接进行全量复制以提升性能。',
    )
    FS_PROXY_ENABLED: bool = Field(
        default=True,
        description=(
            '本地文件操作是否走可强杀的子进程代理。FUSE/网络挂载进入「请求永不返回」状态时，stat/listdir 这类调用会永久悬挂，而 Python 既不能中断已发出的系统调用、'
            '也不能强杀线程，阻塞其上的线程无法回收。走子进程后超时可以 SIGKILL 真正回收，block 型故障被转换成各层已能处理的crash 型故障（OSError）。'
            '出现兼容问题时可关闭回到直接调用。'
        ),
    )
    FS_PROXY_TIMEOUT: int = Field(default=30, description='单次本地快操作（stat/listdir/删除/重命名）的超时秒数，超时即判定挂载无响应并回收代理进程')
    FS_PROXY_STALL_TIMEOUT: int = Field(
        default=120,
        description='复制大文件时两次进度上报之间的最长间隔秒数。代理每秒上报一次进度作为心跳，因此这个阈值判定的是「传输完全没有推进」而不是「传输很慢」，复制几小时的大文件也不会被误杀',
    )
    TRANSFER_MAX_FAILED_RETRIES: int = Field(
        default=3,
        description=(
            '自动整理（目录监控、下载器轮询）遇到失败整理记录时，同一源路径允许自动重试的最大次数。一次瞬时故障（网络抖动、TMDB 瞬断、移动失败）不该让文件永久漏整理，因此必须重试；'
            '但永远识别不出的文件重试再多也不会成功，只会重复推送失败通知，批量导入时更会刷屏，因此必须有界。取值区间 1-10，越界或非法值会被钳制到边界并记录 warn：既不支持关闭重试，'
            '也不支持无限重试。整理成功或删除整理记录时计数清零。与本项无关的是同路径新版本：已成功整理的文件源大小变化时一律放行，由整理链的 overwrite_mode 决断是否覆盖'
        ),
    )
    TRANSFER_TASK_TIMEOUT: int = Field(default=120, description='外部接管的运行中整理任务无状态心跳超时（分钟），0 表示禁用')
    MOVIE_RENAME_FORMAT: str = Field(
        default=(
            '{{title}}{% if year %} ({{year}}){% endif %}/{{title}}{% if year %} ({{year}}){% endif %}{% '
            'if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}'
        ),
        description='电影重命名格式',
    )
    TV_RENAME_FORMAT: str = Field(
        default=(
            '{{title}}{% if year %} ({{year}}){% endif %}/Season {{season}}/{{title}} - '
            '{{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif '
            '%}{{fileExt}}'
        ),
        description='电视剧重命名格式',
    )
    MUSIC_RENAME_FORMAT: str = Field(default="{{album_artist or artist or 'Unknown Artist'}}"
            "/{{album or 'Unknown Album'}}{% if year %} ({{year}}){% endif %}"
            "{% if total_discs and total_discs > 1 %}/Disc {{disc_number or 1}}{% endif %}"
            "/{% if track %}{{track}} - {% endif %}{{title}}{{fileExt}}", description='音乐重命名格式')
    RENAME_FORMAT_S0_NAMES: list[str] = Field(default=["Specials", "SPs"], description='重命名时支持的S0别名')
    DEFAULT_SUB: Optional[str] = Field(default="zh-cn", description='为指定默认字幕添加.default后缀')
    SCRAP_FOLLOW_TMDB: bool = Field(default=True, description='新增已入库媒体是否跟随TMDB信息变化')
    RECOGNIZE_PLUGIN_FIRST: bool = Field(default=False, description='优先使用辅助识别')
    MEDIA_RECOGNIZE_SHARE: bool = Field(default=True, description='共享使用媒体识别数据')

    # 服务地址配置
    MP_SERVER_HOST: str = Field(
        default="https://movie-pilot.org",
        description='服务器地址，对应 https://github.com/jxxghp/MoviePilot-Server 项目',
    )
    MEDIA_RECOGNIZE_SHARE_API: Optional[str] = Field(
        default=None,
        description='共享媒体识别API地址，留空时默认拼接为 MP_SERVER_HOST + /recognize/share',
    )

    # 个性化
    WALLPAPER: str = Field(default="", description='登录页面壁纸来源：空字符串表示无壁纸，另支持 tmdb/bing/mediaserver/customize/static')
    WALLPAPER_ROTATION_INTERVAL: int = Field(default=15, description='壁纸轮换间隔（秒），0 表示不轮换')
    WALLPAPER_IMAGE_URL: Optional[str] = Field(default=None, description='静态壁纸地址，可使用前端可访问的本地路径或 URL')
    CUSTOMIZE_WALLPAPER_API_URL: Optional[str] = Field(default=None, description='自定义壁纸api地址')

    # 插件配置
    PLUGIN_MARKET: str = (
        "https://github.com/jxxghp/MoviePilot-Plugins,"
        "https://github.com/thsrite/MoviePilot-Plugins,"
        "https://github.com/honue/MoviePilot-Plugins,"
        "https://github.com/InfinityPacer/MoviePilot-Plugins,"
        "https://github.com/dandkong/MoviePilot-Plugins,"
        "https://github.com/Aqr-K/MoviePilot-Plugins,"
        "https://github.com/AnjoyLi/MoviePilot-Plugins,"
        "https://github.com/WithdewHua/MoviePilot-Plugins,"
        "https://github.com/HankunYu/MoviePilot-Plugins,"
        "https://github.com/baozaodetudou/MoviePilot-Plugins,"
        "https://github.com/almus2zhang/MoviePilot-Plugins,"
        "https://github.com/Pixel-LH/MoviePilot-Plugins,"
        "https://github.com/lightolly/MoviePilot-Plugins,"
        "https://github.com/suraxiuxiu/MoviePilot-Plugins,"
        "https://github.com/gxterry/MoviePilot-Plugins,"
        "https://github.com/hotlcc/MoviePilot-Plugins-Third,"
        "https://github.com/boeto/MoviePilot-Plugins,"
        "https://github.com/xiangt920/MoviePilot-Plugins,"
        "https://github.com/yubanmeiqin9048/MoviePilot-Plugins,"
        "https://github.com/loongcheung/MoviePilot-Plugins,"
        "https://github.com/xcehnz/MoviePilot-Plugins,"
        "https://github.com/imaliang/MoviePilot-Plugins,"
        "https://github.com/wikrin/MoviePilot-Plugins,"
        "https://github.com/DDSRem-Dev/MoviePilot-Plugins,"
        "https://github.com/KoWming/MoviePilot-Plugins,"
        "https://github.com/madrays/MoviePilot-Plugins,"
        "https://github.com/aClarkChen/MoviePilot-Plugins,"
        "https://github.com/justzerock/MoviePilot-Plugins,"
        "https://github.com/Seed680/MoviePilot-Plugins,"
        "https://github.com/DzAvril/MoviePilot-Plugins,"
        "https://github.com/xijin285/MoviePilot-Plugins,"
        "https://github.com/cqubioyj/MoviePilot-Plugins,"
        "https://github.com/Hqyel/MoviePilot-Plugins-Third,"
        "https://github.com/xiaoQQya/MoviePilot-Plugins,"
        "https://github.com/mrtian2016/MoviePilot-Plugins,"
        "https://github.com/andyxu8023/MoviePilot-Plugins,"
        "https://github.com/Sinterdial/MoviePilot-Plugins,"
        "https://github.com/4Nest/MoviePilot-Plugins,"
        "https://github.com/baranwang/MoviePilot-Plugins,"
        "https://github.com/luanyi143/MoviePilot-Plugins,"
        "https://github.com/jianji112/MoviePilot-Plugins,"
        "https://github.com/AWdress/MoviePilot-Plugins,"
        "https://github.com/cyt-666/MoviePilot-Plugins,"
        "https://github.com/jiangbkvir/MoviePilot-Plugins,"
        "https://github.com/wio-ki/MoviePilot-Plugins,"
        "https://github.com/bfjy2024/MoviePilot-Plugins,"
        "https://github.com/ui-beam-9/MoviePilot-Plugins,"
        "https://github.com/Mister-album/MoviePilot-Plugins,"
        "https://github.com/ifsherlock/MoviePilot-Plugins,"
        "https://github.com/clone-fan/MoviePilot-Plugins,"
        "https://github.com/jun9100/moviepilot-multiratingsrecommend,"
        "https://github.com/AshBearlt/MoviePilot-Plugins,"
        "https://github.com/z2561221/MoviePilot-Plugins,"
        "https://github.com/jinyuhao-886/MoviePilot-Plugins,"
        "https://github.com/Fuck996/MoviePilot-Plugins,"
        "https://github.com/shyblacktea/MoviePilot-Plugins,"
        "https://github.com/milikii/MoviePilot-ProwlarrExtend,"
        "https://github.com/cudamin/MoviePilot-Plugins,"
        "https://github.com/singleton-altman/MoviePilot-Plugins,"
        "https://github.com/morningstar-ski/moviepilot-zhuiju-helper,"
        "https://github.com/jtcymc/MoviePilot-PluginsV2,"
        "https://github.com/EWEDLCM/MoviePilot-Plugins,"
        "https://github.com/mitlearn/MoviePilot-PluginsV2,"
        "https://github.com/BrettDean/MoviePilot-Plugins,"
        "https://github.com/odomu/MoviePilot-Plugins,"
        "https://github.com/zyt0339/MoviePilot-Plugins,"
        "https://github.com/TorrenKt/MoviePilot-Plugins-TSLM,"
        "https://github.com/LXT-A-X/MoviePilot-Plugins,"
        "https://github.com/zhwejjjj/MoviePilot-Plugins,"
        "https://github.com/xlmc/MoviePilot-Plugins,"
        "https://github.com/LunaticXJ/MoviePilot-Plugins,"
        "https://github.com/JinxJie/MoviePilot-Plugins,"
        "https://github.com/SAGIRIxr/MoviePilot-Plugins,"
        "https://github.com/irab-liu/MoviePilot-Plugins,"
        "https://github.com/changzhanghs/MoviePilot-Plugins,"
        "https://github.com/q10710/MoviePilot-Plugins,"
        "https://github.com/mtno1/MoviePilot-Plugins,"
        "https://github.com/GuanAllen/MoviePilot-Plugins,"
        "https://github.com/zkmydgth/MoviePilot-Plugins,"
        "https://github.com/sanqianxingluo/MoviePilot-Plugins,"
        "https://github.com/SuCorine/MoviePilot-Plugins,"
        "https://github.com/liclown2020/MoviePilot-Plugins,"
        "https://github.com/anorl/MoviePilot-Plugins,"
        "https://github.com/strongrexue/MoviePilot-Plugins,"
        "https://github.com/ranzhigg/MoviePilot-Plugins,"
        "https://github.com/ACE-830/MoviePilot-Plugins,"
        "https://github.com/qingyukb/MoviePilot-Plugins,"
        "https://github.com/FlameSky-S/MoviePilot-Plugins,"
        "https://github.com/KingNuoNuo/MoviePilot-Plugins,"
        "https://github.com/pipi20xx/MoviePilot-Plugins,"
        "https://github.com/dramafan2026/MoviePilot-Plugins,"
        "https://github.com/yyhoworks/MoviePilot-Plugins,"
        "https://github.com/demigody/MoviePilot-Plugins,"
        "https://github.com/hbq0405/MoviePilot-Plugins,"
        "https://github.com/chens-hi/MoviePilot-Plugins,"
        "https://github.com/po9873/MoviePilot-Plugins"
    )
    PLUGIN_STATISTIC_SHARE: bool = Field(default=True, description='插件安装数据共享')
    USAGE_STATISTIC_SHARE: bool = Field(default=True, description='安装版本统计上报')
    PLUGIN_AUTO_RELOAD: bool = Field(default=False, description='是否开启插件热加载')
    DEPRECATION_ENABLED: Optional[str] = Field(default=None, description='临时放行的废弃标识，多个用,分隔；仅对已进入停用阶段的接口有效，用于观察真实依赖方')
    PLUGIN_LOCAL_REPO_PATHS: Optional[str] = Field(default=None, description='本地插件仓库目录，多个地址使用,分隔')

    # 技能配置
    SKILL_MARKET: str = Field(default="https://clawhub.ai,"
            "https://github.com/openai/skills,"
            "https://github.com/anthropics/skills,"
            "https://github.com/vercel-labs/agent-skills", description='技能市场仓库地址，多个地址使用,分隔')

    # GitHub 与 Python 包索引配置
    GITHUB_TOKEN: Annotated[
        Optional[str],
        SettingPolicy(sensitive=True),
    ] = Field(default=None, description='GitHub Token 供 Agent 提交 Issue/PR 和访问 API；原文只保留在服务端运行配置中')
    GITHUB_PROXY: Optional[str] = Field(default="", description='Github代理服务器，格式：https://mirror.ghproxy.com/')
    PIP_PROXY: Optional[str] = Field(
        default="",
        description='pip镜像站点，格式：https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple',
    )
    REPO_GITHUB_TOKEN: Optional[str] = Field(
        default=None,
        description='指定的仓库Github token，多个仓库使用,分隔，格式：{user1}/{repo1}:ghp_****,{user2}/{repo2}:github_pat_****',
    )
    COMMUNITY_MEDALS_ENABLE: bool = Field(
        default=True,
        description='社区勋章：关闭后不查询服务端开关、不拉取勋章名单；开启时仍需 MoviePilot 服务端开放该功能',
    )

    # 飞书通知配置
    FEISHU_APP_ID: Optional[str] = Field(default=None, description='飞书应用 App ID')
    FEISHU_APP_SECRET: Optional[str] = Field(default=None, description='飞书应用 App Secret')
    FEISHU_OPEN_ID: Optional[str] = Field(default=None, description='飞书默认接收用户 Open ID')
    FEISHU_CHAT_ID: Optional[str] = Field(default=None, description='飞书默认接收群聊 Chat ID')
    FEISHU_ADMINS: Optional[str] = Field(default=None, description='飞书管理员 Open ID 列表，多个使用 , 分隔')
    FEISHU_VERIFICATION_TOKEN: Optional[str] = Field(default=None, description='飞书事件校验 Token')
    FEISHU_ENCRYPT_KEY: Optional[str] = Field(default=None, description='飞书事件加密 Key')

    # 性能配置
    BIG_MEMORY_MODE: bool = Field(default=False, description='大内存模式')
    RUST_ACCEL: Annotated[bool, SettingPolicy(validate=_validate_rust_accel)] = Field(
        default=True,
        description='Rust 加速总开关，free-threaded 运行时固定启用',
    )
    ENCODING_DETECTION_PERFORMANCE_MODE: bool = Field(default=True, description='是否启用编码探测的性能模式')
    ENCODING_DETECTION_MIN_CONFIDENCE: float = Field(default=0.8, description='编码探测的最低置信度阈值')
    MEMORY_GC_INTERVAL: int = Field(default=30, description='主动内存回收时间间隔（分钟），0为不启用')

    # 安全配置
    SECURITY_IMAGE_DOMAINS: list[str] = Field(
            default=[
                "image.tmdb.org",
                "images.tmdb.org",
                "static-mdb.v.geilijiasu.com",
                "bing.com",
                "doubanio.com",
                "lain.bgm.tv",
                "bgm.tv",
                "bangumi.tv",
                "bangumi.lol",
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
            ], description='允许的图片缓存域名')
    IMAGE_PROXY_ALLOWED_PRIVATE_RANGES: list[str] = Field(default=[], description='图片代理允许访问的非公网 IP/CIDR，默认不放行任何非公网解析结果')
    SECURITY_IMAGE_SUFFIXES: list[str] = Field(
            default=[".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif"], description='允许的图片文件后缀格式')
    PASSKEY_REQUIRE_UV: bool = Field(default=True, description='PassKey 是否强制用户验证（生物识别等）')
    API_DOCS_ENABLE: bool = Field(
        default=False,
        description='是否开放 API 文档；开放 /docs、/redoc 与 /api/v1/openapi.json，文档首次生成后常驻约 20–30MB 内存，修改后立即生效',
    )

    # 工作流配置
    WORKFLOW_STATISTIC_SHARE: bool = Field(default=True, description='工作流数据共享')

    # 存储配置
    RCLONE_SNAPSHOT_CHECK_FOLDER_MODTIME: bool = Field(default=True, description='对rclone进行快照对比时，是否检查文件夹的修改时间')
    OPENLIST_SNAPSHOT_CHECK_FOLDER_MODTIME: bool = Field(default=True, description='对OpenList进行快照对比时，是否检查文件夹的修改时间')
    ALIPAN_SNAPSHOT_CHECK_FOLDER_MODTIME: bool = Field(
        default=False,
        description='对阿里云盘进行快照对比时，是否检查文件夹的修改时间（默认关闭，因为阿里云盘目录时间不随子文件变更而更新）',
    )

    PLAYWRIGHT_BROWSER_TYPE: str = Field(
        default="chromium",
        description='Playwright浏览器类型，供智能体浏览器工具和插件直接使用 Playwright 时读取',
    )

    # AI智能体配置
    AI_AGENT_ENABLE: bool = Field(default=False, description='AI智能体开关')
    AGENT_INTERACTIVE_PRIORITY: bool = Field(default=True, description='为交互式智能助手隔离执行容量，并让新启动的非关键后台服务有限避让')
    AGENT_BACKGROUND_DEFER_SECONDS: int = Field(default=30, ge=0, description='交互活跃时非关键后台服务最多避让的秒数，0为不避让；不暂停已开始的任务')
    AGENT_LLM_TIMEOUT: int = Field(default=180, ge=0, description='Agent单次模型请求的总超时秒数，包含供应商重试；0为不限制')
    SCHEDULER_JOB_TIMEOUT: int = Field(default=3600, ge=0, description='协程调度任务默认执行超时秒数，0为不限制；不强行终止同步线程')
    AI_AGENT_GLOBAL: bool = Field(default=False, description='合局AI智能体')
    AI_AGENT_HIDE_ENTRY: bool = Field(default=False, description='是否隐藏前端全局智能体入口')
    AI_AGENT_PET: str = Field(default="", description='默认助手形象，格式为 插件ID:形象key，留空使用内置机器人；用户个人选择优先')
    AI_AGENT_OUTPUT_LANGUAGE: str = Field(default="zh-CN", description='内置智能助手输出语言，默认使用简体中文')
    LLM_PROVIDER: str = Field(default="deepseek", description='LLM提供商（支持内置 provider，以及从 models.dev 动态补充的平台）')
    LLM_MODEL: str = Field(default="deepseek-chat", description='LLM模型名称')
    LLM_THINKING_LEVEL: Optional[str] = Field(
        default="off",
        description='思考模式/深度配置：off/auto/minimal/low/medium/high/max/xhigh；OpenAI兼容接口保留max/xhigh，仅按已知模型能力降级',
    )
    LLM_API_PROTOCOL: str = Field(default="auto", description='OpenAI兼容接口API协议：auto（自动）/ chat_completions / responses')
    LLM_WEB_SEARCH_MODE: str = Field(
        default="local",
        description='联网搜索模式：local（本地）/ builtin（模型服务端）/ auto（自动）/ disabled（关闭）',
    )
    LLM_SUPPORT_IMAGE_INPUT: bool = Field(default=True, description='LLM是否支持图片输入，开启后消息图片会按多模态输入发送给模型')
    LLM_SUPPORT_AUDIO_INPUT: bool = Field(default=False, description='是否启用音频输入，开启后用户语音会先转写为文本再进入 Agent')
    LLM_SUPPORT_AUDIO_OUTPUT: bool = Field(default=False, description='是否启用音频输出，开启后 Agent 可在支持渠道发送语音回复')
    LLM_API_KEY: Optional[str] = Field(default=None, description='LLM API密钥')
    LLM_BASE_URL: Optional[str] = Field(default="https://api.deepseek.com", description='LLM基础URL（用于自定义API端点）')
    LLM_USE_PROXY: bool = Field(default=True, description='LLM调用是否使用系统代理')
    LLM_BASE_URL_PRESET: Optional[str] = Field(default=None, description='LLM Base URL 预设标识，用于区分同一 Base URL 下的不同模型目录')
    LLM_MAX_CONTEXT_TOKENS: int = Field(default=256, description='LLM最大上下文Token数量（K），用于目录缺失回退和未匹配兼容端点的用户上限')
    LLM_USER_AGENT: Optional[str] = Field(default=None, description='LLM OpenAI兼容接口请求User-Agent')
    LLM_TEMPERATURE: Optional[float] = Field(default=None, description='LLM温度参数，留空时不覆盖提供商默认值')
    LLM_MAX_ITERATIONS: int = Field(default=512, description='LLM最大迭代次数')
    LLM_TOOL_TIMEOUT: int = Field(default=300, description='LLM工具调用超时时间（秒）')
    LLM_VERBOSE: bool = Field(default=False, description='是否启用详细日志')
    LLM_MEMORY_RETENTION_DAYS: int = Field(default=1, description='内存记忆保留天数')
    AI_RECOMMEND_ENABLED: bool = Field(default=False, description='是否启用AI推荐')
    AI_RECOMMEND_USER_PREFERENCE: str = Field(default="", description='AI推荐用户偏好')

    AI_RECOMMEND_MAX_ITEMS: int = Field(default=50, description='AI推荐条目数量限制')
    LLM_MAX_TOOLS: int = Field(default=0, description='LLM工具选择中间件最大工具数量，0为不启用工具选择中间件')
    AI_AGENT_JOB_INTERVAL: int = Field(default=0, description='AI智能体定时任务检查间隔（小时），0为不启用，默认24小时')
    AI_AGENT_VERBOSE: bool = Field(default=False, description='AI智能体啰嗦模式，开启后会回复工具调用过程')
    AI_AGENT_RETRY_TRANSFER: bool = Field(default=False, description='AI智能体自动重试整理失败记录开关')
    TRANSFER_FAILURE_NOTIFICATION_AGGREGATION: bool = Field(default=True, description='是否按媒体聚合整理失败通知，关闭时保持逐条发送')
    TRANSFER_SUCCESS_NOTIFICATION_AGGREGATION: bool = Field(default=True, description='是否按批次聚合电影、电视剧和音乐的整理成功通知')

    AUDIO_INPUT_PROVIDER: str = Field(default="openai", description='音频输入提供商：openai/openai_chat_audio/mimo/minimax')
    AUDIO_INPUT_API_KEY: Optional[str] = Field(default=None, description='音频输入 API 密钥')
    AUDIO_INPUT_BASE_URL: Optional[str] = Field(default=None, description='音频输入基础URL')
    AUDIO_INPUT_MODEL: str = Field(default="gpt-4o-mini-transcribe", description='音频输入模型')
    AUDIO_INPUT_LANGUAGE: str = Field(default="zh", description='音频输入识别语言')
    AUDIO_OUTPUT_PROVIDER: str = Field(default="openai", description='音频输出提供商：openai/openai_chat_audio/mimo/minimax')
    AUDIO_OUTPUT_API_KEY: Optional[str] = Field(default=None, description='音频输出 API 密钥')
    AUDIO_OUTPUT_BASE_URL: Optional[str] = Field(default=None, description='音频输出基础URL')
    AUDIO_OUTPUT_MODEL: str = Field(default="gpt-4o-mini-tts", description='音频输出模型')
    AUDIO_OUTPUT_VOICE: str = Field(default="alloy", description='音频输出音色/发音人')
    AUDIO_OUTPUT_INCLUDE_TEXT: bool = Field(default=False, description='回复语音时是否同时附带文字说明')


# 每次构造独立记录外部文件字段，避免并发构造配置时互相污染写回保护。
_secret_fields_context: ContextVar[Optional[set[str]]] = ContextVar(
    "settings_secret_fields", default=None
)


def _get_secrets_dir() -> Optional[str]:
    """
    从进程环境选择 secrets 目录；显式空值或 false 关闭文件来源。

    :return: secrets 目录路径；关闭时返回 None
    """
    value = os.environ.get(
        "MOVIEPILOT_SECRETS_DIR", os.environ.get("SECRETS_DIR", "/run/secrets")
    ).strip()
    return None if value.lower() in {"", "false", "off", "0"} else value


class _DeploymentSecretsSource(SecretsSettingsSource):
    """
    按配置名读取外部文件，隔离坏文件并保护编排管理的字段不被写回。

    复用 Pydantic 的来源顺序和复杂值解析；只记录字段名，不缓存或记录原文。
    """

    def __call__(self) -> dict[str, Any]:
        """
        逐项读取并校验 secret，缺失目录静默跳过，其余错误只告警字段名。

        :return: 通过字段类型与约束校验的配置值
        """
        values: dict[str, Any] = {}
        if self.secrets_dir is None:
            return values
        directories = (
            [self.secrets_dir]
            if isinstance(self.secrets_dir, (str, os.PathLike))
            else self.secrets_dir
        )
        for directory in directories:
            path = Path(directory).expanduser()
            try:
                if not path.exists():
                    continue
                if not path.is_dir():
                    raise NotADirectoryError
                self._read_directory(path, values)
            except OSError:
                logger.warning("secrets 目录不可用，请检查目录类型与读取权限")
        return values

    def _read_directory(self, directory: Path, values: dict[str, Any]) -> None:
        """
        一次索引目录后按精确字段名定位文件，支持 K8s 投影链接。

        :param directory: 已确认存在的 secrets 目录
        :param values: 按目录顺序合并的配置值，后面的同名文件优先
        """
        protected = _secret_fields_context.get()
        entries = {entry.name: entry for entry in directory.iterdir()}
        for name, field in self.settings_cls.model_fields.items():
            path = entries.get(name)
            if path is None:
                continue
            try:
                if protected is not None:
                    protected.add(name)
                # 即使该文件无效，外部字段也不得被默认值或自动修正写回遮蔽。
                values.pop(name, None)
                if name in self.current_state:
                    continue
                raw = path.read_text(encoding="utf-8-sig").strip()
                if not raw or (name == "API_TOKEN" and len(raw) < 16):
                    raise ValueError
                value = self.prepare_field_value(name, field, raw, False)
                values[name] = TypeAdapter(field.rebuild_annotation()).validate_python(value)
            except (OSError, ValueError):
                if protected is not None:
                    protected.add(name)
                logger.warning(f"配置项 '{name}' 的 secret 无法读取或格式无效，已跳过，请检查文件与字段格式")


class Settings(BaseSettings, ConfigModel, LogConfigModel):
    """
    加载部署环境配置并提供运行目录、外部地址和配置更新入口。

    字段语义继承配置模型；构造实例会创建配置目录，二进制部署还会补齐内置环境文件。

    :cvar model_config: 来源优先级为构造参数、环境变量、环境文件、secrets、默认值
    :ivar _secret_fields: 存在外部 secret 的字段名，包含被高优先级来源遮蔽或无效的文件
    """

    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=get_env_path(),
        env_file_encoding="utf-8",
        secrets_dir=_get_secrets_dir(),
    )
    _secret_fields: set[str] = PrivateAttr(default_factory=set)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        保持既有来源优先级，仅替换 secrets 读取以隔离文件错误。

        :param settings_cls: 本次构造的设置类型
        :param init_settings: 显式构造参数来源
        :param env_settings: 进程环境来源
        :param dotenv_settings: 部署环境文件来源
        :param file_secret_settings: 框架按 model_config 或 _secrets_dir 创建的来源
        :return: 从高到低排列的来源
        """
        secret_source = _DeploymentSecretsSource(
            settings_cls, secrets_dir=cast(SecretsSettingsSource, file_secret_settings).secrets_dir
        )
        return init_settings, env_settings, dotenv_settings, secret_source

    def __init__(self, **kwargs: Any) -> None:
        """
        加载环境配置并确保运行时目录和内置配置可用。
        """
        protected: set[str] = set()
        token = _secret_fields_context.set(protected)
        try:
            super().__init__(**kwargs)
            self._secret_fields = protected
        finally:
            _secret_fields_context.reset(token)
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
        校验 API_TOKEN。
        """
        return _normalize_api_token(value, original_value)

    @staticmethod
    def generic_type_converter(
        value: Any,
        original_value: Any,
        expected_type: object,
        default: Any,
        field_name: str,
        raise_exception: bool = False,
    ) -> Tuple[Any, bool]:
        """
        先执行字段声明的转换策略，再根据预期类型转换值。如果转换失败，返回默认值。

        :param expected_type: Pydantic 字段注解；字段未提供注解时为 None
        :return: 元组 (转换后的值, 是否需要更新)
        """
        if isinstance(value, (list, dict, set)):
            value = copy.deepcopy(value)

        field = Settings.model_fields.get(field_name)
        policy = _get_setting_policy(field)
        if policy:
            if policy.before_convert:
                value = policy.before_convert(value)
            if policy.converter:
                return policy.converter(value, original_value)

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
                        converted: object = bool_map[value_clean]
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
            elif expected_type in (float, Optional[float]):
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

    # 校验器由 Pydantic 在模型构造前调用，保留框架要求的类方法签名。
    @model_validator(mode="before")
    @classmethod
    def generic_type_validator(cls, data: Any) -> object:  # noqa
        """
        通用校验器，迁移旧 Dev 跟踪偏好后将配置值转换为期望的类型。
        """
        if not isinstance(data, dict):
            return data

        # 字段策略负责兼容迁移，公共校验器只负责执行并持久化迁移结果。
        for field_info in cls.model_fields.values():
            policy = _get_setting_policy(field_info)
            if not policy or not policy.migrate:
                continue
            updates = policy.migrate(data)
            for field_name, (original_value, converted_value) in updates.items():
                cls.update_env_config(field_name, original_value, converted_value)

        for field_name, field in cls.model_fields.items():
            if field_name not in data:
                continue
            value = data[field_name]
            if value is None:
                continue

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
        按字段策略序列化并更新 env 配置。
        """
        if field_name in (_secret_fields_context.get() or set()):
            message = f"配置项 '{field_name}' 由 secret 文件管理，请修改文件并重启"
            logger.warning(message)
            return False, message
        policy = _get_setting_policy(Settings.model_fields.get(field_name))
        # 成功且无提示时使用空字符串，保证与 Tuple[bool, str] 返回类型一致
        message = ""
        is_converted = original_value is not None and str(original_value) != str(
            converted_value
        )
        if is_converted:
            if policy and policy.sensitive:
                message = f"配置项 '{field_name}' 的值无效，已替换为安全值"
            else:
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
            if policy and policy.serialize:
                value_to_write = policy.serialize(converted_value)
            elif isinstance(converted_value, (list, dict, set)):
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
        更新单个配置项。

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
            if key in self._secret_fields:
                if value == original_value:
                    return None, ""
                return False, f"配置项 '{key}' 由 secret 文件管理，请修改文件并重启"
            converted_value, needs_update = self.generic_type_converter(
                value, original_value, field.annotation, field.default, key
            )
            policy = _get_setting_policy(field)
            if policy and policy.validate:
                validation_message = policy.validate(converted_value)
                if validation_message:
                    return False, validation_message
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
        更新多个配置项。
        """
        results = {}
        for k, v in env.items():
            results[k] = self.update_setting(k, v)
        return results

    @property
    def VERSION_FLAG(self) -> str:
        """
        版本标识，用来区分重大版本，为空则为v1，不允许外部修改。
        """
        return "v3"

    @property
    def RESOURCE_VERSION_FLAG(self) -> str:
        """
        返回站点索引和认证资源使用的重大版本标识。
        """
        return "v3"

    @property
    def USER_AGENT(self) -> str:
        """
        全局用户代理字符串。
        """
        return (
            f"{self.PROJECT_NAME}/{get_app_version()[1:]} "
            f"({platform.system()} {platform.release()}; {cpu_arch()})"
        )

    @property
    def NORMAL_USER_AGENT(self) -> str:
        """
        默认浏览器用户代理字符串。
        """
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

    @property
    def INNER_CONFIG_PATH(self) -> Path:
        """
        返回随程序发布的内置配置目录。
        """
        return self.ROOT_PATH / "config"

    @property
    def CONFIG_PATH(self) -> Path:
        """
        按显式配置、容器和冻结运行环境确定配置目录。
        """
        if self.CONFIG_DIR:
            return Path(self.CONFIG_DIR)
        elif is_docker():
            return Path("/config")
        elif is_frozen():
            return Path(sys.executable).parent / "config"
        return self.ROOT_PATH / "config"

    @property
    def TEMP_PATH(self) -> Path:
        """
        返回运行时临时目录。
        """
        return self.CONFIG_PATH / "temp"

    @property
    def CACHE_PATH(self) -> Path:
        """
        返回业务缓存目录。
        """
        return self.CONFIG_PATH / "cache"

    @property
    def PACKAGE_CACHE_PATH(self) -> Path:
        """
        返回 pip 和 uv 等包工具使用的缓存根目录。
        """
        if self.PACKAGE_CACHE_ROOT and self.PACKAGE_CACHE_ROOT.strip():
            return Path(self.PACKAGE_CACHE_ROOT).expanduser()
        return self.CONFIG_PATH / ".cache"

    @property
    def ROOT_PATH(self) -> Path:
        """
        返回项目源码或冻结程序的根目录。
        """
        return Path(__file__).parents[2]

    @property
    def PLUGIN_DATA_PATH(self) -> Path:
        """
        返回插件持久化数据目录。
        """
        return self.CONFIG_PATH / "plugins"

    @property
    def DATABASE_BACKUP_PATH(self) -> Path:
        """
        返回数据库备份根目录，允许相对当前配置目录进行配置。
        """
        configured = str(self.DB_BACKUP_PATH or "").strip()
        if not configured:
            return self.CONFIG_PATH / "database_backup"
        path = Path(configured).expanduser()
        return path if path.is_absolute() else self.CONFIG_PATH / path

    @property
    def LOG_PATH(self) -> Path:
        """
        返回应用日志目录。
        """
        return self.CONFIG_PATH / "logs"

    @property
    def COOKIE_PATH(self) -> Path:
        """
        返回站点 Cookie 文件目录。
        """
        return self.CONFIG_PATH / "cookies"

    @property
    def CONF(self) -> SystemConfModel:
        """
        根据内存模式返回系统配置。
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
        fallback_proxy_host = https_proxy or http_proxy
        if fallback_proxy_host:
            return {
                "http": http_proxy or fallback_proxy_host,
                "https": https_proxy or fallback_proxy_host,
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
        """
        判断 PostgreSQL 主机配置是否为 Unix Socket 路径。
        """
        host = (self.DB_POSTGRESQL_HOST or "").strip()
        return host.startswith("/")

    @property
    def DB_POSTGRESQL_TARGET(self) -> str:
        """
        返回适合日志展示的 PostgreSQL 连接目标。
        """
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
        """
        按同步或异步驱动构造 PostgreSQL SQLAlchemy URL。
        """
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
    def PROXY_SERVER(self) -> Optional[dict[str, str]]:
        """
        返回浏览器自动化使用的代理服务器配置。
        """
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
    def GITHUB_HEADERS(self) -> dict[str, str]:
        """
        Github请求头。
        """
        if self.GITHUB_TOKEN:
            return {
                "Authorization": f"Bearer {self.GITHUB_TOKEN}",
                "User-Agent": self.NORMAL_USER_AGENT,
            }
        return {}

    def REPO_GITHUB_HEADERS(self, repo: Optional[str] = None) -> dict[str, str]:
        """
        Github指定的仓库请求头。

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
                    print("无效的仓库 GitHub Token 配置格式")
                    continue
                repo_info = parts[0].strip()
                token = parts[1].strip()
                if not repo_info or not token:
                    print("仓库 GitHub Token 配置缺少仓库名或 Token")
                    continue
                headers[repo_info] = {
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self.NORMAL_USER_AGENT,
                }
            except Exception:
                print("解析仓库 GitHub Token 配置失败")
        # 如果传入了指定的仓库名称，则返回该仓库的请求头信息，否则返回默认请求头
        return headers.get(repo, self.GITHUB_HEADERS)

    @property
    def VAPID(self) -> dict[str, str]:
        """
        返回 Web Push 使用的 VAPID 配置。
        """
        return {
            "subject": f"mailto:{self.SUPERUSER or 'moviepilot'}@movie-pilot.org",
            "publicKey": "BH3w49sZA6jXUnE-yt4jO6VKh73lsdsvwoJ6Hx7fmPIDKoqGiUl2GEoZzy-iJfn4SfQQcx7yQdHf9RknwrL_lSM",
            "privateKey": "JTixnYY0vEw97t9uukfO3UWKfHKJdT5kCQDiv3gu894",
        }

    def MP_DOMAIN(self, url: Optional[str] = None) -> Optional[str]:
        """
        将相对路径组合为当前 MoviePilot 对外访问地址。
        """
        if not self.APP_DOMAIN:
            return None
        return UrlUtils.combine_url(host=self.APP_DOMAIN, path=url)

    def RENAME_FORMAT(self, media_type: MediaType) -> str:
        """
        获取指定类型的重命名格式。

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
        获取TMDB图片网址。

        :param file_path: TMDB API返回的xxx_path
        :param file_size: 图片大小，例如：'original', 'w500' 等
        :return: 图片的完整URL，如果 file_path 为空则返回 None
        """
        if not file_path:
            return None
        return f"https://{self.TMDB_IMAGE_DOMAIN}/t/p/{file_size}/{file_path.removeprefix('/')}"


settings = Settings()
configure_log_settings(settings)


class GlobalVar(object):
    """
    保留旧调用方使用的全局运行标识，停止状态统一转发给进程状态服务。
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
        """
        绑定兼容门面背后的显式 owner；普通实例保持循环状态隔离。
        """
        self._loop_registry = loop_registry or MainLoopRegistry()
        self._push_registry = push_registry or webpush_registry

    @property
    def CURRENT_EVENT_LOOP(self) -> Optional[AbstractEventLoop]:
        """
        兼容旧代码读取未经可用性校验的主循环。
        """
        return self._loop_registry.current

    @CURRENT_EVENT_LOOP.setter
    def CURRENT_EVENT_LOOP(self, loop: Optional[AbstractEventLoop]) -> None:
        """
        兼容旧测试和插件直接替换主循环投递目标。
        """
        self._loop_registry.replace_compat(loop)

    @CURRENT_EVENT_LOOP.deleter
    def CURRENT_EVENT_LOOP(self) -> None:
        """
        兼容属性 patch 清理，删除时仅清空当前投递目标。
        """
        self._loop_registry.replace_compat(None)

    @property
    def SUBSCRIPTIONS(self) -> List[dict[str, Any]]:
        """
        兼容旧代码在持锁后直接访问订阅列表。
        """
        return self._push_registry.compat_items

    @property
    def SUBSCRIPTIONS_LOCK(self) -> threading.Lock:
        """
        兼容旧代码保护原始订阅列表的互斥锁。
        """
        return self._push_registry.compat_lock

    @property
    def STOP_EVENT(self) -> threading.Event:
        """
        兼容旧代码读取进程停止事件。
        """
        return runtime_stop_state.system_event

    @STOP_EVENT.setter
    def STOP_EVENT(self, event: threading.Event) -> None:
        """
        兼容旧测试替换事件，同时保持新 StopState 为唯一状态源。
        """
        runtime_stop_state.replace_system_event(event)

    def stop_system(self) -> None:
        """
        停止系统。
        """
        runtime_stop_state.stop_system()

    @property
    def is_system_stopped(self) -> bool:
        """
        是否停止。
        """
        return runtime_stop_state.is_system_stopped

    def get_subscriptions(self) -> list[dict[str, Any]]:
        """
        获取webpush订阅。
        """
        return self._push_registry.list()

    def push_subscription(self, subscription: dict[str, Any]) -> None:
        """
        添加或更新webpush订阅。
        """
        self._push_registry.upsert(subscription)

    def remove_subscription(self, subscription: dict[str, Any]) -> bool:
        """
        根据 endpoint 移除webpush订阅，返回是否实际删除。
        """
        return self._push_registry.remove(subscription)

    def stop_workflow(self, workflow_id: int) -> None:
        """
        停止工作流。
        """
        runtime_stop_state.stop_workflow(workflow_id)

    def workflow_resume(self, workflow_id: int) -> None:
        """
        恢复工作流。
        """
        runtime_stop_state.resume_workflow(workflow_id)

    def is_workflow_stopped(self, workflow_id: int) -> bool:
        """
        是否停止工作流。
        """
        return runtime_stop_state.is_workflow_stopped(workflow_id)

    def stop_transfer(self, path: str) -> None:
        """
        停止文件整理。
        """
        runtime_stop_state.stop_transfer(path)

    def is_transfer_stopped(self, path: str) -> bool:
        """
        是否停止文件整理。
        """
        return runtime_stop_state.consume_transfer_stop(path)

    @property
    def loop(self) -> AbstractEventLoop:
        """
        返回由应用生命周期登记的主事件循环。
        """
        return self._loop_registry.require()

    def set_loop(self, loop: AbstractEventLoop) -> object:
        """
        登记主事件循环，并返回仅供当前生命周期释放的 owner。
        """
        return self._loop_registry.register(loop)

    def clear_loop(self, owner: object) -> None:
        """
        释放指定 owner，保留仍然有效的其他生命周期登记。
        """
        self._loop_registry.release(owner)


global_vars = GlobalVar(
    loop_registry=main_loop_registry,
    push_registry=webpush_registry,
)
