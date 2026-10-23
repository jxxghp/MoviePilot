"""
系统配置、模块目录、网络测试和更新状态的数据结构。
"""

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
    封装已配置服务的实例、所属模块与原始配置。

    :param name: 服务配置中的名称
    :param instance: 服务实例，尚未创建时为空
    :param module: 提供该服务的模块实例
    :param type: 服务类型标识
    :param config: 该服务对应的配置对象
    """

    name: Optional[str] = None
    instance: Optional[Any] = None
    module: Optional[Any] = None
    type: Optional[str] = None
    config: Optional[Any] = None


class MediaServerConf(BaseModel):
    """
    媒体服务器配置。
    """

    name: Optional[str] = Field(default=None, description="名称")
    type: Optional[str] = Field(default=None, description="类型 emby/zspace/jellyfin/plex/trimemedia/ugreen/navidrome")
    config: Optional[dict[str, Any]] = Field(default_factory=dict, description="配置")
    enabled: Optional[bool] = Field(default=False, description="是否启用")
    sync_libraries: Optional[list[str]] = Field(default_factory=list, description="同步媒体体库列表")
    sync_interval: Optional[int] = Field(default=None, description="自动同步间隔（小时），未设置时使用旧全局配置")

    @field_validator("sync_interval", mode="before")
    @classmethod
    def validate_sync_interval(cls, value: Any) -> Optional[int]:
        """
        兼容前端清空输入框后残留的空字符串等非法值，避免历史配置导致模块初始化失败。

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
    下载器配置。
    """

    name: Optional[str] = Field(default=None, description="名称")
    type: Optional[str] = Field(default=None, description="类型 qbittorrent/transmission/rtorrent")
    default: Optional[bool] = Field(default=False, description="是否默认")
    config: Optional[dict[str, Any]] = Field(default_factory=dict, description="配置")
    enabled: Optional[bool] = Field(default=False, description="是否启用")
    path_mapping: Optional[list[tuple[str, str]]] = Field(default_factory=list, description="路径映射")


class NotificationConf(BaseModel):
    """
    通知配置。
    """

    id: str = Field(default_factory=lambda: str(_uuid4()), description="稳定渠道身份；名称变化时保持不变，用于运行态凭据和跨页面引用")
    name: Optional[str] = Field(default=None, description="名称")
    type: Optional[str] = Field(
        default=None,
        description="类型 telegram/wechat/feishu/vocechat/synologychat/slack/webpush/qqbot/apppush",
    )
    config: Optional[dict[str, Any]] = Field(default_factory=dict, description="配置")
    switchs: Optional[list[str]] = Field(
        default_factory=list,
        description="场景开关名称列表；NotificationSwitchConf 属于全局通知范围配置，不是渠道字段",
    )
    enabled: Optional[bool] = Field(default=False, description="是否启用")


class NotificationSwitchConf(BaseModel):
    """
    通知场景开关配置。
    """

    type: Optional[str] = Field(default=None, description="场景名称")
    action: Optional[str] = Field(default="all", description="通知范围 all/user/admin")


class PluginMarketSyncRequest(BaseModel):
    """
    插件市场仓库同步请求。
    """

    wiki_url: Optional[str] = Field(
        default="https://raw.githubusercontent.com/jxxghp/MoviePilot-Wiki/main/plugin.md",
        description="Wiki 插件文档 Markdown 原始文件地址",
    )


class StorageConf(BaseModel):
    """
    存储配置。
    """

    type: Optional[str] = Field(default=None, description="类型 local/alipan/u115/rclone/alist")
    name: Optional[str] = Field(default=None, description="名称")
    config: Optional[dict[str, Any]] = Field(default_factory=dict, description="配置")


class SystemEnvironmentUpdateData(BaseModel):
    """
    环境配置更新的成功项和失败项。
    """

    success_updates: dict[str, tuple[Optional[bool], str]] = Field(default_factory=dict, description="更新成功的环境项")
    failed_updates: dict[str, tuple[Optional[bool], str]] = Field(default_factory=dict, description="更新失败的环境项")


class SystemSettingsUpdateRequest(BaseModel):
    """
    统一系统设置更新请求。
    """

    setting_key: str = Field(
        description=(
            "精确的设置键，可使用 Settings 字段名、SystemConfigKey 值或枚举名，以及能唯一解析的别名；未知时先调用 config.system.get 按组或关键词查询"
        )
    )
    value: Any = Field(
        default=None,
        description=(
            "新值或列表项；replace 提交完整值，merge_dict 提交待合并的对象片段，列表增删操作提交单个对象或标量"
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
            "replace 整体替换；merge_dict 浅层合并对象；upsert_list_item 插入或替换匹配项；remove_list_item 删除匹配项"
        ),
    )
    remove_keys: list[str] = Field(
        default_factory=list,
        description="完成 merge_dict 合并后需要删除的对象键",
    )
    match_field: Optional[str] = Field(
        default=None,
        description=(
            "列表项匹配字段；下载器、媒体服务器、通知、目录和存储默认用 name，通知场景开关默认用 type，其他对象列表需显式指定"
        ),
    )
    match_value: Any = Field(
        default=None,
        description="与匹配字段比较的值；省略时使用 value 中对应字段，标量列表直接使用 value",
    )
    expected_revision: Optional[str] = Field(
        description=(
            "此设置最近读取的版本；提供时若期间有其他写入则拒绝更新，新 Agent 客户端应始终提供"
        ),
        default=None,
        min_length=1,
        max_length=128,
    )


class CustomIdentifiersUpdateRequest(BaseModel):
    """
    完整替换自定义识别词的请求。
    """

    identifiers: list[str] = Field(
        default_factory=list,
        description="按顺序排列的完整自定义识别词规则列表",
    )
    expected_identifiers: Optional[list[str]] = Field(
        default=None,
        description=(
            "最近读取的完整有序列表；提供时若现有列表已变化则拒绝替换"
        ),
    )


SystemUpdateType = Literal["application", "resources"]
SystemUpdateTarget = Literal["application", "resources", "all"]


class SystemUpdateItemStatus(BaseModel):
    """
    单类升级的可恢复状态快照。
    """

    type: SystemUpdateType = Field(description="类型标识")
    state: Literal[
        "idle",
        "available",
        "downloading",
        "ready",
        "installing",
        "failed",
    ] = Field(default="idle", description="当前业务状态")
    current_version: Optional[str] = Field(default=None, description="当前已安装版本")
    version: Optional[str] = Field(default=None, description="待安装的目标版本")
    frontend_version: Optional[str] = Field(default=None, description="发布包中的前端版本")
    current_auth_version: Optional[str] = Field(default=None, description="当前已安装的认证资源版本")
    auth_version: Optional[str] = Field(default=None, description="发布包中的站点认证资源版本")
    current_indexer_version: Optional[str] = Field(default=None, description="当前已安装的索引资源版本")
    indexer_version: Optional[str] = Field(default=None, description="发布包中的站点索引资源版本")
    release_name: Optional[str] = Field(default=None, description="版本发布名称")
    release_notes: Optional[str] = Field(default=None, description="版本更新说明")
    published_at: Optional[str] = Field(default=None, description="版本发布时间")
    checked_at: Optional[str] = Field(default=None, description="最近检查此类更新的时间")
    downloaded_bytes: int = Field(default=0, description="已下载的更新包字节数")
    total_bytes: int = Field(default=0, description="更新包总字节数")
    progress: int = Field(default=0, description="当前完成百分比")
    error: Optional[str] = Field(default=None, description="错误摘要")
    can_update: bool = Field(default=False, description="当前是否有可安装的更新")
    can_install: bool = Field(default=False, description="当前版本是否可以安装")


class SystemUpdateRequest(BaseModel):
    """
    单类或合并升级动作的请求体，all 在一次重启中安装全部已准备更新。
    """

    target: SystemUpdateTarget = Field(
        default="application",
        description="升级目标：application 为后端及配套前端，resources 为站点资源，all 为合并升级",
    )


class SystemUpdateStatus(BaseModel):
    """
    主程序与站点资源后台更新的聚合状态快照。
    """

    supports_batch_update: bool = Field(default=False, description="是否支持全部下载和一次重启合并安装")
    auto_update: bool = Field(default=False, description="是否启用主程序自动检查及升级提醒")
    auto_update_resource: bool = Field(default=True, description="是否启用站点资源自动检查及升级提醒")

    state: Literal[
        "idle",
        "available",
        "downloading",
        "ready",
        "installing",
        "failed",
    ] = Field(default="idle", description="当前业务状态")
    current_version: str = Field(description="当前已安装版本")
    version: Optional[str] = Field(default=None, description="待安装的目标版本")
    frontend_version: Optional[str] = Field(default=None, description="发布包中的前端版本")
    release_name: Optional[str] = Field(default=None, description="版本发布名称")
    release_notes: Optional[str] = Field(default=None, description="版本更新说明")
    published_at: Optional[str] = Field(default=None, description="版本发布时间")
    checked_at: Optional[str] = Field(default=None, description="最近检查更新的时间")
    downloaded_bytes: int = Field(default=0, description="已下载的更新包字节数")
    total_bytes: int = Field(default=0, description="更新包总字节数")
    progress: int = Field(default=0, description="当前完成百分比")
    error: Optional[str] = Field(default=None, description="错误摘要")
    can_update: bool = Field(default=False, description="当前是否有可安装的更新")
    can_install: bool = Field(default=False, description="当前版本是否可以安装")
    updates: list[SystemUpdateItemStatus] = Field(default_factory=list, description="可用的更新项列表")


class PluginMarketSyncData(BaseModel):
    """
    Wiki 插件市场仓库同步结果。
    """

    value: str = Field(description="同步后的插件市场配置值")
    repos: list[str] = Field(default_factory=list, description="插件仓库地址列表")
    wiki_repos: list[str] = Field(default_factory=list, description="维基提供的插件仓库列表")
    added_count: int = Field(default=0, description="本次新增的仓库数量")
    total_count: int = Field(default=0, description="同步后的仓库总数")
    source_url: str = Field(description="插件市场来源地址")


class RuleTestData(BaseModel):
    """
    过滤规则测试的输入、识别和匹配明细。
    """

    title: str = Field(description="标题")
    subtitle: Optional[str] = Field(default=None, description="副标题")
    rulegroup_name: str = Field(description="匹配的规则组名称")
    rulegroup: Optional[FilterRuleGroup] = Field(default=None, description="匹配的规则组信息")
    meta_info: MetaInfo = Field(description="识别元数据")
    media_info: Optional[MediaInfo] = Field(default=None, description="媒体信息")
    torrent_info: TorrentInfo = Field(description="种子信息")
    priority: Optional[int] = Field(default=None, description="优先级")
    matched: bool = Field(default=False, description="条件是否匹配")


class NetTestTarget(BaseModel):
    """
    前端可选择的网络测试目标。
    """

    id: str = Field(description="用于执行受控网络测试的稳定标识")
    name: str = Field(description="被测功能或服务的简短名称")
    address: str = Field(
        description="脱敏后的目标源站，不包含路径、查询参数或凭据"
    )
    icon: str = Field(description="被测服务的前端图标键")


class SystemModuleInfo(BaseModel):
    """
    当前配置下已启用系统模块摘要。
    """

    id: str = Field(description="记录标识")
    name: str = Field(description="显示名称")
    name_i18n: str = Field(description="多语言名称")
    name_key: str = Field(description="显示名称的本地化键")


class SystemModuleListData(BaseModel):
    """
    当前配置下已启用系统模块列表。
    """

    modules: list[SystemModuleInfo] = Field(default_factory=list, description="模块列表")


class SystemModuleSettingInfo(BaseModel):
    """
    可由用户统一开关的内置模块设置摘要。
    """

    id: str = Field(description="记录标识")
    name: str = Field(description="显示名称")
    name_i18n: str = Field(description="多语言名称")
    name_key: str = Field(description="显示名称的本地化键")
    description_i18n: str = Field(description="按语言组织的描述文本")
    description_key: str = Field(description="描述文本的本地化键")
    enabled: bool = Field(description="是否启用")


class SystemModuleSettingListData(BaseModel):
    """
    可由用户统一开关的内置模块设置列表。
    """

    modules: list[SystemModuleSettingInfo] = Field(default_factory=list, description="模块列表")


class SystemModuleCatalogInfo(BaseModel):
    """
    前端服务选择器可消费的宿主模块目录项。
    """

    id: str = Field(description="记录标识")
    name: str = Field(description="显示名称")
    name_i18n: str = Field(description="多语言名称")
    name_key: str = Field(description="显示名称的本地化键")
    description_i18n: str = Field(description="按语言组织的描述文本")
    description_key: str = Field(description="描述文本的本地化键")
    type: str = Field(description="类型标识")
    subtype: str = Field(description="模块能力的子类型")
    option_value: Optional[str] = Field(default=None, description="前端选项使用的实际值")
    enabled: bool = Field(default=True, description="是否启用")
    active: bool = Field(default=False, description="当前模块是否处于活动状态")


class SystemModuleCatalogListData(BaseModel):
    """
    宿主模块及其服务类型目录。
    """

    modules: list[SystemModuleCatalogInfo] = Field(default_factory=list, description="模块列表")


class DatabaseBackupArtifactData(BaseModel):
    """
    Web 管理端可见的受管数据库备份摘要。
    """

    # 受管文件名，不包含宿主目录
    name: str = Field(description="显示名称")
    # 创建制品的数据库类型
    db_type: str = Field(description="备份对应的数据库类型")
    # 从受管文件名解析出的创建时间
    created_at: _DateTime = Field(description="创建时间")
    # 备份文件字节数
    size: int = Field(description="文件大小，单位为字节")


class DatabaseBackupVerificationData(BaseModel):
    """
    受管数据库备份的脱敏校验结果。
    """

    # 是否通过当前数据库类型的内容校验
    valid: bool = Field(description="最近一次校验是否有效")
    # SQLite integrity_check 或 PostgreSQL 归档目录校验
    method: str = Field(description="处理方式")


class TransferDirectoryConf(BaseModel):
    """
    文件整理目录配置。
    """

    name: Optional[str] = Field(default=None, description="名称")
    priority: Optional[int] = Field(default=0, description="优先级")
    storage: Optional[str] = Field(default=None, description="存储")
    download_path: Optional[str] = Field(default=None, description="下载目录")
    media_type: Optional[str] = Field(default=None, description="适用媒体类型")
    media_category: Optional[str] = Field(default=None, description="适用媒体类别")
    media_category_id: Optional[str] = Field(default=None, description="适用媒体类别稳定 ID；media_category 在兼容期保存路径快照")
    download_type_folder: Optional[bool] = Field(default=False, description="下载类型子目录")
    download_category_folder: Optional[bool] = Field(default=False, description="下载类别子目录")
    monitor_type: Optional[str] = Field(default=None, description="监控方式 downloader/monitor，None为不监控")
    monitor_mode: Optional[str] = Field(default="fast", description="监控模式 fast / compatibility")
    transfer_type: Optional[str] = Field(default=None, description="整理方式 move/copy/link/softlink/reflink")
    overwrite_mode: Optional[str] = Field(default=None, description="文件覆盖模式 always/size/never/latest")
    library_path: Optional[str] = Field(default=None, description="整理到媒体库目录")
    library_storage: Optional[str] = Field(default=None, description="媒体库目录存储")
    renaming: Optional[bool] = Field(default=False, description="智能重命名")
    scraping: Optional[bool] = Field(default=False, description="刮削")
    notify: Optional[bool] = Field(default=True, description="是否发送通知")
    library_type_folder: Optional[bool] = Field(default=False, description="媒体库类型子目录")
    library_category_folder: Optional[bool] = Field(default=False, description="媒体库类别子目录")
