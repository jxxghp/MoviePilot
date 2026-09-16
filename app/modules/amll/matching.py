"""AMLL 歌词来源的录音身份核对规则。"""

import re
import unicodedata
from typing import Any

from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic


def normalize_text(value: str) -> str:
    """统一字形、大小写和标点，保留现场版等版本文字参与精确比较。"""
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", value).casefold())


def normalize_isrc(value: str | None) -> str:
    """接受带空格或连字符的 ISRC，拒绝不完整的录音标识。"""
    normalized = re.sub(r"[\s-]", "", value or "").upper()
    return normalized if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", normalized) else ""


def text_values(payload: dict[str, Any], key: str) -> list[str]:
    """只接收来源声明的字符串列表，避免异常 JSON 值参与身份匹配。"""
    values = payload.get(key)
    if not isinstance(values, list):
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def search_identity(music: MetaMusic | MusicInfo) -> tuple[str, list[str], str]:
    """提取当前单曲的完整标题、艺术家和专辑，不丢弃独立版本标记。"""
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
    """严格核对标题别名、艺术家及已有专辑，AMLL 时间戳不用于音轨时长比较。"""
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
    """校验原生 API 的正整数歌词 ID，用于下载与详情一致性检查。"""
    value = payload.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 53:
        return ""
    return str(value)
