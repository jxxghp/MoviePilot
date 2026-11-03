"""
永久黑名单的纯匹配规则、严格资源身份与安全历史投影。
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Protocol
from urllib.parse import SplitResult, parse_qs, urlsplit

from pydantic import ValidationError

from app.domain.context import TorrentInfo
from app.domain.torrent import download_hashes
from app.schemas.blacklist import BlacklistConfig, BlacklistEntry, BlacklistIdentity


class BlacklistConfigurationError(ValueError):
    """
    表示持久化黑名单损坏，调用方应显示错误并停止下载。
    """


class BlacklistIdentityError(ValueError):
    """
    表示实际提交内容含相互冲突的资源哈希，必须在调用下载器前拒绝。
    """


class BlacklistHistory(Protocol):
    """
    历史一键屏蔽只读取安全备注与下载哈希。

    :ivar note: 已脱离持久化会话的历史备注
    :ivar download_hash: 下载器保存的任务哈希，旧记录可能为空或无效
    """

    @property
    def note(self) -> object:
        """
        返回只读备注，调用方不能通过端口修改历史。
        """
        ...

    @property
    def download_hash(self) -> str | None:
        """
        返回只读下载器任务哈希，未知时为空。
        """
        ...


def parse_blacklist_config(value: object) -> BlacklistConfig:
    """
    仅缺失配置视为空；已存在的对象必须包含版本与条目集合，损坏时显式拒绝下载。
    """
    if value is None:
        return BlacklistConfig()
    if not isinstance(value, Mapping) or "version" not in value or "entries" not in value:
        raise BlacklistConfigurationError("种子黑名单配置无效，请管理员修复后重试")
    try:
        return BlacklistConfig.model_validate(value)
    except ValidationError as exc:
        raise BlacklistConfigurationError("种子黑名单配置无效，请管理员修复后重试") from exc


def _single_resource_hashes(hashes: frozenset[str]) -> bool:
    """
    单一资源最多包含一个 v1、一个 v2 和后者的截断别名，不截断或抽样真实哈希。
    """
    v2 = {value for value in hashes if len(value) == 64}
    v1 = {value for value in hashes if len(value) == 40} - {value[:40] for value in v2}
    return len(v2) <= 1 and len(v1) <= 1


def validated_content_hashes(content: str | bytes) -> frozenset[str]:
    """
    完整解析实际提交内容；多个资源身份不能通过截断规避黑名单或溢出历史结构。
    """
    hashes = download_hashes(content)
    if not _single_resource_hashes(hashes):
        raise BlacklistIdentityError("种子内容包含多个资源哈希，无法安全提交")
    return hashes


def normalize_hash_aliases(value: str | None) -> frozenset[str]:
    """
    仅接受标准 BitTorrent 哈希或磁力标识，不把任意任务 ID 当作哈希。
    """
    if not isinstance(value, str) or len(value) > 4096:
        return frozenset()
    value = value.strip()
    if re.fullmatch(r"[a-fA-F0-9]{64}", value):
        value = f"magnet:?xt=urn:btmh:1220{value}"
    elif re.fullmatch(r"[a-fA-F0-9]{40}|[a-zA-Z2-7]{32}", value):
        value = f"magnet:?xt=urn:btih:{value}"
    hashes = download_hashes(value)
    return hashes if _single_resource_hashes(hashes) else frozenset()


def _resource_urls(torrent: TorrentInfo) -> tuple[SplitResult, ...]:
    """
    限定 URL 长度并忽略不合法地址；不保留用户名、密码或查询原文。
    """
    result = []
    for url in (torrent.page_url, torrent.enclosure):
        if not isinstance(url, str) or len(url) > 16384:
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme in ("http", "https") and parsed.hostname:
                _ = parsed.port
                result.append(parsed)
        except ValueError:
            continue
    return tuple(result)


def _query_id(parsed: SplitResult, name: str) -> str | None:
    """
    仅接受唯一且有界的明确种子标识，重复参数不构造身份。
    """
    values = parse_qs(parsed.query).get(name, [])
    if len(values) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", values[0]):
        return values[0]
    return None


def _known_resource_id(parsed: SplitResult) -> str | None:
    """
    只接受已知的具体种子页面或下载路径，Gazelle 分组 ID 不作为种子 ID。
    """
    path = parsed.path.rstrip("/").lower()
    if path.endswith(("/download.php", "/details.php")):
        return _query_id(parsed, "id")
    if path.endswith("/torrents.php") and parse_qs(parsed.query).get("action") == ["download"]:
        return _query_id(parsed, "id")
    if match := re.fullmatch(r"/detail/(\d+)", path):
        return match[1]
    host = parsed.hostname or ""
    if host == "totheglory.im" or host.endswith(".totheglory.im"):
        if match := re.fullmatch(r"/(?:t/(\d+)|dl/(\d+)/[^/]+)", path):
            return match[1] or match[2]
    return None


def _resource_digest(torrent: TorrentInfo) -> tuple[str, ...]:
    """
    按显式字段、跨 URL 明确参数、已知具体资源路径的顺序构造 SHA256。
    """
    urls = _resource_urls(torrent)
    resource_id = str(torrent.torrent_id or "")
    origin = urls[0] if urls else None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", resource_id):
        resource_id = ""
    for name in ("torrent_id", "torrentid"):
        if resource_id:
            break
        for parsed in urls:
            if value := _query_id(parsed, name):
                resource_id, origin = value, parsed
                break
    if not resource_id:
        for parsed in urls:
            if value := _known_resource_id(parsed):
                resource_id, origin = value, parsed
                break
    if isinstance(torrent.site, int) and not isinstance(torrent.site, bool) and torrent.site > 0:
        site = f"site:{torrent.site}"
    elif origin:
        site = f"host:{origin.hostname}:{origin.port or (443 if origin.scheme == 'https' else 80)}"
    else:
        site = ""
    if not site or not resource_id:
        return ()
    raw = json.dumps([site, resource_id], separators=(",", ":")).encode()
    return (hashlib.sha256(raw).hexdigest(),)


def torrent_blacklist_identity(
    torrent: TorrentInfo,
    content: str | bytes = b"",
    download_hash: str | None = None,
) -> BlacklistIdentity:
    """
    从资源元数据和实际内容提取安全身份，不保存 URL 或站点凭据。
    """
    hashes = validated_content_hashes(content)
    for optional_hash in (torrent.info_hash, download_hash):
        aliases = normalize_hash_aliases(optional_hash)
        # 实际内容优先；可选来源只能补全已知身份，不能把不相关资源并入成功历史。
        combined = hashes | aliases
        if (not hashes or hashes.intersection(aliases)) and _single_resource_hashes(combined):
            hashes = combined
    site = torrent.site
    uploader = (torrent.uploader or "").strip().casefold()
    return BlacklistIdentity(
        digests=_resource_digest(torrent),
        hashes=tuple(sorted(hashes)),
        site_id=site if isinstance(site, int) and not isinstance(site, bool) and site > 0 else None,
        uploader=uploader[:128] or None,
    )


def history_blacklist_identity(history: BlacklistHistory) -> BlacklistIdentity:
    """
    新历史恢复安全快照；旧历史仅用合法下载哈希，不以标题扩大屏蔽范围。
    """
    note = history.note
    if isinstance(note, Mapping) and "torrent_blacklist" in note:
        try:
            identity = BlacklistIdentity.model_validate(note["torrent_blacklist"])
        except ValidationError as exc:
            raise ValueError("历史资源身份无效，请添加手动黑名单规则") from exc
    else:
        identity = BlacklistIdentity(hashes=tuple(sorted(normalize_hash_aliases(history.download_hash))))
    if not (identity.digests or identity.hashes):
        raise ValueError("历史记录缺少安全资源身份，请添加手动黑名单规则")
    return identity


def blacklist_matches(
    entries: Iterable[BlacklistEntry],
    torrent: TorrentInfo,
    content: str | bytes = b"",
) -> bool:
    """
    单条手动规则内部按与、多条规则按或；资源身份摘要或哈希任一命中即拒绝。
    """
    identity = None
    for entry in entries:
        if entry.rule is not None:
            rule = entry.rule
            if rule.site_id is not None and rule.site_id != torrent.site:
                continue
            if rule.uploader is not None and rule.uploader != (torrent.uploader or "").strip().casefold():
                continue
            if rule.keyword is not None and not any(
                rule.keyword in (value or "").casefold() for value in (torrent.title, torrent.description)
            ):
                continue
            return True
        if entry.identity is not None:
            identity = identity or torrent_blacklist_identity(torrent, content)
            if set(identity.digests).intersection(entry.identity.digests) or set(identity.hashes).intersection(
                entry.identity.hashes
            ):
                return True
    return False
