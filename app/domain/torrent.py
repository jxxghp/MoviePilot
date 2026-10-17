"""
种子身份与下载链接的纯领域判断规则。
"""

import base64
import binascii
import hashlib
import re
from typing import TYPE_CHECKING, Optional, Union
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from app.domain.context import TorrentInfo


def is_magnet_link(content: Union[str, bytes]) -> bool:
    """
    判断字符串或字节内容是否为磁力链接。
    """
    if not content:
        return False
    if isinstance(content, str):
        return content.startswith("magnet:")
    if isinstance(content, bytes):
        return content.startswith(b"magnet:")
    return False


def _bencoded_value_end(content: bytes, offset: int) -> int:
    """
    跳过一个完整 bencode 值，保留 info 原始字节以避免重编码改变哈希。
    """
    marker = content[offset:offset + 1]
    if marker == b"i":
        end = content.index(b"e", offset + 1)
        if not re.fullmatch(rb"0|-?[1-9][0-9]*", content[offset + 1:end]):
            raise ValueError("bencode 整数无效")
        return end + 1
    if marker in (b"d", b"l"):
        offset += 1
        keys: set[bytes] = set()
        while content[offset:offset + 1] != b"e":
            if marker == b"d":
                if not content[offset:offset + 1].isdigit():
                    raise ValueError("bencode 字典键无效")
                separator = content.index(b":", offset)
                key_end = _bencoded_value_end(content, offset)
                key = content[separator + 1:key_end]
                if key in keys:
                    raise ValueError("bencode 字典键重复")
                keys.add(key)
                offset = key_end
            offset = _bencoded_value_end(content, offset)
        return offset + 1
    if marker.isdigit():
        separator = content.index(b":", offset)
        if not re.fullmatch(rb"0|[1-9][0-9]*", content[offset:separator]):
            raise ValueError("bencode 字符串长度无效")
        length = int(content[offset:separator])
        end = separator + 1 + length
        if end > len(content):
            raise ValueError("bencode 字符串不完整")
        return end
    raise ValueError("bencode 值无效")


def _bencoded_dictionary(content: bytes) -> dict[bytes, bytes]:
    """
    提取字典直属键值的原始切片，不解码二进制文件树或 pieces root。
    """
    if not content.startswith(b"d") or _bencoded_value_end(content, 0) != len(content):
        raise ValueError("bencode 字典无效")
    result: dict[bytes, bytes] = {}
    offset = 1
    while content[offset:offset + 1] != b"e":
        if not content[offset:offset + 1].isdigit():
            raise ValueError("bencode 字典键无效")
        separator = content.index(b":", offset)
        key_end = _bencoded_value_end(content, offset)
        key = content[separator + 1:key_end]
        value_end = _bencoded_value_end(content, key_end)
        if key in result:
            raise ValueError("bencode 字典键重复")
        result[key] = content[key_end:value_end]
        offset = value_end
    return result


def download_hashes(content: Union[str, bytes]) -> frozenset[str]:
    """
    读取实际种子或磁力的 v1/v2 身份；v2 同时接受下载器使用的 40 位截断哈希。

    哈希只由原始 info 字节或明确的磁力标识构造，名称和大小不构成身份。
    解析失败返回空集合，调用方不得据此接管其它已有任务。
    """
    try:
        hashes: set[str] = set()
        if is_magnet_link(content):
            url = content.decode("utf-8") if isinstance(content, bytes) else content
            for value in parse_qs(urlsplit(url).query).get("xt", []):
                if match := re.fullmatch(r"urn:btih:([a-fA-F0-9]{40})", value):
                    hashes.add(match.group(1).lower())
                elif match := re.fullmatch(r"urn:btih:([a-zA-Z2-7]{32})", value):
                    hashes.add(base64.b32decode(match.group(1).upper()).hex())
                elif match := re.fullmatch(r"urn:btmh:1220([a-fA-F0-9]{64})", value):
                    digest = match.group(1).lower()
                    hashes.update((digest, digest[:40]))
        elif isinstance(content, bytes):
            info = _bencoded_dictionary(content).get(b"info", b"")
            fields = _bencoded_dictionary(info)
            if b"pieces" in fields:
                hashes.add(hashlib.sha1(info).hexdigest())
            if fields.get(b"meta version") == b"i2e":
                digest = hashlib.sha256(info).hexdigest()
                hashes.update((digest, digest[:40]))
        return frozenset(hashes)
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        return frozenset()


def resource_identity(torrent: "TorrentInfo") -> Optional[tuple[str, str]]:
    """
    按站点和稳定种子标识匹配资源，不把临时签名或标题当成种子 ID。
    """
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
        # Gazelle 详情页的 id 是分组 ID，torrentid 才对应可下载的具体资源。
        for name in ("torrentid", "torrent_id", "tid", "id", "hash"):
            values = params.get(name)
            if values and len(values) == 1:
                prefix = "hash" if name == "hash" else "id"
                return site or parsed.netloc, f"{prefix}={values[0]}"
        # 馒头详情页把稳定种子 ID 放在路径中，换票请求参数不参与资源身份。
        match = re.fullmatch(r"/detail/(\d+)/?", parsed.path)
        if match:
            return site or parsed.netloc, f"id={match[1]}"
    return None


def is_expiring_download_url(url: Optional[str]) -> bool:
    """
    识别已知的时间戳加签名下载链接；不推断签名的具体有效期。
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        params = parse_qs(urlsplit(url).query)
    except ValueError:
        return False
    return bool(params.get("t") and params.get("sign"))


def rate_limit_cooldown(message: Optional[str]) -> Optional[int]:
    """
    识别种子下载限流语义并返回冷却秒数；未知错误返回空。

    每日下载配额采用完整 24 小时以覆盖站点时区；短期限流沿用至少一小时的
    订阅冷却，提示更长等待时按提示延长，不在下载调用内立即重试。
    """
    text = str(message or "").lower()
    daily_quota = (
        re.search(r"[当當]天|每天|每日|今日|daily", text)
        and re.search(r"下载|下載|download", text)
        and re.search(r"最多|上限|限[额額]|次[数數]|limit|quota", text)
    )
    if daily_quota:
        return 86400
    keywords = ("限流", "流控", "请求太多", "请求过多", "请求频繁", "请求过于频繁",
                "請求太多", "請求過多", "請求頻繁", "請求過於頻繁", "too many requests", "rate limit")
    if not any(keyword in text for keyword in keywords):
        return None
    wait = re.search(r"(\d+)\s*(秒|分[钟鐘]?|小[时時]|seconds?|minutes?|hours?)", text)
    if not wait:
        return 3600
    unit = wait.group(2)
    multiplier = 3600 if unit.startswith(("小", "hour")) else (
        60 if unit.startswith(("分", "minute")) else 1
    )
    return max(3600, int(wait.group(1)) * multiplier)
