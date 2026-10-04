"""种子身份与下载链接的纯领域判断规则。"""

from typing import TYPE_CHECKING, Optional, Union
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from app.domain.context import TorrentInfo


def is_magnet_link(content: Union[str, bytes]) -> bool:
    """判断字符串或字节内容是否为磁力链接。"""
    if not content:
        return False
    if isinstance(content, str):
        return content.startswith("magnet:")
    if isinstance(content, bytes):
        return content.startswith(b"magnet:")
    return False


def resource_identity(torrent: "TorrentInfo") -> Optional[tuple[str, str]]:
    """按站点和稳定种子标识匹配资源，不把临时签名或标题当成种子 ID。"""
    site = str(torrent.site or torrent.site_name or "")
    for name, prefix in (("torrent_id", "id"), ("info_hash", "hash")):
        value = getattr(torrent, name, None)
        if site and isinstance(value, (str, int)) and value:
            return site, f"{prefix}={value}"
    for url in (torrent.page_url, torrent.enclosure):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        params = parse_qs(parsed.query)
        for name in ("id", "torrentid", "torrent_id", "tid", "hash"):
            values = params.get(name)
            if values and len(values) == 1:
                prefix = "hash" if name == "hash" else "id"
                return site or parsed.netloc, f"{prefix}={values[0]}"
    return None


def is_expiring_download_url(url: Optional[str]) -> bool:
    """识别已知的时间戳加签名下载链接；不推断签名的具体有效期。"""
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        params = parse_qs(urlsplit(url).query)
    except ValueError:
        return False
    return bool(params.get("t") and params.get("sign"))
