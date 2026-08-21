from dataclasses import dataclass
from typing import Optional, Any

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
    # 是否为本族的默认调用目标
    default: Optional[bool] = False
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

    # 名称
    name: Optional[str] = None
    # 类型 telegram/wechat/feishu/vocechat/synologychat/slack/webpush/qqbot
    type: Optional[str] = None
    # 是否为本族的默认调用目标
    default: Optional[bool] = False
    # 配置
    config: Optional[dict] = Field(default_factory=dict)
    # 场景开关
    switchs: Optional[list] = Field(default_factory=list)
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
    存储实例配置

    一个存储类型可配置多份实例，``name`` 即实例名，与存储类型拼成存储令牌
    ``u115@work``。

    ``default`` 与 ``bare_token_target`` 回答的不是同一个问题：前者是本族的默认调用
    目标，即调用没有指定存储时用哪一个，整族至多一份，与下载器、媒体服务器、消息通知
    完全同规格；后者是不带实例名的裸令牌 ``u115`` 落到该类型的哪个实例上，每个存储
    类型各一份，是存量路径没有实例名时才需要的兼容指针，所有路径补全实例名后即可移除。
    """

    # 类型 local/alipan/u115/rclone/alist
    type: Optional[str] = None
    # 实例名，同一存储类型下唯一
    name: Optional[str] = None
    # 是否为本族的默认调用目标，即调用未指定存储时选中的那一份
    default: Optional[bool] = False
    # 是否承接本存储类型的裸令牌，兼容存量路径用，非默认语义
    bare_token_target: bool = False
    # 配置
    config: Optional[dict] = Field(default_factory=dict)


class AuthProviderConf(BaseModel):
    """
    登录认证入口配置

    一个登录入口类型可配置多份实例，``name`` 即实例名，也是登录页上那个按钮的名称：
    媒体服务器单点登录每台一份，第三方站点单点登录通常只有一份。

    ``identity_provider`` 是本入口写进第三方身份绑定表 ``provider`` 列的标识，留空时
    宿主按 ``类型@实例名`` 派生。它可显式填写，因为该列是绑定唯一键的一半，取值一变
    就是另一个身份命名空间——把它填成分身时代那个入口用过的旧标识，即可让存量绑定继续
    命中；反过来，两条配置填了同一个取值就是身份歧义，宿主拒绝据此产出登录入口。

    派生取值随实例名走，因此**改名等于换入口**，改完之后已绑定的用户按新标识查不到自己
    的绑定。要改名又保住绑定，先把改名前的派生取值填进本字段再改名。

    本族不设默认调用目标：族级默认回答的是「调用没指定用哪个」，而登录时用户点的是
    具体某个入口，不存在未指定这回事。
    """

    # 类型，即扩展声明的登录入口类型标识
    type: Optional[str] = None
    # 实例名，同一类型下唯一，即登录页上该入口的名称
    name: Optional[str] = None
    # 是否启用
    enabled: Optional[bool] = False
    # 身份绑定标识，留空时按 类型@实例名 派生
    identity_provider: Optional[str] = None
    # 配置
    config: Optional[dict] = Field(default_factory=dict)


class SystemEnvironmentUpdateData(BaseModel):
    """环境配置更新的成功项和失败项。"""

    success_updates: dict[str, tuple[Optional[bool], str]] = Field(default_factory=dict)
    failed_updates: dict[str, tuple[Optional[bool], str]] = Field(default_factory=dict)


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
