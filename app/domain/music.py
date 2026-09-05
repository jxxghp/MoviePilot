"""音乐名称、版本与站点候选匹配的纯业务规则。"""

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Optional
from unicodedata import combining, normalize

from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.foundation.text import convert as zhconv_convert
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaType

_EDITION = re.compile(
    r"\s*[\[(（【](?:[^\])）】]*\b(?:deluxe|expanded|special|limited|anniversary|remaster(?:ed)?)\b"
    r"[^\])）】]*|[^\])）】]*(?:豪华版|典藏版|纪念版|重制版|周年版)[^\])）】]*)[\])）】]",
    re.IGNORECASE,
)
_VERSIONS = {
    "live": r"\blive\b|现场|現場|演唱会|演唱會",
    "remix": r"\bremix(?:ed)?\b|混音",
    "instrumental": r"\binstrumental\b|\bkaraoke\b|伴奏|纯音乐|純音樂",
    "acoustic": r"\bacoustic\b|\bunplugged\b|不插电|不插電",
    "demo": r"\bdemo\b",
}
_VERSION_SUFFIX = re.compile(
    r"\s*[\[(（【][^\])）】]*(?:\blive\b|\bremix\b|\binstrumental\b|\bacoustic\b|"
    r"\bunplugged\b|\bdemo\b|\bkaraoke\b|现场|現場|混音|伴奏|不插电|不插電)[^\])）】]*[\])）】]",
    re.IGNORECASE,
)
_BARE_VERSION_SUFFIX = re.compile(
    r"\s+[-/]\s+(?:live\b|remix\b|instrumental\b|acoustic\b|unplugged\b|demo\b|karaoke\b|"
    r"现场|現場|混音|伴奏|不插电|不插電).*$", re.IGNORECASE,
)
_TITLE_LABEL = re.compile(r"^(?:专辑(?:名|名称)?|專輯(?:名|名稱)?|曲名|歌曲|album|title)\s*[:：]\s*", re.I)
_COLLECTIVE_ARTISTS = ("Various Artists", "Various", "VA", "群星", "众艺人", "眾藝人")


@dataclass(frozen=True, slots=True)
class MusicMatch:
    """区分可自动采用的精确命中、仅可人工确认的候选和无关资源。"""

    status: Literal["exact", "candidate", "album", "rejected"]
    reason: str


def unique_music_texts(values: Iterable[Optional[str]]) -> list[str]:
    """保留原始文字和顺序，仅合并空白及大小写相同的名称。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result


def music_text_key(value: Optional[str]) -> str:
    """统一繁简、大小写、全半角和拉丁变音符，忽略名称排版符号。"""
    text = normalize("NFKD", str(value or "")).casefold()
    return str(zhconv_convert("".join(char for char in text if char.isalnum() and not combining(char)), "zh-hans"))


def music_titles(music: MusicInfo, *, album: bool = False) -> list[str]:
    """返回同一作品的可信名称，单曲绝不消费兼容 names 中的专辑名。"""
    if album or music.music_type == MUSIC_ENTITY_ALBUM:
        return unique_music_texts([
            music.album or (music.title if music.music_type == MUSIC_ENTITY_ALBUM else None),
            *(music.album_aliases or []),
            *((music.title_aliases or []) if music.music_type == MUSIC_ENTITY_ALBUM else ()),
            *((music.names or []) if music.music_type == MUSIC_ENTITY_ALBUM else ()),
        ])
    return unique_music_texts([music.title, *(music.title_aliases or [])])


def music_artists(music: MusicInfo) -> list[str]:
    """合并实体艺术家和来源别名，仅为合辑署名扩展通用缩写。"""
    album_artist = music.album_artist if music.music_type == MUSIC_ENTITY_ALBUM or not music.artists else None
    artists = unique_music_texts([album_artist, *(music.artists or []), *(music.artist_aliases or [])])
    collective_keys = {music_text_key(item) for item in _COLLECTIVE_ARTISTS}
    if music.music_type != MUSIC_ENTITY_ALBUM and any(music_text_key(artist) not in collective_keys for artist in music.artists):
        artists = [artist for artist in artists if music_text_key(artist) not in collective_keys]
    if any(music_text_key(artist) in collective_keys for artist in artists):
        artists = unique_music_texts([*artists, *_COLLECTIVE_ARTISTS])
    return artists


def music_base_title(value: Optional[str]) -> str:
    """仅剥离已知发行版本后缀，保留未知括号和属于作品本身的文字。"""
    text = _VERSION_SUFFIX.sub("", _EDITION.sub("", str(value or "")))
    return _BARE_VERSION_SUFFIX.sub("", text).strip()


def _contains_artist(text: str, artist: str) -> bool:
    """匹配完整署名，避免短拉丁艺名命中另一个人名的子串。"""
    normalized = str(zhconv_convert(normalize("NFKD", text).casefold(), "zh-hans"))
    normalized = "".join(char for char in normalized if not combining(char))
    key = music_text_key(artist)
    if not key:
        return False
    pattern = r"[\W_]*".join(re.escape(char) for char in key)
    return bool(re.search(r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])", normalized))


def _resource_names(primary: MetaMusic, artists: list[str], *, album: bool = False,
                    album_suffixes: Optional[list[str]] = None) -> list[str]:
    """复用音乐命名解析器提取作品片段，去掉已确认的首尾艺术家署名。"""
    names: list[str] = []
    artist_keys = [music_text_key(item) for item in artists if item]
    suffix_keys = {music_text_key(item) for item in album_suffixes or []}
    for value in (primary.title, primary.album if album else None):
        if not value:
            continue
        parts = re.split(r"\s+[-|/]\s+|[;；]", value)
        # 只有可核验为所属专辑的尾段才允许剥离，未知连字符后缀仍属于作品本身。
        variants = [value]
        if len(parts) > 1 and all(music_text_key(part) in suffix_keys for part in parts[1:]):
            variants.append(parts[0])
        for part in variants:
            key = music_text_key(music_base_title(_TITLE_LABEL.sub("", part.strip())))
            if not key:
                continue
            names.append(key)
            for artist in artist_keys:
                if key.startswith(artist) and key != artist:
                    names.append(key[len(artist):])
                if key.endswith(artist) and key != artist:
                    names.append(key[:-len(artist)])
    return names


def _version_markers(text: str) -> set[str]:
    """识别会改变录音身份的版本标记，普通发行后缀单独处理。"""
    return {name for name, pattern in _VERSIONS.items() if re.search(pattern, text, re.I)}


def match_music_resource(
    music: MusicInfo,
    title: str,
    description: Optional[str] = None,
    category: Optional[str] = MediaType.MUSIC.value,
    *,
    meta: Optional[MetaMusic] = None,
) -> MusicMatch:
    """以作品名称为基础验证艺术家、分类和版本，并保留可供人工确认的关联候选。"""
    if category not in (None, "", MediaType.UNKNOWN, MediaType.UNKNOWN.value, MediaType.MUSIC, MediaType.MUSIC.value):
        return MusicMatch("rejected", "category_mismatch")
    description = description or ""
    resource = meta or MetaMusic.parse_resource(title, description)
    artists = music_artists(music)
    albums = music_titles(music, album=True)
    names = _resource_names(resource, artists, album=music.music_type == MUSIC_ENTITY_ALBUM,
                            album_suffixes=albums if music.music_type != MUSIC_ENTITY_ALBUM else None)
    titles = music_titles(music)
    title_matched = any(music_text_key(music_base_title(item)) in names for item in titles)
    content = f"{title} {description}"
    resource_artist_keys = {music_text_key(artist) for artist in resource.artists}
    if len(resource.artists) > 1 and any(any(separator in artist for separator in ("/", "&", ",")) for artist in artists):
        # 带分隔符的完整艺名可能被解析成多个片段，保留整段署名参与比较，不拼接无分隔符艺名。
        resource_artist_keys.add(music_text_key(resource.artist))
    artist_matched = bool(resource_artist_keys & {music_text_key(artist) for artist in artists}) if resource.artists \
        else any(_contains_artist(content, artist) for artist in artists)
    if not title_matched:
        if music.music_type != MUSIC_ENTITY_ALBUM and artist_matched and any(
            music_text_key(music_base_title(item)) in names
            for item in music_titles(music, album=True)
        ):
            return MusicMatch("album", "related_album")
        return MusicMatch("rejected", "title_mismatch")
    if not artists or (music.music_type != MUSIC_ENTITY_ALBUM and not music.artists):
        return MusicMatch("candidate", "target_artist_missing")
    if not artist_matched:
        return MusicMatch("candidate", "artist_unverified")
    if category not in (MediaType.MUSIC, MediaType.MUSIC.value):
        return MusicMatch("candidate", "category_unknown")
    if music.music_type == MUSIC_ENTITY_ALBUM:
        # 所属专辑只说明单曲归属，不能代替资源主标题证明整专范围。
        primary_names = _resource_names(resource, artists)
        if resource.track_number or (resource.album and not any(
            music_text_key(music_base_title(item)) in primary_names for item in titles
        )):
            return MusicMatch("candidate", "partial_album")
        if music.year and resource.year and str(music.year) != str(resource.year):
            return MusicMatch("candidate", "year_mismatch")
    target_title = music.album or music.title if music.music_type == MUSIC_ENTITY_ALBUM else music.title
    expected_version = _version_markers(f"{target_title or ''} {music.version or ''}")
    if expected_version != _version_markers(f"{resource.title or ''} {resource.version or ''}"):
        return MusicMatch("candidate", "version_mismatch")
    if _EDITION.search(music.title or "") and not any(music_text_key(item) in music_text_key(content) for item in titles):
        return MusicMatch("candidate", "edition_unverified")
    return MusicMatch("exact", "matched")
