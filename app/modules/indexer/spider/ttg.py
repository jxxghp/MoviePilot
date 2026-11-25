"""
TTG 详情与下载路径的稳定资源身份及短时票据规则。

沿用通用页面爬虫完成搜索；这里只解释专站路径，不持有配置、不执行 I/O。
"""

import base64
import binascii
import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

_HOST = "totheglory.im"
_DETAIL_PATH = re.compile(r"/t/(\d+)/?")
_DOWNLOAD_PATH = re.compile(r"/dl/(\d+)/[^/]+/?")
_RSS_DOWNLOAD_PATH = re.compile(r"/rssdd\.php")
_RSS_PAR_ID = re.compile(r"^vvv([1-9]\d*)(?:$|[^0-9])")


class TTGDownloadAdapter:
    """
    解释 TTG 路径，末段 auth 票据不能作为资源身份。
    """

    @staticmethod
    def _rss_torrent_id(query: str) -> Optional[str]:
        """
        从 TTG RSS 直链的 base64 ``par`` 参数提取稳定种子 ID。

        :param query: ``rssdd.php`` 请求的原始查询串
        :return: 解码出 ``vvv{id}`` 前缀时的种子 ID；非法或缺失时返回 None
        """
        values = parse_qs(query).get("par")
        if not values or len(values) != 1:
            return None
        raw = values[0].strip()
        if not raw:
            return None
        padded = raw + "=" * (-len(raw) % 4)
        try:
            decoded = base64.b64decode(padded.encode("ascii"), validate=True).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None
        match = _RSS_PAR_ID.match(decoded)
        return match[1] if match else None

    @staticmethod
    def torrent_id_from_url(url: str) -> Optional[str]:
        """
        从本站详情页、签名下载路径或 RSS 直链提取稳定种子 ID。

        :param url: 旧缓存、RSS 或搜索候选中的公开地址
        :return: 稳定种子 ID；其他站点或不支持的路径返回 None
        """
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if host != _HOST and not host.endswith(f".{_HOST}"):
            return None
        match = _DETAIL_PATH.fullmatch(parsed.path) or _DOWNLOAD_PATH.fullmatch(parsed.path)
        if match:
            return match[1]
        if _RSS_DOWNLOAD_PATH.fullmatch(parsed.path):
            return TTGDownloadAdapter._rss_torrent_id(parsed.query)
        return None

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
