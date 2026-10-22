"""
系统配置应用服务与组合根注入点。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Optional, Protocol, TypeVar, cast

from app.application.database import AsyncDatabaseExecutor
from app.schemas.common import JsonData
from app.schemas.types import MediaType, SystemConfigKey

# 系统配置值在进入持久化端口前使用的规范化函数。
SystemConfigValueNormalizer = Callable[[Any, Any], Any]

# 系统配置提交成功后的运行时变更发布函数。
SystemConfigChangePublisher = Callable[[Any, Any], Awaitable[None]]

T = TypeVar("T")


class SystemConfigReader(Protocol):
    """
    持久化用户配置的最小只读端口。
    """

    def get(self, key: Any = None) -> Any:
        """
        读取配置。

        :param key: 待查询的配置键，None 表示读取全部配置
        :return: 配置值的独立副本；单项不存在时为 None，不指定键时返回全部配置映射
        """


class SystemConfigWriter(Protocol):
    """
    持久化用户配置的最小写入端口。
    """

    def set(self, key: Any, value: Any) -> bool | None:
        """
        写入配置。

        :param key: 已登记的 SystemConfigKey 配置键
        :param value: 待持久化的配置值，空值与假值仍是有效配置
        :return: True 表示已写入，False 表示失败，None 表示值未变化
        """

    def delete(self, key: Any) -> Any:
        """
        删除配置。

        :param key: 已登记的 SystemConfigKey 配置键
        :return: 持久化端口返回的删除结果
        """

    def increment(self, key: SystemConfigKey, step: int = 1) -> int:
        """
        原子递增整数配置并返回递增后的值。

        :param key: 已登记的 SystemConfigKey 配置键
        :param step: 本次增加的整数步长，默认 1
        :return: 提交后的整数配置值，原值为空时按 0 起算
        """

    def update_atomically(
        self,
        key: Any,
        mutation: Callable[[Any, Any], tuple[T, Any]],
    ) -> T:
        """
        在持久化写锁内读取旧值、提交新值并返回业务结果。

        :param key: 已登记的 SystemConfigKey 配置键
        :param mutation: 接收事务会话和旧值，返回业务结果与新配置值；旧值不存在时为 None
        :return: 与新配置值一起提交成功的业务结果
        """


class ConfigurationRepository(SystemConfigReader, SystemConfigWriter, Protocol):
    """
    兼容同时提供读写能力的旧配置仓储。
    """


class SystemConfigStagingPort(Protocol):
    """
    跨表应用服务在调用方 Session 内读取并暂存系统配置。
    """

    def get_for_update(self, key: SystemConfigKey) -> JsonData:
        """
        同步锁定并读取一项配置。

        :param key: 已登记的 SystemConfigKey 配置键
        :return: 锁定后读取的 JSON 配置值，不存在时为 None
        """

    def stage_set(self, key: SystemConfigKey, value: JsonData) -> None:
        """
        同步暂存一项配置，不提交事务。

        :param key: 已登记的 SystemConfigKey 配置键
        :param value: 在调用方事务中暂存的 JSON 配置值
        """

    async def async_get_for_update(self, key: SystemConfigKey) -> JsonData:
        """
        异步锁定并读取一项配置。

        :param key: 已登记的 SystemConfigKey 配置键
        :return: 锁定后读取的 JSON 配置值，不存在时为 None
        """

    async def async_stage_set(self, key: SystemConfigKey, value: JsonData) -> None:
        """
        异步暂存一项配置，不提交事务。

        :param key: 已登记的 SystemConfigKey 配置键
        :param value: 在调用方事务中暂存的 JSON 配置值
        """


class MutableRuntimeSettings(Protocol):
    """
    部署设置对象对管理 API 暴露的最小可变合同。
    """

    def model_dump(
        self,
        *,
        include: Optional[set[str]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        """
        按白名单或排除列表导出当前设置。

        :param include: 只导出这些设置名，None 表示不使用白名单
        :param exclude: 不导出的设置名，None 表示不额外排除
        :return: 所选设置的名称到当前值映射
        """
        ...

    def update_settings(
        self,
        env: dict[str, Any],
    ) -> dict[str, tuple[Optional[bool], str]]:
        """
        批量更新部署设置并返回逐项结果。

        :param env: 待更新的设置名称到新值映射
        :return: 各设置对应的处理结果与说明；True 为成功，False 为失败，None 为无需更新
        """
        ...

    def update_setting(
        self,
        key: str,
        value: Any,
    ) -> tuple[Optional[bool], str]:
        """
        更新单个部署设置。

        :param key: 部署设置中声明的字段名称
        :param value: 待转换、校验和持久化的新值
        :return: 处理结果与说明；True 为成功，False 为失败，None 为无需更新
        """
        ...


@dataclass(frozen=True, slots=True)
class TransferRetryConfig:
    """
    整理失败重试用例在一次调用中使用的稳定配置快照。

    :param max_failed_retries: 整理失败后的最大自动重试次数，保留原始配置值交由用例校验
    """

    max_failed_retries: Any


@dataclass(frozen=True, slots=True)
class TokenRuntimeConfig:
    """
    令牌编解码在一次宿主生命周期内使用的安全配置快照。

    :param secret_key: 访问令牌签名与验证使用的宿主密钥
    :param resource_secret_key: 资源访问令牌签名与验证使用的独立密钥
    :param access_token_expire_minutes: 访问令牌有效期，单位为分钟
    :param resource_access_token_expire_seconds: 资源访问令牌有效期，单位为秒
    """

    secret_key: str
    resource_secret_key: str
    access_token_expire_minutes: int
    resource_access_token_expire_seconds: int


@dataclass(frozen=True, slots=True)
class SystemConfigWriteResult:
    """
    系统配置一次写入的变更状态和实际规范化值。

    :param changed: True 表示已写入变更，False 表示写入失败，None 表示无需更新
    :param normalized_value: 规范化后实际交给持久化端口的配置值
    """

    changed: bool | None
    normalized_value: Any


@dataclass(frozen=True, slots=True)
class ApiRuntimeConfig:
    """
    单次 API 请求使用的宿主配置快照。

    :param access_token_expire_minutes: 访问令牌有效期，单位为分钟
    :param btrfs_fsid_dedup: 是否启用 Btrfs FSID 子卷容量去重（仅 Linux amd64/arm64）
    :param ai_agent_enable: 是否启用 AI 智能体
    :param api_token: 宿主 API 认证密钥，None 表示快照未提供
    :param temp_path: 本次请求使用的临时文件目录
    :param media_recognize_share: 共享使用媒体识别数据
    :param subscribe_mode: 订阅刷新模式：spider 使用站点搜索，rss 使用 RSS 订阅
    :param search_source: 媒体搜索来源 themoviedb/douban/bangumi/anilist/imdb/musicbrainz/theaudiodb/doubanmusic，多个用,分隔
    :param media_extensions: 受支持的媒体文件扩展名集合
    :param subtitle_extensions: 受支持的字幕文件扩展名集合
    :param audio_extensions: 受支持的音频文件扩展名集合
    :param movie_rename_format: 电影重命名格式
    :param television_rename_format: 电视剧文件的重命名模板
    :param music_rename_format: 音乐重命名格式
    :param vapid_private_key: Web Push 消息签名使用的 VAPID 私钥
    :param vapid_subject: Web Push VAPID 声明中的联系地址或主体标识
    :param cookiecloud_enable_local: CookieCloud是否启动本地服务
    :param cookiecloud_auth_header: CookieCloud本地上传接口的X-CookieCloud-Auth期望值，留空表示不校验
    :param cookie_path: 站点 Cookie 文件存储目录
    :param root_path: 项目源码或程序安装根目录
    :param version_flag: 当前版本分支标识，默认 v3
    :param app_domain: 域名 格式；https://movie-pilot.org
    :param nginx_port: 前端监听端口
    :param passkey_require_uv: PassKey 是否强制用户验证（生物识别等）
    """

    access_token_expire_minutes: int
    btrfs_fsid_dedup: bool
    ai_agent_enable: bool
    api_token: str | None = None
    temp_path: Path = Path(".")
    media_recognize_share: bool = False
    subscribe_mode: str = "spider"
    search_source: str = ""
    media_extensions: tuple[str, ...] = ()
    subtitle_extensions: tuple[str, ...] = ()
    audio_extensions: tuple[str, ...] = ()
    movie_rename_format: str = ""
    television_rename_format: str = ""
    music_rename_format: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""
    cookiecloud_enable_local: bool = False
    cookiecloud_auth_header: Optional[str] = None
    cookie_path: Path = Path(".")
    root_path: Path = Path(".")
    version_flag: str = "v3"
    app_domain: str | None = None
    nginx_port: int = 3000
    passkey_require_uv: bool = True

    def rename_format(self, media_type: MediaType) -> str:
        """
        从请求快照返回指定媒体类型的稳定重命名格式。
        """
        if media_type == MediaType.TV:
            return self.television_rename_format
        if media_type == MediaType.MUSIC:
            return self.music_rename_format
        return self.movie_rename_format


@dataclass(frozen=True, slots=True)
class SchedulerRuntimeConfig:
    """
    一次 Scheduler 初始化或任务注册使用的稳定配置快照。

    :param dev: 是否开发模式
    :param timezone: 调度触发器采用的时区名称
    :param scheduler_workers: 调度执行器可使用的工作线程数
    :param db_backup_enable: 是否启用主程序数据库自动备份
    :param db_backup_cron: 定时备份的 Cron 表达式，留空时不注册定时任务
    :param cookiecloud_interval: CookieCloud同步间隔（分钟）
    :param mediaserver_sync_interval: 媒体服务器同步间隔（小时）
    :param subscribe_search: 订阅搜索开关
    :param subscribe_search_interval: 订阅搜索时间间隔（小时）
    :param subscribe_mode: 订阅刷新模式：spider 使用站点搜索，rss 使用 RSS 订阅
    :param subscribe_rss_interval: RSS订阅模式刷新时间间隔（分钟）
    :param data_cleanup_enable: 是否启用数据表定时清理
    :param sitedata_refresh_interval: 站点数据刷新间隔（小时）
    :param memory_gc_interval: 主动内存回收时间间隔（分钟），0为不启用
    :param ai_agent_enable: 是否启用 AI 智能体
    :param ai_agent_job_interval: AI 智能体定时任务检查间隔，单位为小时；0 表示停用
    :param usage_statistic_share: 安装版本统计上报
    :param site_link: 通知中跳转站点管理页面的地址，未配置域名时可为 None
    :param auto_update: 是否启用主程序版本更新检查
    :param auto_update_resource: 后台检查站点资源包，确认后由启动器在进程拉起前应用
    :param wallpaper: 登录页面壁纸来源：空字符串表示无壁纸，另支持 tmdb/bing/mediaserver/customize/static
    :param cookiecloud_configured: CookieCloud 服务地址与凭据是否已具备同步条件
    """

    dev: bool
    timezone: str
    scheduler_workers: int
    db_backup_enable: bool
    db_backup_cron: str
    cookiecloud_interval: Any
    mediaserver_sync_interval: Any
    subscribe_search: bool
    subscribe_search_interval: Any
    subscribe_mode: str
    subscribe_rss_interval: int
    data_cleanup_enable: bool
    sitedata_refresh_interval: Any
    memory_gc_interval: Any
    ai_agent_enable: bool
    ai_agent_job_interval: Any
    usage_statistic_share: bool
    site_link: str | None
    auto_update: bool = False
    auto_update_resource: bool = True
    wallpaper: str = ""
    cookiecloud_configured: bool = False

    @property
    def update_check_enabled(self) -> bool:
        """
        主程序或资源任一检查开启时保留共享的定时检测服务。
        """
        return self.auto_update or self.auto_update_resource


@dataclass(frozen=True, slots=True)
class ChainRuntimeConfig:
    """
    Chain 在一次宿主生命周期内使用的基础配置快照。

    :param media_extensions: 受支持的媒体文件扩展名集合
    :param api_port: 宿主 API 监听端口
    :param api_token: 宿主 API 认证密钥，None 表示快照未提供
    :param video_extensions: 受支持的视频文件扩展名集合
    :param subtitle_extensions: 受支持的字幕文件扩展名集合
    :param audio_extensions: 受支持的音频文件扩展名集合
    :param temporary_path: 应用运行时临时文件目录
    :param root_path: 项目源码或程序安装根目录
    :param config_path: 运行配置与持久化数据根目录
    :param frontend_path: 前端资源路径
    :param superuser: 超级管理员用户名；V3 首次启动时为空，由初始化页面设置
    :param media_recognize_share: 共享使用媒体识别数据
    :param auxiliary_auth_enable: 辅助认证，允许通过外部服务进行认证、单点登录以及自动创建用户
    :param global_image_cache: 全局图片缓存，将媒体图片缓存到本地
    :param encoding_detection_performance_mode: 是否启用编码探测的性能模式
    :param encoding_detection_min_confidence: 编码探测的最低置信度阈值
    :param data_cleanup_enable: 是否启用数据表定时清理
    :param data_cleanup_message_days: 消息表保留天数，0为不清理
    :param data_cleanup_download_history_days: 下载历史表保留天数，0为不清理
    :param data_cleanup_site_userdata_days: 站点用户数据表保留天数，0为不清理
    :param data_cleanup_transfer_history_days: 整理历史表保留天数，0为不清理
    :param data_cleanup_download_failure_days: 下载失败冷却记录保留天数，0为不清理
    :param data_cleanup_subscribe_history_days: 订阅完成历史保留天数，0为不清理
    :param data_cleanup_agent_chat_days: Agent 会话历史保留天数，0为不清理
    :param data_cleanup_agent_task_run_days: Agent 定时任务运行历史保留天数，0为不清理
    :param data_cleanup_outbox_completed_days: Outbox 已完成记录保留天数，0为不清理
    :param data_cleanup_outbox_dead_days: Outbox 死信记录保留天数，0为不清理
    :param download_subtitle: 下载站点字幕
    :param scrap_season_image_to_root: 季图片是否额外写入剧集根目录；默认开启以兼容已有媒体库
    :param scrap_image_alias: 是否为背景图和缩略图写入兼容别名文件
    :param scrap_4k_background: 是否获取 Fanart 提供的 4K 背景图
    :param lyrics_batch_timeout: 单次音乐刮削批次用于在线歌词查询的总预算（秒）
    :param music_metadata_to_simplified: 是否将识别到的音乐标题、艺术家、专辑等标准元数据转换为简体中文
    :param music_lyrics_to_simplified: 是否将写入 LRC/TXT 的歌词正文转为简体中文；保留时间轴、LRC 标签和来源 Lyricsfile
    :param music_cue_enable: 是否读取关联 CUE；已分轨音频可关闭以忽略错误索引，整轨专辑应保持开启
    :param music_release_region_priority: MusicBrainz 发行地区优先级，按 ISO 3166-1 两位代码从高到低排列
    :param music_release_script_priority: MusicBrainz 发行文字字形优先级，按 ISO 15924 四位代码从高到低排列
    :param recognize_plugin_first: 优先使用辅助识别
    :param ai_agent_enable: 是否启用 AI 智能体
    :param ai_agent_global: 是否由 AI 智能体统一接管消息处理
    :param ai_agent_retry_transfer: AI智能体自动重试整理失败记录开关
    :param llm_provider: LLM提供商（支持内置 provider，以及从 models.dev 动态补充的平台）
    :param llm_model: LLM模型名称
    :param search_resource_pages: 搜索资源获取页数
    :param ai_recommend_enabled: 是否启用AI推荐
    :param ai_recommend_max_items: AI推荐条目数量限制
    :param ai_recommend_user_preference: AI推荐用户偏好
    :param max_search_name_limit: 最大搜索名称数量
    :param search_multiple_name: 搜索多个名称
    :param search_source: 媒体搜索来源 themoviedb/douban/bangumi/anilist/imdb/musicbrainz/theaudiodb/doubanmusic，多个用,分隔
    :param search_threadpool_size: 搜索用例可使用的线程池容量
    :param transfer_threads: 文件整理线程数
    :param transfer_failure_notification_aggregation: 是否按媒体聚合整理失败通知，关闭时保持逐条发送
    :param transfer_success_notification_aggregation: 是否按批次聚合电影、电视剧和音乐的整理成功通知
    :param transfer_task_timeout: 外部接管的运行中整理任务无状态心跳超时（分钟），0 表示禁用
    :param scrape_follow_tmdb: 是否使用 TMDB 的季集编排刮削电视剧元数据
    :param metadata_cache_ttl: 识别元数据缓存的有效期，单位为秒
    :param auto_download_user: 允许交互搜索后自动下载的用户标识，多个用逗号分隔，None 表示未配置
    :param resource_url: 资源搜索页面地址，未配置时为 None
    :param history_url: 整理历史页面地址，未配置时为 None
    :param downloading_url: 正在下载页面地址，未配置时为 None
    :param movie_subscribe_url: 电影订阅页面地址，未配置时为 None
    :param television_subscribe_url: 电视剧订阅页面地址，未配置时为 None
    :param music_subscribe_url: 音乐订阅页面地址，未配置时为 None
    :param user_agent: 站点及外部服务请求使用的默认 User-Agent
    :param normal_user_agent: 模拟普通浏览器请求使用的 User-Agent
    :param proxy: 供 requests 兼容客户端使用的协议到代理地址映射，None 表示不配置代理
    :param proxy_server: 供浏览器使用的代理连接参数，None 表示不配置代理
    :param proxy_host: 网络代理服务器地址
    :param github_headers: 访问 GitHub 时使用的请求头，可能包含授权凭据
    :param cookiecloud_blacklist: CookieCloud同步黑名单，多个域名,分割
    :param subscribe_mode: 订阅刷新模式：spider 使用站点搜索，rss 使用 RSS 订阅
    :param no_cache_site_key: 不缓存站点资源的域名关键字，多个使用,分隔；留空时所有站点使用缓存
    :param refresh_batch_size: 站点订阅刷新时每批处理的资源数
    :param torrent_cache_size: 站点种子缓存容量
    :param site_url: 站点管理页面地址，未配置时为 None
    :param workflow_url: 工作流管理页面地址，未配置时为 None
    :param season_zero_names: 重命名时可识别为特别篇季的名称集合
    :param movie_rename_format: 电影重命名格式
    :param television_rename_format: 电视剧文件的重命名模板
    :param music_rename_format: 音乐重命名格式
    :param tmdb_image_domain: TMDB图片地址
    :param wallpaper: 登录页面壁纸来源：空字符串表示无壁纸，另支持 tmdb/bing/mediaserver/customize/static
    :param wallpaper_image_url: 静态壁纸地址，可使用前端可访问的本地路径或 URL
    :param customize_wallpaper_api_url: 自定义壁纸api地址
    :param security_image_suffixes: 允许的图片文件后缀格式
    :param image_proxy_allowed_private_ranges: 图片代理允许访问的非公网 IP/CIDR
    :param cache_path: 运行缓存文件根目录
    :param global_image_cache_days: 全局图片缓存保留天数
    """

    media_extensions: tuple[str, ...]
    api_port: int = 3000
    api_token: str | None = None
    video_extensions: tuple[str, ...] = ()
    subtitle_extensions: tuple[str, ...] = ()
    audio_extensions: tuple[str, ...] = ()
    temporary_path: Path = Path(".")
    root_path: Path = Path(".")
    config_path: Path = Path(".")
    frontend_path: Path = Path(".")
    superuser: str = ""
    media_recognize_share: bool = False
    auxiliary_auth_enable: bool = False
    global_image_cache: bool = False
    encoding_detection_performance_mode: bool = False
    encoding_detection_min_confidence: float = 0.5
    data_cleanup_enable: bool = False
    data_cleanup_message_days: Any = 0
    data_cleanup_download_history_days: Any = 0
    data_cleanup_site_userdata_days: Any = 0
    data_cleanup_transfer_history_days: Any = 0
    data_cleanup_download_failure_days: Any = 0
    data_cleanup_subscribe_history_days: Any = 0
    data_cleanup_agent_chat_days: Any = 0
    data_cleanup_agent_task_run_days: Any = 0
    data_cleanup_outbox_completed_days: Any = 0
    data_cleanup_outbox_dead_days: Any = 0
    download_subtitle: bool = True
    scrap_season_image_to_root: bool = True
    scrap_image_alias: bool = True
    scrap_4k_background: bool = True
    lyrics_batch_timeout: int = 120
    music_metadata_to_simplified: bool = True
    music_lyrics_to_simplified: bool = False
    music_cue_enable: bool = True
    music_release_region_priority: tuple[str, ...] = ()
    music_release_script_priority: tuple[str, ...] = ()
    recognize_plugin_first: bool = False
    ai_agent_enable: bool = False
    ai_agent_global: bool = False
    ai_agent_retry_transfer: bool = False
    llm_provider: str = ""
    llm_model: str = ""
    search_resource_pages: int = 1
    ai_recommend_enabled: bool = False
    ai_recommend_max_items: int = 50
    ai_recommend_user_preference: str = ""
    max_search_name_limit: int = 3
    search_multiple_name: bool = False
    search_source: str = "themoviedb"
    search_threadpool_size: int = 1
    transfer_threads: int = 1
    transfer_failure_notification_aggregation: bool = True
    transfer_success_notification_aggregation: bool = True
    transfer_task_timeout: int = 120
    scrape_follow_tmdb: bool = True
    metadata_cache_ttl: int = 3600
    auto_download_user: Optional[str] = None
    resource_url: Optional[str] = None
    history_url: Optional[str] = None
    downloading_url: Optional[str] = None
    movie_subscribe_url: Optional[str] = None
    television_subscribe_url: Optional[str] = None
    music_subscribe_url: Optional[str] = None
    user_agent: str = ""
    normal_user_agent: str = ""
    proxy: Any = None
    proxy_server: Any = None
    proxy_host: Optional[str] = None
    github_headers: Any = None
    cookiecloud_blacklist: Any = None
    subscribe_mode: str = "spider"
    no_cache_site_key: str = ""
    refresh_batch_size: int = 50
    torrent_cache_size: int = 1000
    site_url: Optional[str] = None
    workflow_url: Optional[str] = None
    season_zero_names: tuple[str, ...] = ()
    movie_rename_format: str = ""
    television_rename_format: str = ""
    music_rename_format: str = ""
    tmdb_image_domain: str = "image.tmdb.org"
    wallpaper: str = ""
    wallpaper_image_url: Optional[str] = None
    customize_wallpaper_api_url: Optional[str] = None
    security_image_suffixes: tuple[str, ...] = ()
    image_proxy_allowed_private_ranges: tuple[str, ...] = ()
    cache_path: Path = Path(".")
    global_image_cache_days: int = 7

    def rename_format(self, media_type: MediaType) -> str:
        """
        从快照返回指定媒体类型的稳定重命名格式。
        """
        if media_type == MediaType.TV:
            return self.television_rename_format
        if media_type == MediaType.MUSIC:
            return self.music_rename_format
        return self.movie_rename_format

    def tmdb_image_url(
        self,
        file_path: Optional[str],
        file_size: str = "original",
    ) -> Optional[str]:
        """
        使用快照中的 TMDB 图片域名构造完整图片地址。
        """
        if not file_path:
            return None
        normalized_path = file_path.removeprefix("/")
        return f"https://{self.tmdb_image_domain}/t/p/{file_size}/{normalized_path}"


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    """
    由启动组合根提供的 API、Scheduler 与 Chain 配置快照工厂。

    :param api: 按需构建 API 请求配置快照的工厂
    :param scheduler: 按需构建调度配置快照的工厂
    :param chain: 按需构建业务链配置快照的工厂
    """

    api: Callable[[], ApiRuntimeConfig]
    scheduler: Callable[[], SchedulerRuntimeConfig]
    chain: Callable[[], ChainRuntimeConfig]


class RuntimeSettingsService:
    """
    隔离管理 API 与全局 Settings 实例的读写适配服务。
    """

    def __init__(self, settings: MutableRuntimeSettings) -> None:
        """
        保存由 Startup 注入的唯一部署设置对象。
        """
        self._settings = settings

    def contains(self, key: str) -> bool:
        """
        判断部署设置是否声明指定字段或属性。
        """
        return hasattr(self._settings, key)

    def get(self, key: str, default: Any = None) -> Any:
        """
        读取一个部署设置，缺失时返回调用方默认值。
        """
        return getattr(self._settings, key, default)

    def snapshot(
        self,
        *,
        include: Optional[set[str]] = None,
        exclude: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        """
        导出脱离可变 Settings 对象的请求级字典快照。
        """
        return self._settings.model_dump(include=include, exclude=exclude)

    def update_many(
        self,
        env: dict[str, Any],
    ) -> dict[str, tuple[Optional[bool], str]]:
        """
        批量更新部署设置。
        """
        return self._settings.update_settings(env=env)

    def update(self, key: str, value: Any) -> tuple[Optional[bool], str]:
        """
        更新单个部署设置。
        """
        return self._settings.update_setting(key, value)


class SystemConfigService:
    """
    系统配置读写应用服务。
    """

    def __init__(
        self,
        repository: ConfigurationRepository | None = None,
        *,
        reader: SystemConfigReader | None = None,
        writer: SystemConfigWriter | None = None,
        async_executor: AsyncDatabaseExecutor | None = None,
        value_normalizer: SystemConfigValueNormalizer | None = None,
        change_publisher: SystemConfigChangePublisher | None = None,
    ) -> None:
        """
        注入读写端口、异步事务执行能力、规范化边界和变更发布端口。
        """
        resolved_reader = reader or repository
        resolved_writer = writer or repository
        if resolved_reader is None or resolved_writer is None:
            raise ValueError("系统配置服务必须同时提供 reader 与 writer")
        self._reader = resolved_reader
        self._writer = resolved_writer
        self._async_executor = async_executor
        self._value_normalizer = value_normalizer
        self._change_publisher = change_publisher

    def configure_change_publisher(
        self,
        publisher: SystemConfigChangePublisher | None,
    ) -> None:
        """
        登记或清除异步配置变更发布器，供组合根绑定宿主事件总线。
        """
        self._change_publisher = publisher

    async def _publish_async_change(
        self,
        key: Any,
        value: Any,
        changed: bool | None,
    ) -> None:
        """
        仅在异步写入确实改变持久化值后发布运行时变更。
        """
        if changed is True and self._change_publisher is not None:
            await self._change_publisher(key, value)

    def get(self, key: Any = None) -> Any:
        """
        读取配置。
        """
        return self._reader.get(key)

    def set(self, key: Any, value: Any) -> bool | None:
        """
        写入配置。
        """
        return self.set_with_normalized_value(key, value).changed

    def set_with_normalized_value(
        self,
        key: Any,
        value: Any,
    ) -> SystemConfigWriteResult:
        """
        规范化一次后同步写入，并返回实际交给持久化端口的值。
        """
        normalized_value = self.normalize_value(key, value)
        return SystemConfigWriteResult(
            changed=self._writer.set(key, normalized_value),
            normalized_value=normalized_value,
        )

    def normalize_value(self, key: Any, value: Any) -> Any:
        """
        在写入和配置事件发布前返回同一份规范化配置值。
        """
        if self._value_normalizer is None:
            return value
        return self._value_normalizer(key, value)

    def increment(self, key: SystemConfigKey, step: int = 1) -> int:
        """
        原子递增整数系统配置，保持计数更新由持久化端口负责。
        """
        return self._writer.increment(key, step)

    async def async_set(self, key: Any, value: Any) -> bool | None:
        """
        异步写入配置，并在返回前等待已登记的运行时重载完成。
        """
        result = await self.async_set_with_normalized_value(key, value)
        await self._publish_async_change(key, result.normalized_value, result.changed)
        return result.changed

    async def async_set_with_normalized_value(
        self,
        key: Any,
        value: Any,
    ) -> SystemConfigWriteResult:
        """
        规范化一次后异步写入，并返回配置事件应发布的同一值。
        """
        if self._async_executor is None:
            raise RuntimeError("系统配置异步数据库执行端口尚未配置")
        normalized_value = self.normalize_value(key, value)
        result = await self._async_executor.run(
            partial(self._writer.set, key, normalized_value)
        )
        return SystemConfigWriteResult(
            changed=result,
            normalized_value=normalized_value,
        )

    def update_atomically(
        self,
        key: Any,
        mutation: Callable[[Any], tuple[T, Any]],
    ) -> T:
        """
        在持久化写锁和短事务内原子变更配置，不向应用回调暴露数据库会话。

        :param key: 系统配置键
        :param mutation: 根据最新值返回业务结果和新值的纯变更回调
        :return: 事务提交后返回变更回调的业务结果
        """
        def apply(_session: Any, current: Any) -> tuple[T, Any]:
            """
            把不暴露数据库会话的应用层 mutation 适配到底层原子仓储。
            """
            result, value = mutation(current)
            return result, self.normalize_value(key, value)

        return self._writer.update_atomically(key, apply)

    async def async_update_atomically(
        self,
        key: Any,
        mutation: Callable[[Any], tuple[T, Any]],
    ) -> T:
        """
        在线程化短事务内复用同步原子变更，避免事件循环中的阻塞数据库I/O。

        :param key: 系统配置键
        :param mutation: 根据最新值返回业务结果和新值的纯变更回调
        :return: 事务提交后返回变更回调的业务结果
        """
        if self._async_executor is None:
            raise RuntimeError("系统配置异步数据库执行端口尚未配置")
        return cast(T, await self._async_executor.run(partial(self.update_atomically, key, mutation)))

    async def async_delete(self, key: Any) -> Any:
        """
        异步删除配置，并等待数据库提交或回滚完成。
        """
        if self._async_executor is None:
            raise RuntimeError("系统配置异步数据库执行端口尚未配置")
        return await self._async_executor.run(partial(self._writer.delete, key))

    def delete(self, key: Any) -> Any:
        """
        删除配置。
        """
        return self._writer.delete(key)


_configured_system_config: SystemConfigService | None = None
_transfer_retry_config_provider: Callable[[], TransferRetryConfig] | None = None
_token_runtime_config_provider: Callable[[], TokenRuntimeConfig] | None = None
_runtime_configuration: RuntimeConfiguration | None = None
_runtime_settings_service: RuntimeSettingsService | None = None


def configure_system_config(service: SystemConfigService) -> None:
    """
    由启动组合根登记系统配置服务。
    """
    global _configured_system_config
    _configured_system_config = service


def reset_system_config() -> None:
    """
    清除当前 lifespan 的系统配置服务，避免重复启动复用旧实例。
    """
    global _configured_system_config
    _configured_system_config = None


def get_configured_system_config() -> SystemConfigService:
    """
    返回启动阶段登记的系统配置服务。
    """
    if _configured_system_config is None:
        raise RuntimeError("系统配置服务尚未配置")
    return _configured_system_config


def configure_transfer_retry_config(
    provider: Callable[[], TransferRetryConfig],
) -> None:
    """
    由组合根登记整理失败重试快照工厂。
    """
    global _transfer_retry_config_provider
    _transfer_retry_config_provider = provider


def reset_transfer_retry_config() -> None:
    """
    清除整理失败重试快照工厂，使未装配读取重新失败。
    """
    global _transfer_retry_config_provider
    _transfer_retry_config_provider = None


def get_transfer_retry_config() -> TransferRetryConfig:
    """
    为一次整理历史判定创建不可变配置快照。
    """
    if _transfer_retry_config_provider is None:
        raise RuntimeError("整理失败重试配置尚未装配")
    return _transfer_retry_config_provider()


def configure_token_runtime_config(
    provider: Callable[[], TokenRuntimeConfig],
) -> None:
    """
    由启动组合根登记令牌安全配置快照工厂。
    """
    global _token_runtime_config_provider
    _token_runtime_config_provider = provider


def reset_token_runtime_config() -> None:
    """
    清除令牌安全配置快照工厂，禁止复用上一 lifespan 的密钥。
    """
    global _token_runtime_config_provider
    _token_runtime_config_provider = None


def get_token_runtime_config() -> TokenRuntimeConfig:
    """
    返回当前令牌编解码使用的不可变配置快照。
    """
    if _token_runtime_config_provider is None:
        raise RuntimeError("令牌运行时配置尚未装配")
    return _token_runtime_config_provider()


def configure_runtime_configuration(configuration: RuntimeConfiguration) -> None:
    """
    由启动组合根登记各运行面使用的类型化配置快照工厂。
    """
    global _runtime_configuration
    _runtime_configuration = configuration


def reset_runtime_configuration() -> None:
    """
    清除各运行面共享的类型化配置快照工厂。
    """
    global _runtime_configuration
    _runtime_configuration = None


def configure_runtime_settings(service: RuntimeSettingsService) -> None:
    """
    由组合根登记管理 API 使用的部署设置服务。
    """
    global _runtime_settings_service
    _runtime_settings_service = service


def reset_runtime_settings() -> None:
    """
    清除管理 API 使用的部署设置服务。
    """
    global _runtime_settings_service
    _runtime_settings_service = None


def reset_configuration_services() -> None:
    """
    清除当前 lifespan 登记的全部配置服务与快照 provider。
    """
    reset_system_config()
    reset_transfer_retry_config()
    reset_token_runtime_config()
    reset_runtime_configuration()
    reset_runtime_settings()


def get_runtime_settings() -> RuntimeSettingsService:
    """
    返回组合根登记的部署设置服务。
    """
    if _runtime_settings_service is None:
        raise RuntimeError("部署设置服务尚未装配")
    return _runtime_settings_service


def get_scheduler_runtime_config() -> SchedulerRuntimeConfig:
    """
    为一次调度操作创建不可变配置快照。
    """
    if _runtime_configuration is None:
        raise RuntimeError("运行时配置尚未装配")
    return _runtime_configuration.scheduler()


def get_api_runtime_config_snapshot() -> ApiRuntimeConfig:
    """
    为一次 API 调用创建不可变配置快照。
    """
    if _runtime_configuration is None:
        raise RuntimeError("运行时配置尚未装配")
    return _runtime_configuration.api()


def get_chain_runtime_config_snapshot() -> ChainRuntimeConfig:
    """
    为无实例兼容入口创建一次稳定的 Chain 配置快照。
    """
    if _runtime_configuration is None:
        raise RuntimeError("运行时配置尚未装配")
    return _runtime_configuration.chain()
