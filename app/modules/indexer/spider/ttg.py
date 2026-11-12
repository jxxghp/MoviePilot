"""
TTG 详情与下载路径的稳定资源身份及短时票据规则。

沿用通用页面爬虫完成搜索；这里只解释专站路径，不持有配置、不执行 I/O。
"""

import re
from typing import Optional
from urllib.parse import urlsplit

_HOST = "totheglory.im"
_DETAIL_PATH = re.compile(r"/t/(\d+)/?")
_DOWNLOAD_PATH = re.compile(r"/dl/(\d+)/[^/]+/?")


class TTGDownloadAdapter:
    """
    解释 TTG 路径，末段 auth 票据不能作为资源身份。
    """

    @staticmethod
    def torrent_id_from_url(url: str) -> Optional[str]:
        """
        从本站详情页或签名下载路径提取稳定种子 ID。

        :param url: 旧缓存、RSS 或搜索候选中的公开地址
        :return: 稳定种子 ID；其他站点或不支持的路径返回 None
        """
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if host != _HOST and not host.endswith(f".{_HOST}"):
            return None
        match = _DETAIL_PATH.fullmatch(parsed.path) or _DOWNLOAD_PATH.fullmatch(parsed.path)
        return match[1] if match else None

    @staticmethod
    def is_expiring_download_url(url: str) -> bool:
        """
        仅把本站 /dl/{id}/{auth} 地址解释为短时签名。

        :param url: 原始或重定向后的下载地址
        :return: 是否为本站的签名下载路径；RSS 地址保持普通链接
        """
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (host == _HOST or host.endswith(f".{_HOST}")) and _DOWNLOAD_PATH.fullmatch(parsed.path) is not None
