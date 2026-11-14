"""
种子身份、下载链接与剧集文件选择的纯领域判断规则。
"""

import base64
import binascii
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union
from urllib.parse import parse_qs, urlsplit

from app.domain.metainfo import MetaInfo

if TYPE_CHECKING:
    from app.domain.context import Context, TorrentInfo


@dataclass(frozen=True)
class EpisodeSelection:
    """
    下载目标季与识别阶段已经确认的季集对应关系，不推算未确认的集数。

    :param season: 目标季号，0 表示特别篇
    :param source_season: 种子标题识别修正前的季号；None 表示未标季
    :param episode_map: 标题原集号与目标集号的对应项；空元组表示没有额外换算依据
    """

    season: int
    source_season: Optional[int] = None
    episode_map: tuple[tuple[int, int], ...] = ()


def episode_selection_for_context(context: "Context") -> Optional[EpisodeSelection]:
    """
    从单季候选冻结季集依据，只有等长的明确集号列表才建立逐集对应关系。

    不根据目标季号或单个差值外推整包偏移。来源识别词由文件解析自行应用，
    此处保留应用来源临时词之前的标题坐标，避免标题专用词无法命中文件名。
    """
    meta, torrent = context.meta_info, context.torrent_info
    if meta is None or len(meta.season_list) != 1:
        return None
    season = meta.season_list[0]
    original = MetaInfo(torrent.title or "", torrent.description) if torrent else None
    source_season = original.begin_season if original else None
    source_episodes = original.episode_list if original else []
    target_episodes = meta.episode_list
    mapping: tuple[tuple[int, int], ...] = ()
    if original and len(original.season_list) == 1 and source_episodes and len(source_episodes) == len(target_episodes) and (
            source_season != season or source_episodes != target_episodes
    ):
        mapping = tuple(zip(source_episodes, target_episodes))
    return EpisodeSelection(season, source_season, mapping)


def _file_episode_coordinates(
        name: str, custom_words: Optional[list[str]], selection: Optional[EpisodeSelection],
) -> tuple[Optional[set[int]], bool]:
    """
    解析文件并应用明确映射；None 表示季集冲突，布尔值区分无集号附件。
    """
    original = MetaInfo(Path(name).name)
    meta = MetaInfo(Path(name).name, custom_words=custom_words) if custom_words else original
    episodes = set(meta.episode_list)
    numbered = bool(episodes)
    if selection is None:
        return episodes, numbered
    corrected = (meta.begin_season, meta.episode_list) != (original.begin_season, original.episode_list)
    if not corrected and selection.episode_map and original.begin_season in (None, selection.source_season):
        mapping = dict(selection.episode_map)
        mapped = {mapping[episode] for episode in episodes if episode in mapping}
        return mapped or None, numbered
    if meta.begin_season is not None and meta.season_list != [selection.season]:
        return None, numbered
    return episodes, numbered


def torrent_file_episodes(
        name: str, custom_words: Optional[list[str]] = None,
        episode_selection: Optional[EpisodeSelection] = None,
) -> set[int]:
    """
    返回文件在目标季中的已确认集号，供下载候选检查复用；冲突不贡献集数。
    """
    episodes, _numbered = _file_episode_coordinates(name, custom_words, episode_selection)
    return episodes or set()


def select_torrent_files(
        files: Iterable[tuple[int, str]], episodes: set[int], media_extensions: Iterable[str],
        custom_words: Optional[str] = None, episode_selection: Optional[EpisodeSelection] = None,
        *, allow_single_fallback: bool = True,
) -> tuple[list[int], list[int], set[int]]:
    """
    按目标季和已确认映射选择真实文件，下载器与历史结算共用相同规则。

    :param files: 下载器文件索引和路径；仅解析文件名，父目录不作为季集依据
    :param episodes: 必须由视频完整覆盖的目标集号
    :param media_extensions: 视频后缀；历史调用方可传入其媒体后缀集合
    :param custom_words: 来源临时识别词，先于明确映射应用，避免重复换算
    :param episode_selection: 冻结的目标季与映射；None 保留旧调用方的集号选集行为
    :param allow_single_fallback: 历史去重后不能用剩余唯一文件冒充原始单文件种子
    :return: 需要下载的索引、不下载的索引和已选视频覆盖的目标集号
    """
    extensions = {extension.lower() for extension in media_extensions}
    words = custom_words.splitlines() if custom_words else None
    entries: list[tuple[int, set[int], bool]] = []
    excluded: set[int] = set()
    for file_id, name in files:
        is_video = Path(name).suffix.lower() in extensions
        mapped, numbered = _file_episode_coordinates(name, words, episode_selection)
        if mapped is None and (is_video or numbered):
            excluded.add(file_id)
        entries.append((file_id, mapped or set(), is_video))
    return select_episode_files(entries, episodes, excluded=excluded, allow_single_fallback=allow_single_fallback)


def select_episode_files(
    files: Iterable[tuple[int, set[int], bool]], episodes: set[int],
    *, excluded: Optional[set[int]] = None, allow_single_fallback: bool = True,
) -> tuple[list[int], list[int], set[int]]:
    """
    按视频覆盖集数选择文件，保留无集号的非视频附属文件。

    多集文件与目标集有交集即选中，允许一并下载同文件中的其它集。仅请求一集且
    只有一个单集或无集号视频时，沿用识别修正后的唯一视频兜底；附属文件不证明视频覆盖。

    :param files: 文件索引、已按来源识别词解析的集数、是否为视频组成的条目
    :param episodes: 本次需要完整覆盖的目标集数
    :param excluded: 季集依据冲突的文件，不能参与匹配或唯一视频兜底
    :param allow_single_fallback: 是否允许唯一视频兜底
    :return: 需要下载的索引、不下载的索引、已选视频覆盖的集数；输入条目不会被修改
    """
    wanted: list[int] = []
    unwanted: list[int] = []
    matched: set[int] = set()
    media_files: list[tuple[int, set[int]]] = []
    for file_id, file_episodes, is_video in files:
        if is_video:
            media_files.append((file_id, file_episodes))
        if excluded and file_id in excluded:
            unwanted.append(file_id)
        elif file_episodes.intersection(episodes):
            wanted.append(file_id)
            if is_video:
                matched.update(file_episodes)
        elif not file_episodes and not is_video:
            wanted.append(file_id)
        else:
            unwanted.append(file_id)
    if allow_single_fallback and not episodes.issubset(matched) and len(episodes) == 1 and len(media_files) == 1:
        file_id, file_episodes = media_files[0]
        # 无交集的多集视频不能用唯一视频规则冒充目标单集。
        if len(file_episodes) <= 1 and (not excluded or file_id not in excluded):
            unwanted.remove(file_id)
            wanted.append(file_id)
            matched.update(episodes)
    return wanted, unwanted, matched


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
    按显式字段和公开查询参数匹配资源；专站路径由 Module 解释。

    :param torrent: 候选的站点、显式身份和公开定位地址
    :return: 站点及明确资源标识；标题、票据或含糊的地址不产生身份
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
    return None


def is_expiring_download_url(url: Optional[str]) -> bool:
    """
    识别已知的短时签名下载链接；不推断签名的具体有效期。

    只识别查询参数中的时间戳加签名（t、sign）；专站路径票据由 Module 解释。

    :param url: 下载地址或间接配方
    :return: 是否带有完整的通用时间戳和签名参数
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
    except ValueError:
        return False
    return bool(params.get("t") and params.get("sign"))


def rate_limit_cooldown(message: Optional[str]) -> Optional[int]:
    """
    识别种子下载限流语义并返回冷却秒数；未知错误返回空。

    每日下载配额采用完整 24 小时以覆盖站点时区；明确的频控秒数按原值冷却。
    其他短期限流沿用至少一小时的订阅冷却，不在下载调用内立即重试。
    """
    text = str(message or "").lower()
    daily_quota = (
        re.search(r"[当當]天|每天|每日|今日|daily", text)
        and re.search(r"下载|下載|download", text)
        and re.search(r"最多|上限|限[额額]|次[数數]|limit|quota", text)
    )
    if daily_quota:
        return 86400
    frequency_limit = re.search(r"request frequency limit,?\s*please try in\s+(\d+)\s+seconds?", text)
    if frequency_limit:
        return max(1, int(frequency_limit.group(1)))
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
