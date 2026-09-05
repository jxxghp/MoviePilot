"""音乐名称、版本与站点候选匹配的纯业务规则。"""

import re
from dataclasses import dataclass
from datetime import date
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
_VERSION_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_VERSION_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:[-./]|年)\s*(\d{1,2})(?:[-./]|月)\s*(\d{1,2})日?(?!\d)")
_ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", re.IGNORECASE | re.ASCII)


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


def music_artist_matches(music: MusicInfo, parsed_artists: Iterable[str]) -> bool:
    """使用同实体署名和别名核验解析艺人，兼容完整艺名被分隔符拆成多个片段。"""
    artists = music_artists(music)
    parsed = unique_music_texts(parsed_artists)
    keys = {music_text_key(artist) for artist in parsed}
    if len(parsed) > 1 and any(any(separator in artist for separator in ("/", "&", ",")) for artist in artists):
        keys.add(music_text_key(" / ".join(parsed)))
    return bool(keys & {music_text_key(artist) for artist in artists})


def music_base_title(value: Optional[str], *, preserve_editions: bool = False) -> str:
    """剥离已知版本后缀；数据源可保留发行版标签，不改变未知括号中的作品名。"""
    text = str(value or "")
    if not preserve_editions:
        text = _EDITION.sub("", text)

    def strip_version(match: re.Match[str]) -> str:
        """保留发行版声明，避免被包含它的录音版本标签一并删除。"""
        return match.group(0) if preserve_editions and _EDITION.search(match.group(0)) else ""

    return _BARE_VERSION_SUFFIX.sub(strip_version, _VERSION_SUFFIX.sub(strip_version, text)).strip()


def music_title_matches(music: MusicInfo, title: Optional[str], *, preserve_editions: bool = False) -> bool:
    """统一全半角后比较完整名称及别名，允许数据源保持既有发行版本边界。"""
    expected = music_text_key(music_base_title(normalize("NFKC", str(title or "")), preserve_editions=preserve_editions))
    return bool(expected and any(
        expected == music_text_key(music_base_title(normalize("NFKC", name), preserve_editions=preserve_editions))
        for name in music_titles(music)
    ))


def _isrc_key(value: Optional[str]) -> Optional[str]:
    """校验 12 位 ISRC 结构，兼容展示前缀、空白和分隔符，不接受占位值。"""
    code = re.sub(r"[\s-]+", "", str(value or ""))
    if not _ISRC.fullmatch(code) and code[:4].lower() == "isrc":
        code = code[4:].lstrip(":")
    return code.upper() if _ISRC.fullmatch(code) else None


def music_isrc_matches(music: MusicInfo, meta: MetaMusic) -> bool:
    """只有格式有效且相同的 ISRC 才能作为优先于文本匹配的录音身份。"""
    expected = _isrc_key(meta.isrc)
    return bool(expected and expected == _isrc_key(music.isrc))


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
    normalized = normalize("NFKC", text)
    return {name for name, pattern in _VERSIONS.items() if re.search(pattern, normalized, re.I)}


def _version_dates(title: Optional[str], version: Optional[str]) -> tuple[set[int], set[date]]:
    """只提取明确版本字段及版本后缀中的日期，不把数字作品名或发行年份当作录制日期。"""
    title_text = normalize("NFKC", str(title or ""))
    text = normalize("NFKC", " ".join([
        str(version or ""), *_VERSION_SUFFIX.findall(title_text), *_BARE_VERSION_SUFFIX.findall(title_text),
    ]))
    years = {int(year) for year in _VERSION_YEAR.findall(text)}
    dates: set[date] = set()
    for match in _VERSION_DATE.finditer(text):
        try:
            dates.add(date(*(int(value) for value in match.groups())))
        except ValueError:
            continue
    return years, dates


def music_version_matches(music: MusicInfo, meta: MetaMusic) -> bool:
    """资源匹配与候选确认共用录音版本约束，不从艺术家字段推断版本。"""
    target_title = music.album or music.title if music.music_type == MUSIC_ENTITY_ALBUM else music.title
    # 专辑类型描述整专版本，但单曲的所属专辑类型不能代替该录音自身的版本。
    album_versions = " ".join(music.secondary_types or []) if music.music_type == MUSIC_ENTITY_ALBUM else ""
    expected = _version_markers(f"{target_title or ''} {music.version or ''} {album_versions}")
    if expected != _version_markers(f"{meta.title or ''} {meta.version or ''}"):
        return False
    expected_years, expected_dates = _version_dates(target_title, music.version)
    actual_years, actual_dates = _version_dates(meta.title, meta.version)
    # 多个时间值可能描述区间或重发记录，不能当作唯一录制时间互斥比较。
    if len(expected_dates) == len(actual_dates) == 1 and expected_dates != actual_dates:
        return False
    return not (len(expected_years) == len(actual_years) == 1 and expected_years != actual_years)


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
    title_matched = any(music_title_matches(music, name) for name in names)
    content = f"{title} {description}"
    artist_matched = music_artist_matches(music, resource.artists) if resource.artists \
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
    if not music_version_matches(music, resource):
        return MusicMatch("candidate", "version_mismatch")
    if _EDITION.search(music.title or "") and not any(music_text_key(item) in music_text_key(content) for item in titles):
        return MusicMatch("candidate", "edition_unverified")
    return MusicMatch("exact", "matched")
