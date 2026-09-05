from dataclasses import dataclass
from datetime import datetime as _DateTime
from typing import Any, Literal, Optional
from uuid import uuid4 as _uuid4

from pydantic import BaseModel, Field, field_validator

from app.schemas.context import MediaInfo, MetaInfo, TorrentInfo
from app.schemas.rule import FilterRuleGroup


@dataclass
class ServiceInfo:
    """
    封装服务相关信息的数据类
    """

    # 名称
    name: Optional[str] = None
    # 实例
    instance: Optional[Any] = None
    # 模块
    module: Optional[Any] = None
    # 类型
    type: Optional[str] = None
    # 配置
    config: Optional[Any] = None


class MediaServerConf(BaseModel):
    """
    媒体服务器配置
    """

    # 名称
    name: Optional[str] = None
    # 类型 emby/zspace/jellyfin/plex/trimemedia/ugreen/navidrome
    type: Optional[str] = None
    # 配置
    config: Optional[dict] = Field(default_factory=dict)
    # 是否启用
    enabled: Optional[bool] = False
    # 同步媒体体库列表
    sync_libraries: Optional[list] = Field(default_factory=list)
    # 自动同步间隔（小时），未设置时使用旧全局配置
    sync_interval: Optional[int] = None

    @field_validator("sync_interval", mode="before")
    @classmethod
    def validate_sync_interval(cls, value: Any) -> Optional[int]:
        """
        兼容前端清空输入框后残留的空字符串等非法值，避免历史配置导致模块初始化失败

        :param value: 原始配置值
        :return: 合法的间隔小时数，无法解析时返回 None
        """
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class DownloaderConf(BaseModel):
    """
    下载器配置
    """

    # 名称
    name: Optional[str] = None
    # 类型 qbittorrent/transmission/rtorrent
    type: Optional[str] = None
    # 是否默认
    default: Optional[bool] = False
    # 配置
    config: Optional[dict] = Field(default_factory=dict)
    # 是否启用
    enabled: Optional[bool] = False
    # 路径映射
    path_mapping: Optional[list[tuple[str, str]]] = Field(default_factory=list)


class NotificationConf(BaseModel):
    """
    通知配置
    """

    # 稳定渠道身份；名称变化时保持不变，用于运行态凭据和跨页面引用。
    id: str = Field(default_factory=lambda: str(_uuid4()))
    # 名称
    name: Optional[str] = None
    # 类型 telegram/wechat/feishu/vocechat/synologychat/slack/webpush/qqbot
    type: Optional[str] = None
    # 配置
    config: Optional[dict[str, Any]] = Field(default_factory=dict)
    # 场景开关名称列表；NotificationSwitchConf 属于全局通知范围配置，不是渠道字段。
    switchs: Optional[list[str]] = Field(default_factory=list)
    # 是否启用
    enabled: Optional[bool] = False


class NotificationSwitchConf(BaseModel):
    """
    通知场景开关配置
    """

    # 场景名称
    type: str = None
    # 通知范围 all/user/admin
    action: Optional[str] = "all"


class PluginMarketSyncRequest(BaseModel):
    """
    插件市场仓库同步请求
    """

    # Wiki 插件文档 Markdown 原始文件地址
    wiki_url: Optional[str] = Field(
        default="https://raw.githubusercontent.com/jxxghp/MoviePilot-Wiki/main/plugin.md",
    )


class StorageConf(BaseModel):
    """
    存储配置
    """

    # 类型 local/alipan/u115/rclone/alist
    type: Optional[str] = None
    # 名称
    name: Optional[str] = None
    # 配置
    config: Optional[dict] = Field(default_factory=dict)


class SystemEnvironmentUpdateData(BaseModel):
    """环境配置更新的成功项和失败项。"""

    success_updates: dict[str, tuple[Optional[bool], str]] = Field(default_factory=dict)
    failed_updates: dict[str, tuple[Optional[bool], str]] = Field(default_factory=dict)


class SystemSettingsUpdateRequest(BaseModel):  # type: ignore[misc]
    """统一系统设置更新请求。"""

    setting_key: str = Field(
        description=(
            "Exact setting key. Accepts a Settings field name, a SystemConfigKey value or enum name, "
            "or an alias that resolves to one unique setting. Call config.system.get with group or "
            "keyword first when the key is unknown."
        )
    )
    value: Any = Field(
        default=None,
        description=(
            "New value or list item. For replace, send the complete value. For merge_dict, send the "
            "object fragment to merge. For upsert_list_item or remove_list_item, send one object or scalar item."
        ),
    )
    operation: Literal[
        "replace",
        "merge_dict",
        "upsert_list_item",
        "remove_list_item",
    ] = Field(
        default="replace",
        description=(
            "replace overwrites the complete value; merge_dict shallow-merges an object; "
            "upsert_list_item inserts or replaces one matched list item; remove_list_item removes one matched list item."
        ),
    )
    remove_keys: list[str] = Field(
        default_factory=list,
        description="Object keys to remove after merge_dict applies the supplied value.",
    )
    match_field: Optional[str] = Field(
        default=None,
        description=(
            "Object field used to match a list item. Downloaders, MediaServers, Notifications, Directories, "
            "and Storages default to name; NotificationSwitchs defaults to type. Supply it for other object lists."
        ),
    )
    match_value: Any = Field(
        default=None,
        description="Value compared against match_field. If omitted, use value[match_field]; scalar lists use value directly.",
    )


class CustomIdentifiersUpdateRequest(BaseModel):  # type: ignore[misc]
    """完整替换自定义识别词的请求。"""

    identifiers: list[str] = Field(
        default_factory=list,
        description="Complete ordered list of custom recognition identifier rules.",
    )
    expected_identifiers: Optional[list[str]] = Field(
        default=None,
        description=(
            "Previously read complete ordered list. When supplied, reject the replacement if the stored list has changed."
        ),
    )


SystemUpdateType = Literal["application", "resources"]


class SystemUpdateItemStatus(BaseModel):  # type: ignore[misc]
    """单类升级的可恢复状态快照。"""

    type: SystemUpdateType
    state: Literal[
        "idle",
        "available",
        "downloading",
        "ready",
        "installing",
        "failed",
    ] = "idle"
    current_version: Optional[str] = None
    version: Optional[str] = None
    frontend_version: Optional[str] = None
    current_auth_version: Optional[str] = None
    auth_version: Optional[str] = None
    current_indexer_version: Optional[str] = None
    indexer_version: Optional[str] = None
    release_name: Optional[str] = None
    release_notes: Optional[str] = None
    published_at: Optional[str] = None
    checked_at: Optional[str] = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    progress: int = 0
    error: Optional[str] = None
    can_update: bool = False
    can_install: bool = False


class SystemUpdateRequest(BaseModel):  # type: ignore[misc]
    """主程序或站点资源升级动作的请求体。"""

    target: SystemUpdateType = Field(
        default="application",
        description="升级目标：application 表示主程序，resources 表示认证和索引资源。",
    )


class SystemUpdateStatus(BaseModel):
    """主程序与站点资源后台更新的聚合状态快照。"""

    state: Literal[
        "idle",
        "available",
        "downloading",
        "ready",
        "installing",
        "failed",
    ] = "idle"
    current_version: str
    version: Optional[str] = None
    frontend_version: Optional[str] = None
    release_name: Optional[str] = None
    release_notes: Optional[str] = None
    published_at: Optional[str] = None
    checked_at: Optional[str] = None
    downloaded_bytes: int = 0
    total_bytes: int = 0
    progress: int = 0
    error: Optional[str] = None
    can_update: bool = False
    can_install: bool = False
    updates: list[SystemUpdateItemStatus] = Field(default_factory=list)


class PluginMarketSyncData(BaseModel):
    """Wiki 插件市场仓库同步结果。"""

    value: str
    repos: list[str] = Field(default_factory=list)
    wiki_repos: list[str] = Field(default_factory=list)
    added_count: int = 0
    total_count: int = 0
    source_url: str


class RuleTestData(BaseModel):
    """过滤规则测试的输入、识别和匹配明细。"""

    title: str
    subtitle: Optional[str] = None
    rulegroup_name: str
    rulegroup: Optional[FilterRuleGroup] = None
    meta_info: MetaInfo
    media_info: Optional[MediaInfo] = None
    torrent_info: TorrentInfo
    priority: Optional[int] = None
    matched: bool = False


class NetTestTarget(BaseModel):
    """前端可选择的网络测试目标。"""

    id: str
    name: str
    icon: str


class SystemModuleInfo(BaseModel):
    """已加载系统模块摘要。"""

    id: str
    name: str
    name_i18n: str
    name_key: str


class SystemModuleListData(BaseModel):
    """已加载系统模块列表。"""

    modules: list[SystemModuleInfo] = Field(default_factory=list)


class DatabaseBackupArtifactData(BaseModel):  # type: ignore[misc]
    """Web 管理端可见的受管数据库备份摘要。"""

    name: str  # 受管文件名，不包含宿主目录
    db_type: str  # 创建制品的数据库类型
    created_at: _DateTime  # 从受管文件名解析出的创建时间
    size: int  # 备份文件字节数


class DatabaseBackupVerificationData(BaseModel):  # type: ignore[misc]
    """受管数据库备份的脱敏校验结果。"""

    valid: bool  # 是否通过当前数据库类型的内容校验
    method: str  # SQLite integrity_check 或 PostgreSQL 归档目录校验


class TransferDirectoryConf(BaseModel):
    """
    文件整理目录配置
    """

    # 名称
    name: Optional[str] = None
    # 优先级
    priority: Optional[int] = 0
    # 存储
    storage: Optional[str] = None
    # 下载目录
    download_path: Optional[str] = None
    # 适用媒体类型
    media_type: Optional[str] = None
    # 适用媒体类别
    media_category: Optional[str] = None
    # 适用媒体类别稳定 ID；media_category 在兼容期保存路径快照
    media_category_id: Optional[str] = None
    # 下载类型子目录
    download_type_folder: Optional[bool] = False
    # 下载类别子目录
    download_category_folder: Optional[bool] = False
    # 监控方式 downloader/monitor，None为不监控
    monitor_type: Optional[str] = None
    # 监控模式 fast / compatibility
    monitor_mode: Optional[str] = "fast"
    # 整理方式 move/copy/link/softlink
    transfer_type: Optional[str] = None
    # 文件覆盖模式 always/size/never/latest
    overwrite_mode: Optional[str] = None
    # 整理到媒体库目录
    library_path: Optional[str] = None
    # 媒体库目录存储
    library_storage: Optional[str] = None
    # 智能重命名
    renaming: Optional[bool] = False
    # 刮削
    scraping: Optional[bool] = False
    # 是否发送通知
    notify: Optional[bool] = True
    # 媒体库类型子目录
    library_type_folder: Optional[bool] = False
    # 媒体库类别子目录
    library_category_folder: Optional[bool] = False
