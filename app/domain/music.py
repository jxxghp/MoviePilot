"""音乐名称、版本与站点候选匹配的纯业务规则。"""

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Literal, Optional
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
    "rerecorded": r"\btaylor(?:'|’)?s version\b|\bre-?record(?:ed|ing)\b|重录|重錄",
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
_TITLE_CREDIT = re.compile(
    r"\s*[（(]\s*(?:feat(?:uring)?\.?\s+[^()（）]+|"
    r"(?:电视剧|電視劇|电影|電影|影视剧|影視劇|动画|動畫)?\s*《[^《》]+》"
    r"\s*(?:电视剧|電視劇|电影|電影|影视剧|影視劇|动画|動畫)?"
    r"\s*(?:[“「][^”」]+[”」])?\s*(?:主题曲|主題曲|片头曲|片頭曲|片尾曲|插曲))\s*[）)]",
    re.IGNORECASE,
)
_SOUNDTRACK_CREDIT = re.compile(
    r"\s*[\[(（【]\s*from\s+(?:the\s+)?(?:original\s+)?"
    r"(?:motion\s+picture|film|movie)\b[^\])）】]*[\])）】]",
    re.IGNORECASE,
)
_CJK_SOUNDTRACK_SUFFIX = re.compile(
    r"\s*《[^《》]+》\s*(?:电视剧|電視劇|电影|電影|影视剧|影視劇|动画|動畫)?\s*"
    r"(?:原声|原聲)?\s*(?:主题曲|主題曲|片头曲|片頭曲|片尾曲|插曲)\s*$",
    re.IGNORECASE,
)
_FEATURED_CREDIT = re.compile(r"[（(]\s*feat(?:uring)?\.?\s+([^()（）]+)[）)]", re.IGNORECASE)
_COLLECTIVE_ARTISTS = ("Various Artists", "Various", "VA", "群星", "众艺人", "眾藝人")
_VERSION_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_VERSION_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:[-./]|年)\s*(\d{1,2})(?:[-./]|月)\s*(\d{1,2})日?(?!\d)")
_ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", re.IGNORECASE | re.ASCII)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


class MusicDirectoryMatch(dict[str, MusicInfo]):
    """保持旧文件映射合同，同时保存专辑目录识别的可解释诊断。"""

    def __init__(self, values: Optional[dict[str, MusicInfo]] = None, *, recognition: Optional[dict[str, Any]] = None) -> None:
        """复制映射与诊断，读取和缓存使用方不共享可变摘要。"""
        super().__init__(values or {})
        self.recognition = deepcopy(recognition or {})


def music_package_error(filename: str) -> Optional[str]:
    """明确音乐模式下拒绝尚未展开的发行包，不将整包误识别为单首歌曲。"""
    lowered = filename.casefold()
    if lowered.endswith((".iso", ".img", ".nrg", ".bin", ".mdf")):
        return "音乐镜像暂不支持直接整理，请先提取音频和 CUE 后重试"
    if lowered.endswith((".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".001")):
        return "音乐压缩包暂不支持直接整理，请先解压音频后重试"
    return None


@dataclass(frozen=True, slots=True)
class MusicCueTrack:
    """CUE 中一个逻辑音轨的文件引用与 INDEX 01 起点，单位为每秒 75 帧。"""

    number: int
    file_name: str
    start_frame: int
    title: Optional[str] = None
    artist: Optional[str] = None
    isrc: Optional[str] = None


@dataclass(frozen=True, slots=True)
class MusicCueSheet:
    """不读取文件系统的 CUE 解析结果，专辑字段与逐轨字段保持独立。"""

    title: Optional[str]
    artist: Optional[str]
    year: Optional[int]
    disc_number: Optional[int]
    total_discs: Optional[int]
    tracks: tuple[MusicCueTrack, ...]


def _cue_text(value: str) -> str:
    """读取带引号或未加引号的文本，不执行反斜线转义或路径展开。"""
    value = value.strip()
    if value.startswith('"'):
        if not value.endswith('"') or len(value) < 2:
            raise ValueError("CUE 文本引号未闭合")
        return value[1:-1]
    return value


def _cue_frames(value: str) -> int:
    """校验分、秒、帧或直接帧数，禁止负值和超范围的秒/帧。"""
    if value.isdigit():
        return int(value)
    match = re.fullmatch(r"(\d{1,5}):(\d{2}):(\d{2})", value)
    if not match:
        raise ValueError("CUE 时间索引无效")
    minutes, seconds, frames = map(int, match.groups())
    if seconds >= 60 or frames >= 75:
        raise ValueError("CUE 时间索引超出范围")
    return (minutes * 60 + seconds) * 75 + frames


def parse_music_cue(text: str) -> MusicCueSheet:
    """解析音频 CUE 的专辑、FILE/TRACK/INDEX 结构，拒绝缺索引和逆序音轨。

    索引表示同一文件内的逻辑曲目，不代表已拆出独立音频。未消费的 REM、
    FLAGS 等信息继续保留在原文件中，解析器不重写用户内容。
    """
    album: dict[str, str] = {}
    rows: list[dict[str, str | int]] = []
    current: Optional[dict[str, str | int]] = None
    file_name: Optional[str] = None
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split(maxsplit=1)
        command, value = fields[0].upper(), fields[1].strip() if len(fields) > 1 else ""
        if command == "FILE":
            match = re.fullmatch(r'(?:"([^"\r\n]+)"|(\S+))\s+(\w+)', value)
            if not match or match.group(3).upper() not in {"WAVE", "MP3", "FLAC", "AIFF"}:
                raise ValueError("CUE FILE 格式无效或不是受支持的音频")
            file_name = match.group(1) or match.group(2)
        elif command == "TRACK":
            match = re.fullmatch(r"(\d{1,3})\s+AUDIO", value, re.IGNORECASE)
            if not match or not file_name or len(rows) >= 999:
                raise ValueError("CUE 音轨缺少 FILE 或不是有效音频轨")
            number = int(match.group(1))
            if number < 1 or (rows and number <= int(rows[-1]["number"])):
                raise ValueError("CUE 音轨编号重复或逆序")
            current = {"number": number, "file_name": file_name}
            rows.append(current)
        elif command == "INDEX":
            match = re.fullmatch(r"(\d{1,2})\s+(\S+)", value)
            if current is None or not match:
                raise ValueError("CUE 索引缺少所属音轨")
            frames = _cue_frames(match.group(2))
            if int(match.group(1)) == 1:
                if "start_frame" in current:
                    raise ValueError("CUE 音轨重复声明 INDEX 01")
                current["start_frame"] = frames
        elif command in {"TITLE", "PERFORMER", "ISRC"}:
            key = {"TITLE": "title", "PERFORMER": "artist", "ISRC": "isrc"}[command]
            if current is not None:
                current[key] = _cue_text(value)
            elif command != "ISRC":
                album[key] = _cue_text(value)
        elif command == "REM" and current is None:
            fields = value.split(maxsplit=1)
            key, raw = (fields[0], fields[1]) if len(fields) == 2 else ("", "")
            if key.upper() in {"DATE", "DISCNUMBER", "TOTALDISCS"}:
                album[key.lower()] = _cue_text(raw)
    if not rows:
        raise ValueError("CUE 没有音轨")
    tracks = []
    previous: dict[str, int] = {}
    for row in rows:
        if "start_frame" not in row:
            raise ValueError("CUE 音轨缺少 INDEX 01")
        name, start = str(row["file_name"]), int(row["start_frame"])
        if start <= previous.get(name, -1):
            raise ValueError("CUE 同一文件的时间索引重复或逆序")
        previous[name] = start
        tracks.append(MusicCueTrack(
            number=int(row["number"]), file_name=name, start_frame=start,
            title=str(row["title"]) if row.get("title") else None,
            artist=str(row["artist"]) if row.get("artist") else None,
            isrc=str(row["isrc"]) if row.get("isrc") else None,
        ))
    year = album.get("date", "")[:4]
    return MusicCueSheet(
        title=album.get("title"), artist=album.get("artist"), year=int(year) if year.isdigit() else None,
        disc_number=int(album["discnumber"]) if album.get("discnumber", "").isdigit() else None,
        total_discs=int(album["totaldiscs"]) if album.get("totaldiscs", "").isdigit() else None,
        tracks=tuple(tracks),
    )


def music_tags_are_usable(meta: Optional[MetaMusic]) -> bool:
    """判断纯标签是否足以确定本地整理路径，不把文件名或目录猜测当作标签。

    年份和远端 ID 不影响本地可整理性；占位、乱码或宣传 URL 不能成为
    歌曲/专辑身份。调用方必须传入未经路径补写的原始标签证据。
    """
    if meta is None or music_track_title_is_weak(meta):
        return False
    artist = meta.album_artist or next(iter(meta.artists), None)
    required = [meta.title, meta.album, artist]
    return all(_usable_music_tag(value) for value in required)


def music_track_title_is_weak(meta: MetaMusic) -> bool:
    """编号或占位文件名不能否决实际曲目，但真实标签中的数字歌曲名仍是证据。"""
    title = str(meta.title or "").strip()
    if not _usable_music_tag(title):
        return True
    if title.isdigit():
        if len(title) > 1 and title.startswith("0") and not meta.media_id and meta.music_type != MUSIC_ENTITY_ALBUM:
            return True
        return meta.field_sources.get("title") not in {"tag", "cue", "manual", "remote"}
    if meta.field_sources.get("title") not in {"tag", "cue", "manual", "remote"} and re.fullmatch(r"[a-f\d]{16,64}", title, re.I):
        return True
    return bool(re.fullmatch(r"(?:(?:track|audio|音轨|曲目)[\s._-]*\d*|unknown|untitled)", title, re.I))


def music_usable_artists(artists: Iterable[str]) -> list[str]:
    """剔除抓轨占位、宣传链接和乱码署名；缺失艺人不是与指纹候选冲突的证据。"""
    return [artist for artist in artists if _usable_music_tag(artist)]


def music_album_title_is_weak(meta: MetaMusic) -> bool:
    """普通收件目录不是专辑证据，标签或种子明确提供的同名专辑仍有效。"""
    title = meta.album or meta.title
    if not title:
        return True
    source = meta.field_sources.get("album" if meta.album else "title")
    if source in {"tag", "album_tags", "cue", "torrent", "manual", "remote"}:
        return False
    return music_text_key(title) in {
        "music", "musics", "audio", "downloads", "download", "inbox", "unknown", "untitled",
        "various", "variousartists", "va", "音乐", "音樂", "下载", "下載", "未分类", "未分類",
    }


def expand_music_tracks(metas: list[MetaMusic]) -> tuple[list[MetaMusic], list[int]]:
    """把整轨 CUE 投影为用于匹配的逻辑曲目，并保留各曲所属物理文件索引。"""
    tracks: list[MetaMusic] = []
    owners: list[int] = []
    for owner, meta in enumerate(metas):
        if meta.organization_error:
            return [], []
        if meta.music_layout != "image_cue":
            tracks.append(meta)
            owners.append(owner)
            continue
        if not meta.cue_tracks:
            return [], []
        for index, cue in enumerate(meta.cue_tracks):
            start, number = cue.get("start_frame"), cue.get("number")
            end = meta.cue_tracks[index + 1].get("start_frame") if index + 1 < len(meta.cue_tracks) else (
                meta.duration * 75 if meta.duration else None
            )
            if not isinstance(start, int) or start < 0 or not isinstance(number, int) or not 1 <= number <= 999:
                return [], []
            if end is not None and (not isinstance(end, (int, float)) or end <= start):
                return [], []
            track = MetaMusic.from_dict(meta.to_dict())
            track.title = str(cue.get("title") or f"Track {number}")
            track.field_sources["title"] = "cue" if cue.get("title") else "filename"
            track.artists = [str(cue["artist"])] if cue.get("artist") else list(meta.artists)
            track.track_number = number
            track.duration = round((end - start) / 75) if end is not None else None
            track.isrc = str(cue["isrc"]) if cue.get("isrc") else None
            track.field_sources["track_number"] = "cue"
            for key in ("duration", "isrc"):
                if getattr(track, key) is not None:
                    track.field_sources[key] = "cue"
                else:
                    track.field_sources.pop(key, None)
            track.music_type = "recording"
            track.music_layout, track.cue_filename, track.cue_tracks = None, None, []
            track.media_id, track.media_source, track.musicbrainz_release_track_id = None, None, None
            tracks.append(track)
            owners.append(owner)
    return tracks, owners


def _alignment_title_key(title: Optional[str]) -> str:
    """对位仅忽略名称排版差异，保留 live/remix 等版本正文和纯符号歌曲名。"""
    return music_text_key(title) or re.sub(r"\s+", "", normalize("NFKC", str(title or ""))).casefold()


def _music_track_pair_score(meta: MetaMusic, track: MusicInfo, allow_title_override: bool) -> float:
    """评估一条文件与发行曲目的证据；明确身份或时长冲突不能被其它分数抵消。"""
    identity_match = False
    for field in ("musicbrainz_release_id", "musicbrainz_release_track_id"):
        local_id, remote_id = getattr(meta, field), getattr(track, field)
        if local_id and remote_id:
            if local_id != remote_id and not allow_title_override:
                return 0.0
            if field == "musicbrainz_release_track_id" and local_id == remote_id:
                identity_match = True
    if meta.media_id and track.media_id and meta.media_source == track.media_source and meta.music_type != MUSIC_ENTITY_ALBUM:
        if meta.media_id != track.media_id and not allow_title_override:
            return 0.0
        identity_match = identity_match or meta.media_id == track.media_id
    if (not allow_title_override and meta.artists and track.artists
            and meta.field_sources.get("artists") not in {"directory", "torrent", "album_tags"}
            and not music_artist_matches(track, meta.artists)):
        return 0.0
    duration_close = False
    if meta.duration and track.duration:
        delta = abs(meta.duration - track.duration)
        longest = max(meta.duration, track.duration)
        if delta > max(10, longest * 0.08):
            return 0.0
        duration_close = delta <= max(3, min(8, longest * 0.025))
    title_key = _alignment_title_key(meta.title)
    weak_title = music_track_title_is_weak(meta)
    title_match = not weak_title and bool(title_key) and any(
        title_key == _alignment_title_key(value) for value in music_titles(track)
    )
    position_match = bool(
        meta.track_number and meta.track_number == track.track_number
        and (not meta.disc_number or meta.disc_number == (track.disc_number or 1))
    )
    if not identity_match and not title_match and not weak_title and not (allow_title_override and position_match):
        return 0.0
    if not identity_match and not title_match and meta.duration and track.duration and not duration_close:
        return 0.0
    if not (identity_match or title_match or position_match or (weak_title and duration_close)):
        return 0.0
    # 同一容差内的多个时长候选保持同分，不能以一秒的测量差异打破歧义。
    return 1000 * identity_match + 60 * title_match + 30 * position_match + 20 * duration_close


def _unique_track_choice(scores: dict[int, float]) -> Optional[int]:
    """只选择有唯一最强证据的候选，不用列表顺序消除歧义。"""
    if not scores:
        return None
    maximum = max(scores.values())
    choices = [index for index, score in scores.items() if score == maximum]
    return choices[0] if len(choices) == 1 else None


def align_music_tracks(
        metas: list[MetaMusic], tracks: list[MusicInfo], *, allow_title_override: bool = False,
) -> dict[int, int]:
    """以双向唯一证据对位曲目，返回本地索引到远端索引；无证据项保持未匹配。

    手选发行可依据唯一位置纠正旧曲名及旧身份，但仍拒绝显著时长冲突。
    双向检查使重复版本不能抢占一个位置，输入排序不会改变歌曲身份。
    """
    scores = {
        index: {
            remote: score for remote, track in enumerate(tracks)
            if (score := _music_track_pair_score(meta, track, allow_title_override)) > 0
        }
        for index, meta in enumerate(metas)
    }
    matched: dict[int, int] = {}
    while scores:
        choices = {index: choice for index, values in scores.items() if (choice := _unique_track_choice(values)) is not None}
        accepted = {
            index: remote for index, remote in choices.items()
            if _unique_track_choice({local: values[remote] for local, values in scores.items() if remote in values}) == index
        }
        if not accepted:
            break
        matched.update(accepted)
        used = set(accepted.values())
        scores = {
            index: {remote: score for remote, score in values.items() if remote not in used}
            for index, values in scores.items() if index not in accepted
        }
    return matched


def _usable_music_tag(value: Optional[str]) -> bool:
    """保守排除占位值及乱码，不误删数字专辑名和包含 Unknown 的正常标题。"""
    text = str(value or "").strip()
    placeholders = {"unknown", "unknown artist", "unknown album", "untitled", "未知", "未知艺术家", "未知专辑"}
    return bool(
        text
        and text.casefold() not in placeholders
        and "\ufffd" not in text
        and not re.search(r"https?://|www\.", text, re.IGNORECASE)
        and not re.fullmatch(r"(?:track|audio|音轨|曲目)\s*[-._ ]*\d+", text, re.IGNORECASE)
    )


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
    """剥离明确署名、影视用途和版本注释，保留未知括号中的作品名。"""
    text = _CJK_SOUNDTRACK_SUFFIX.sub(
        "", _SOUNDTRACK_CREDIT.sub("", _TITLE_CREDIT.sub("", str(value or "")))
    )
    if not preserve_editions:
        text = _EDITION.sub("", text)

    def strip_version(match: re.Match[str]) -> str:
        """保留发行版声明，避免被包含它的录音版本标签一并删除。"""
        return match.group(0) if preserve_editions and _EDITION.search(match.group(0)) else ""

    return _BARE_VERSION_SUFFIX.sub(strip_version, _VERSION_SUFFIX.sub(strip_version, text)).strip()


def _music_title_key(value: Optional[str], *, preserve_editions: bool = False) -> str:
    """生成音乐标题比较键，纯符号名称在普通归一化为空时保留其符号身份。"""
    base = music_base_title(
        normalize("NFKC", str(value or "")),
        preserve_editions=preserve_editions,
    )
    key = music_text_key(base)
    if key:
        return key
    return re.sub(r"\s+", "", base).casefold()


def music_title_matches(music: MusicInfo, title: Optional[str], *, preserve_editions: bool = False) -> bool:
    """统一全半角后比较完整名称及别名，纯符号名称也保留其身份。"""
    expected = _music_title_key(title, preserve_editions=preserve_editions)
    return bool(expected and any(
        expected == _music_title_key(name, preserve_editions=preserve_editions)
        for name in music_titles(music)
    ))


def music_album_matches(music: MusicInfo, album: Optional[str]) -> bool:
    """核验所属专辑本体，忽略 Deluxe 等发行装帧说明但保留录音版本边界。"""
    def album_key(value: Optional[str]) -> str:
        """目录常在发行说明后附加 CD1/Disc 2，不应改变专辑身份。"""
        base = music_base_title(normalize("NFKC", str(value or "")))
        base = re.sub(r"[\s._-]*(?:cd|disc|disk)\s*\d+$", "", base, flags=re.I)
        return music_text_key(base)

    expected = album_key(album)
    return bool(expected and any(
        expected == album_key(name)
        for name in music_titles(music, album=True)
    ))


def music_year_matches(music: MusicInfo, meta: MetaMusic) -> bool:
    """双方都有发行年份时要求一致；任一侧未知时不凭空制造冲突。"""
    if not music.year or not meta.year:
        return True
    try:
        return int(music.year) == int(meta.year)
    except (TypeError, ValueError):
        return str(music.year).strip() == str(meta.year).strip()


def music_release_year_matches(music: MusicInfo, meta: MetaMusic) -> bool:
    """具体发行按当前发行年核验；已分离的原始年不能再冒充当前年制造再版冲突。"""
    if meta.release_year:
        return not music.release_year or meta.release_year == music.release_year
    if meta.original_year or (meta.musicbrainz_release_id and meta.musicbrainz_release_id == music.musicbrainz_release_id):
        return True
    return music_year_matches(music, meta) or bool(meta.year and str(meta.year) == str(music.original_year))


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


def _artist_match_text(text: str) -> str:
    """归一署名比较文本但保留分隔符，避免紧凑名称丢失单词边界。"""
    normalized = str(zhconv_convert(normalize("NFKD", text).casefold(), "zh-hans"))
    return "".join(char for char in normalized if not combining(char))


def music_artist_affix_matches(title: str, artist: str, *, suffix: bool = False) -> bool:
    """核验首尾完整署名；保留中日韩连写习惯，但不能截断其它文字的单词。"""
    key = music_text_key(artist)
    if not key:
        return False
    text = _artist_match_text(title)
    pattern = r"[\W_]*".join(re.escape(char) for char in key)
    match = re.search(rf"{pattern}[\W_]*$" if suffix else rf"^[\W_]*{pattern}", text)
    if not match:
        return False
    neighbor = text[match.start() - 1:match.start()] if suffix else text[match.end():match.end() + 1]
    edge = key[0] if suffix else key[-1]
    return not (neighbor.isalnum() and edge.isalnum() and not _CJK.search(neighbor + edge))


def _contains_artist(text: str, artist: str) -> bool:
    """匹配完整署名，避免短拉丁艺名命中另一个人名的子串。"""
    normalized = _artist_match_text(text)
    key = music_text_key(artist)
    if not key:
        return False
    pattern = r"[\W_]*".join(re.escape(char) for char in key)
    return bool(re.search(r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])", normalized))


def _resource_names(primary: MetaMusic, artists: list[str], *, album: bool = False,
                    album_suffixes: Optional[list[str]] = None) -> list[str]:
    """复用音乐命名解析器提取作品片段，去掉已确认的首尾艺术家署名。"""
    names: list[str] = []
    artist_keys = [(item, music_text_key(item)) for item in artists if item]
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
            name = music_base_title(_TITLE_LABEL.sub("", part.strip()))
            key = _music_title_key(name)
            if not key:
                continue
            names.append(key)
            for artist, artist_key in artist_keys:
                if key.startswith(artist_key) and key != artist_key and music_artist_affix_matches(name, artist):
                    remainder = re.sub(r"^[的之]", "", key[len(artist_key):])
                    if remainder:
                        names.append(remainder)
                if key.endswith(artist_key) and key != artist_key and music_artist_affix_matches(name, artist, suffix=True):
                    names.append(key[:-len(artist_key)])
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


def _resource_credits_match(music: MusicInfo, meta: MetaMusic) -> bool:
    """资源显式客串署名必须得到目标艺术家或同一署名注释确认，不能把合作版匹配为独唱。"""
    credits = {
        music_text_key(artist)
        for value in _FEATURED_CREDIT.findall(meta.title or "")
        for artist in MetaMusic._split_artists(value)
    }
    confirmed = {music_text_key(artist) for artist in music_artists(music)}
    confirmed.update(
        music_text_key(artist)
        for value in _FEATURED_CREDIT.findall(music.title or "")
        for artist in MetaMusic._split_artists(value)
    )
    return credits <= confirmed


def music_version_matches(music: MusicInfo, meta: MetaMusic) -> bool:
    """资源匹配与候选确认共用录音版本约束，不从艺术家字段推断版本。"""
    if not _resource_credits_match(music, meta):
        return False
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
            _music_title_key(item) in names
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
            _music_title_key(item) in primary_names for item in titles
        )):
            return MusicMatch("candidate", "partial_album")
        if music.year and resource.year and str(music.year) != str(resource.year):
            return MusicMatch("candidate", "year_mismatch")
    if not music_version_matches(music, resource):
        return MusicMatch("candidate", "version_mismatch")
    if _EDITION.search(music.title or "") and not any(music_text_key(item) in music_text_key(content) for item in titles):
        return MusicMatch("candidate", "edition_unverified")
    return MusicMatch("exact", "matched")
