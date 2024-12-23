from dataclasses import dataclass
from typing import Optional, Any

from pydantic import BaseModel, Field


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
    # 类型 emby/jellyfin/plex
    type: Optional[str] = None
    # 配置
    config: Optional[dict] = Field(default_factory=dict)
    # 是否启用
    enabled: Optional[bool] = False
    # 同步媒体体库列表
    sync_libraries: Optional[list] = Field(default_factory=list)


class DownloaderConf(BaseModel):
    """
    下载器配置
    """
    # 名称
    name: Optional[str] = None
    # 类型 qbittorrent/transmission
    type: Optional[str] = None
    # 是否默认
    default: Optional[bool] = False
    # 配置
    config: Optional[dict] = Field(default_factory=dict)
    # 是否启用
    enabled: Optional[bool] = False


class NotificationConf(BaseModel):
    """
    通知配置
    """
    # 名称
    name: Optional[str] = None
    # 类型 telegram/wechat/vocechat/synologychat/slack/webpush
    type: Optional[str] = None
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
    action: Optional[str] = 'all'


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
    monitor_mode: Optional[str] = 'fast'
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
