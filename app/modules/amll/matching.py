"""
AMLL 歌词来源的录音身份核对规则。
"""

import re
import unicodedata
from typing import Any

from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic


def normalize_text(value: str) -> str:
    """
    统一字形与大小写并移除标点，保留版本文字用于精确比较。

    :param value: 待比较的标题、艺术家或专辑文本
    :return: 规范化的匹配文本，保留下划线及文字、数字
    """
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", value).casefold())


def normalize_isrc(value: str | None) -> str:
    """
    去除空格和连字符并校验 ISRC 结构。

    :param value: 原始录音标识，可为空
    :return: 规范化的大写 ISRC；缺失或格式不完整时返回空字符串
    """
    normalized = re.sub(r"[\s-]", "", value or "").upper()
    return normalized if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", normalized) else ""


def text_values(payload: dict[str, Any], key: str) -> list[str]:
    """
    提取来源字段中有效的非空字符串列表。

    :param payload: AMLL 来源响应
    :param key: 需要读取的字段名称
    :return: 去除首尾空白后的字符串列表；字段不是列表时为空
    """
    values = payload.get(key)
    if not isinstance(values, list):
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def search_identity(music: MetaMusic | MusicInfo) -> tuple[str, list[str], str]:
    """
    提取单曲搜索身份，保留独立的版本标记。

    版本未出现在标题尾部括号内时补入标题，避免现场版等版本与原版混淆。

    :param music: 识别线索或已确认的单曲信息
    :return: 歌曲标题、艺术家列表和专辑名；艺术家缺失时尝试专辑艺术家
    """
    title = str(music.title or "").strip()
    version = str(music.version or "").strip()
    suffix = re.search(r"[\[(（]([^\])）]+)[\])）]\s*$", title)
    has_version = suffix is not None and normalize_text(suffix.group(1)) == normalize_text(version)
    if version and not has_version:
        title = f"{title} ({version})" if title else ""
    artist_values = list(music.artists or [])
    if not artist_values and music.album_artist:
        artist_values.append(music.album_artist)
    artists = [artist.strip() for artist in artist_values if artist.strip()]
    return title, artists, str(music.album or "").strip()


def match_score(music: MetaMusic | MusicInfo, payload: dict[str, Any]) -> int:
    """
    严格核对标题别名、艺术家集合与已知专辑。

    AMLL 时间戳描述歌词位置，不作为音轨总时长参与比较。

    :param music: 待查单曲信息
    :param payload: 来源候选身份字段
    :return: 不匹配为 0，标题和艺术家匹配为 90，同时匹配已知专辑为 95
    """
    title, artists, album = search_identity(music)
    if not title or not artists:
        return 0
    if normalize_text(title) not in {normalize_text(value) for value in text_values(payload, "musicNames")}:
        return 0
    expected_artists = {normalize_text(value) for value in artists}
    source_artists = {normalize_text(value) for value in text_values(payload, "artistNames")}
    if not expected_artists <= source_artists:
        return 0
    if album and normalize_text(album) not in {
        normalize_text(value) for value in text_values(payload, "albumNames")
    }:
        return 0
    return 95 if album else 90


def lyric_id(payload: dict[str, Any]) -> str:
    """
    校验可安全传入原生 API 的歌词标识。

    :param payload: 包含 id 的歌词来源记录
    :return: 正整数标识的字符串；布尔值、非整数或不在 1 至 2**53-1 范围时为空字符串
    """
    value = payload.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 53:
        return ""
    return str(value)
