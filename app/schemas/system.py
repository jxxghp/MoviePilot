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
