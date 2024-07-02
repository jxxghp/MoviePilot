from typing import Optional

from pydantic import BaseModel


class MediaServerConf(BaseModel):
    """
    媒体服务器配置
    """
    # 名称
    name: Optional[str] = None
    # 类型 emby/jellyfin/plex
    type: Optional[str] = None
    # 配置
    config: Optional[dict] = {}
    # 是否启用
    enabled: Optional[bool] = False


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
    config: Optional[dict] = {}
    # 是否启用
    enabled: Optional[bool] = False


class NotificationConf(BaseModel):
    """
    通知配置
    """
    # 名称
    name: Optional[str] = None
    # 类型 telegram/wechat/vocechat/synologychat
    type: Optional[str] = None
    # 配置
    config: Optional[dict] = {}
    # 场景开关
    switchs: Optional[list] = []
    # 是否启用
    enabled: Optional[bool] = False
