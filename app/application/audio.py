import re
import shutil
import struct
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import S_IWUSR
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any, Iterator, Optional, Union
from uuid import UUID

import chardet
from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.aiff import AIFF
from mutagen.apev2 import APEBinaryValue, APETextValue, APEv2
from mutagen.asf import ASF, ASFBoolAttribute, ASFByteArrayAttribute, ASFTags
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, SYLT, TMCL, TXXX, UFID, USLT, Frames
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4Tags
from mutagen.wave import WAVE

from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MUSIC_CREDIT_FIELDS, MetaMusic, music_credit_values, parse_music_release_types
from app.domain.music import MusicCueSheet, parse_music_cue
from app.runtime.log import logger
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource

_AudioSignature = tuple[str, int, int, int, int]

# 首项是写入键，其余是只读兼容别名；独立乐团/演奏者是自定义字段。
_ASF_FIELDS = {
    "title": ("Title",), "artist": ("Author",), "album": ("WM/AlbumTitle",),
    "albumartist": ("WM/AlbumArtist", "album artist"), "date": ("WM/Year", "year"),
    "originaldate": ("WM/OriginalReleaseTime", "WM/OriginalReleaseYear", "originalyear"),
    "tracknumber": ("WM/TrackNumber", "track"), "discnumber": ("WM/PartOfSet", "disc"),
    "isrc": ("WM/ISRC",), "subtitle": ("WM/SubTitle",),
    "composer": ("WM/Composer",), "conductor": ("WM/Conductor",),
    "orchestra": ("WM/Orchestra", "WM/Ensemble", "ensemble"), "performer": ("WM/Performer",),
    "compilation": ("WM/IsCompilation",), "lyrics": ("WM/Lyrics",),
    "musicbrainz_trackid": ("MusicBrainz/Track Id", "musicbrainz_track_id"),
    "musicbrainz_albumid": ("MusicBrainz/Album Id", "musicbrainz_album_id"),
    "musicbrainz_releasegroupid": ("MusicBrainz/Release Group Id", "musicbrainz_release_group_id"),
    "musicbrainz_releasetrackid": ("MusicBrainz/Release Track Id", "musicbrainz_releasetrack_id"),
    "musicbrainz_albumtype": ("MusicBrainz/Album Type",),
}


@dataclass(slots=True)
class _AudioScan:
    """仅在当前扫描中保存元数据副本，不保留可写 Mutagen 对象或文件句柄。"""

    tags: OrderedDict[_AudioSignature, Optional[MetaMusic]] = field(default_factory=OrderedDict)
    cues: OrderedDict[_AudioSignature, str] = field(default_factory=OrderedDict)
    lock: Any = field(default_factory=RLock)
    active: bool = True


_audio_scan: ContextVar[Optional[_AudioScan]] = ContextVar("audio_metadata_scan", default=None)


@contextmanager
def capture_audio_metadata() -> Iterator[None]:
    """一次整理共享有界只读快照，退出后连同继承上下文的 worker 也不能继续复用。"""
    existing = _audio_scan.get()
    if existing is not None and existing.active:
        yield
        return
    scan = _AudioScan()
    token = _audio_scan.set(scan)
    try:
        yield
    finally:
        with scan.lock:
            scan.active = False
            scan.tags.clear()
            scan.cues.clear()
        _audio_scan.reset(token)


def _audio_signature(path: Path) -> Optional[_AudioSignature]:
    """文件替换、内容或元数据变更都使扫描快照失效；保留路径别名的目录语义。"""
    try:
        stat = path.stat()
        return str(path.absolute()), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return None


@contextmanager
def _audio_write_target(path: Path) -> Iterator[Path]:
    """链接目标在独立副本上完成全部标签写入，成功后只替换媒体库中的目录项。

    不解析并改写符号链接指向的源文件。临时文件与目标同目录，以便原子替换；
    任一写入失败或期间目标发生变化时保留原链接，并清理未提交副本。
    """
    stat = path.stat()
    link_inode = path.lstat().st_ino
    if not path.is_symlink() and stat.st_nlink <= 1:
        yield path
        return
    signature = _audio_signature(path)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name[:40]}.", suffix=".mp-audio-partial", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copy2(path, temporary)
        # 做种源可以只读；只给独立副本增加写权限，源权限保持不变。
        temporary.chmod(stat.st_mode | S_IWUSR)
        initial_copy = _audio_signature(temporary)
        yield temporary
        if _audio_signature(temporary) == initial_copy:
            return
        if path.lstat().st_ino != link_inode or _audio_signature(path) != signature:
            raise OSError("标签写入期间目标文件已变化，保留当前目标并取消替换")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_cue_text(path: Path) -> str:
    """同一扫描复用 CUE 文本，文件发生变化时重新读取。"""
    scan, signature = _audio_scan.get(), _audio_signature(path)
    if scan is None or not scan.active or signature is None:
        return _load_cue_text(path)
    with scan.lock:
        if not scan.active:
            return _load_cue_text(path)
        if signature in scan.cues:
            scan.cues.move_to_end(signature)
            return scan.cues[signature]
        text = _load_cue_text(path)
        if signature == _audio_signature(path):
            scan.cues[signature] = text
            while len(scan.cues) > 32:
                scan.cues.popitem(last=False)
        return text


def _load_cue_text(path: Path) -> str:
    """有界读取 CUE，优先 Unicode，再用已有编码探测与 GB18030 兼容中文旧文件。"""
    with path.open("rb") as stream:
        payload = stream.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError("CUE 文件超过 1 MiB，无法自动处理")
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = payload.decode("utf-16")
    else:
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            detected = chardet.detect(payload)
            encoding = detected.get("encoding") if (detected.get("confidence") or 0) >= 0.5 else None
            text = payload.decode(encoding or "gb18030")
    return text


def _cue_mentions_audio(text: str, path: Path) -> bool:
    """即使索引结构损坏，也保留 FILE 对当前音频的明确关联，避免错误回退指纹。"""
    for line in text.splitlines():
        match = re.match(r'\s*FILE\s+(?:"([^"\r\n]+)"|(\S+))', line, re.IGNORECASE)
        if match:
            name = (match.group(1) or match.group(2)).replace("\\", "/")
            if PurePosixPath(name).stem.casefold() == path.stem.casefold():
                return True
    return False


def _cue_candidates(path: Path) -> list[Path]:
    """只扫描音频同目录的有限 CUE，不遍历全集或其它目录。"""
    candidates = []
    for item in path.parent.iterdir():
        if item.is_file() and item.suffix.casefold() == ".cue" and not item.name.startswith("."):
            candidates.append(item)
            if len(candidates) > 32:
                raise ValueError("同目录 CUE 超过 32 个，请按专辑分别整理")
    return sorted(candidates, key=lambda item: (item.stem.casefold() != path.stem.casefold(), item.name))


def _cue_reference_name(name: str) -> str:
    """只允许同目录相对文件名，绝对路径、目录穿越及跨目录引用必须人工处理。"""
    normalized = PurePosixPath(name.replace("\\", "/"))
    if PureWindowsPath(name).drive or normalized.is_absolute() or len(normalized.parts) != 1 or name in {".", ".."}:
        raise ValueError("CUE 引用了目录外或跨目录音频，请保留原目录结构后处理")
    return normalized.name


def _find_audio_cue(path: Path) -> Optional[tuple[Path, MusicCueSheet]]:
    """以 FILE 引用定位关联 CUE，冲突或同名损坏索引不能退回首曲指纹识别。"""
    matches: list[tuple[Path, MusicCueSheet]] = []
    for candidate in _cue_candidates(path):
        try:
            text = _read_cue_text(candidate)
        except (ValueError, OSError, UnicodeError, LookupError):
            if candidate.stem.casefold() == path.stem.casefold():
                raise ValueError(f"关联 CUE 无法解析：{candidate.name}") from None
            continue
        try:
            sheet = parse_music_cue(text)
        except ValueError as error:
            if candidate.stem.casefold() == path.stem.casefold() or _cue_mentions_audio(text, path):
                raise ValueError(f"关联 CUE 无法解析：{candidate.name} - {error}") from error
            continue
        references = {track.file_name for track in sheet.tracks}
        related = [name for name in references if PurePosixPath(name.replace("\\", "/")).stem.casefold() == path.stem.casefold()]
        if not related:
            continue
        for name in related:
            if _cue_reference_name(name) != path.name:
                raise ValueError(f"CUE 引用的文件名与当前音频不一致：{name}")
        matches.append((candidate, sheet))
    if len(matches) > 1 and any(sheet != matches[0][1] for _, sheet in matches[1:]):
        raise ValueError("存在多个内容冲突的 CUE，请保留需要的索引后重试")
    return matches[0] if matches else None


def _apply_audio_cue(path: Path, meta: MetaMusic) -> MetaMusic:
    """读取实际关联 CUE 补全曲目或整轨专辑，保留音频本体与索引文件的原始内容。"""
    if path.suffix.casefold() == ".cue" or not path.is_file():
        return meta
    try:
        found = _find_audio_cue(path)
        if not found:
            return meta
        cue_path, sheet = found
        tracks = [track for track in sheet.tracks if _cue_reference_name(track.file_name) == path.name]
        if meta.duration is not None and any(track.start_frame >= meta.duration * 75 for track in tracks):
            raise ValueError("CUE 索引超出音频实际时长")
        meta.cue_filename = cue_path.name
        meta.cue_tracks = [asdict(track) for track in tracks]
        image = len(tracks) > 1
        if image and len({track.file_name for track in sheet.tracks}) != 1:
            raise ValueError("多文件整轨 CUE 需要保留完整原目录，暂不自动归档")
        meta.music_layout = "image_cue" if image else "tracks_cue"
        fields: dict[str, Any] = {"album": sheet.title, "album_artist": sheet.artist, "year": sheet.year,
                                  "disc_number": sheet.disc_number, "total_discs": sheet.total_discs}
        origins: dict[str, str] = {}
        if image:
            artist = sheet.artist or meta.album_artist or next(iter(meta.artists), None)
            fields.update(title=sheet.title or meta.album, album=sheet.title or meta.album,
                          album_artist=artist, artists=[artist] if artist else [])
            if not sheet.title:
                origins["title"] = origins["album"] = meta.field_sources.get("album", "unknown")
            if not sheet.artist:
                origins["artists"] = origins["album_artist"] = meta.field_sources.get("album_artist", meta.field_sources.get("artists", "unknown"))
            meta.music_type = "album"
            meta.album_type = meta.album_type or "Album"
            meta.track_number = None
            meta.total_tracks = len(tracks)
            meta.musicbrainz_release_track_id = None
            meta.media_source = None
            meta.media_id = None
            for key in ("media_id", "media_source", "track_number", "musicbrainz_release_track_id"):
                meta.field_sources.pop(key, None)
        else:
            track = tracks[0]
            fields.update(title=track.title, artists=[track.artist or sheet.artist] if track.artist or sheet.artist else [],
                          track_number=track.number, isrc=track.isrc)
        for key, value in fields.items():
            if value in (None, "", []):
                continue
            if (image and key in {"title", "artists", "album", "album_artist"}) or not getattr(meta, key) or meta.field_sources.get(key) in {"filename", "directory"}:
                setattr(meta, key, value)
                meta.field_sources[key] = origins.get(key, "cue")
        return meta
    except (ValueError, OSError, UnicodeError, LookupError) as error:
        meta.music_layout = "cue_invalid"
        meta.organization_error = str(error)
        return meta


def _read_mp4_tags(tags: MP4Tags) -> dict[str, list[str]]:
    """读取标准 MP4 atom 与明确的文本 freeform，覆盖 EasyMP4 未注册的发行 ID。"""
    atoms = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
             "albumartist": "aART", "date": "\xa9day", "genre": "\xa9gen", "composer": "\xa9wrt"}
    values = {key: [str(value) for value in tags.get(atom, [])] for key, atom in atoms.items()}
    for key, atom in (("tracknumber", "trkn"), ("discnumber", "disk")):
        positions = tags.get(atom) or []
        if positions:
            current, total = positions[0]
            values[key] = [f"{current}/{total}"]
    normalized = {key.casefold(): value for key, value in tags.items()}
    for name in ("MusicBrainz Track Id", "MusicBrainz Album Id", "MusicBrainz Release Group Id",
                 "MusicBrainz Release Track Id", "MusicBrainz Album Type", "ISRC", "ORIGINALDATE",
                 "ORIGINALYEAR", "VERSION", "SUBTITLE", "RELEASETYPE", "CONDUCTOR", "ORCHESTRA", "ENSEMBLE", "PERFORMER"):
        raw = normalized.get(f"----:com.apple.itunes:{name.casefold()}", [])
        values[name.casefold().replace(" ", "_")] = [
            value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
            for value in raw
        ]
    if "cpil" in tags:
        values["compilation"] = ["1" if tags["cpil"] else "0"]
    return values


def _asf_text_values(values: Any) -> list[str]:
    """ASF文本/整数/布尔属性均有合法文本含义，二进制属性不能伪装成标题或MBID。"""
    return [str(int(item.value)) if isinstance(item, ASFBoolAttribute) else str(item).strip()
            for item in values or [] if not isinstance(item, (ASFByteArrayAttribute, bytes)) and str(item).strip()]


def _read_asf_tags(tags: ASFTags) -> dict[str, list[str]]:
    """优先读取Windows Media原生键，再兼容旧小写写入；WM/Track为零基编号。"""
    values = {key.casefold(): _asf_text_values(items) for key, items in tags.items()}
    for key, aliases in _ASF_FIELDS.items():
        values[key] = next((items for alias in (*aliases, key)
                            if (items := _asf_text_values(tags.get(alias)) or values.get(alias.casefold()))), [])
    current = next(iter(values.get("tracknumber", [])), "").split("/", 1)[0]
    if not current.isdecimal() or int(current) < 1:
        legacy = next(iter(values.get("wm/track", [])), "")
        if legacy.isdecimal():
            values["tracknumber"] = [str(int(legacy) + 1)]
    return values


def _write_asf_tags(tags: ASFTags, values: dict[str, list[str]]) -> bool:
    """差异预检后只替换所需字段及其兼容别名，保留用户其它ASF属性。"""
    for key, value in values.items():
        names = _ASF_FIELDS.get(key, (key,))
        aliases = {name.casefold() for name in (*names, key)}
        if key == "tracknumber":
            aliases.add("wm/track")
        if key in {"tracknumber", "discnumber"} and "/" in value[0]:
            aliases.update(("tracktotal", "totaltracks") if key == "tracknumber" else ("disctotal", "totaldiscs"))
        for existing in list(tags.keys()):
            if existing.casefold() in aliases or (key == "performer" and existing.casefold().startswith("performer:")):
                del tags[existing]
        tags[names[0]] = value
    return bool(values)


def _mark_asf_quality(meta: MetaMusic, audio: ASF) -> None:
    """ASF编码类型来自音频流的Codec List；不能从码率、扩展名或描述中的宣传词推断无损。"""
    codec = audio.info.codec_type.casefold()
    if codec.startswith("windows media audio"):
        meta.apply_audio_quality("WMA lossless" if "lossless" in codec else "WMA", overwrite=True, evidence_source="stream")
    else:
        # 未知编码的ASF不沿用可能错误的.flac/.mp3扩展名声明实际流格式。
        meta.audio_format = None
        meta.audio_lossless = None
        meta.field_sources.pop("audio_format", None)
        meta.field_sources.pop("audio_lossless", None)


def _write_binary_cover(audio: Any, path: Path, data: bytes, mime: str, overwrite: bool) -> None:
    """ASF与APE使用专用二进制属性保存封面，保留缺失才补写的策略。"""
    if audio.tags is None:
        audio.add_tags()
    key = "WM/Picture" if isinstance(audio, ASF) else "Cover Art (Front)"
    if audio.tags.get(key) and not overwrite:
        return
    if isinstance(audio, ASF):
        # ASF_FLAT_PICTURE：类型、图像字节数、两个UTF-16LE终止字符串及图像数据。
        payload = struct.pack("<BI", 3, len(data)) + (mime + "\0\0").encode("utf-16-le") + data
        audio.tags[key] = [ASFByteArrayAttribute(payload)]
    else:
        filename = "cover.png" if mime == "image/png" else "cover.jpg"
        audio.tags[key] = APEBinaryValue(filename.encode("ascii") + b"\x00" + data)
    audio.save(path)


def _performer_pair(value: str) -> tuple[str, str]:
    """解析PERFORMER专用的“人名 (乐器)”标签，不拆分人名内部的逗号或斜线。"""
    match = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", value.strip())
    return (match[2].strip(), match[1].strip()) if match else ("performer", value.strip())


def _id3_credit_field(tags: ID3, frame_id: str, role: str) -> Optional[str]:
    """按原生人员帧职责分类，v2.4制作人员表中的未知职责不猜为乐器。"""
    nonperformers = {"producer", "engineer", "mix", "dj-mix", "remixer", "arranger", "writer", "lyricist", "mastering"}
    role_fields = {"composer": "composer", "conductor": "conductor", "orchestra": "orchestra", "ensemble": "orchestra"}
    role = role.casefold()
    if role in role_fields:
        return role_fields[role]
    if role in nonperformers:
        return None
    legacy = frame_id == "IPLS" or (frame_id == "TIPL" and tags.version < (2, 4, 0))
    return "performer" if frame_id == "TMCL" or legacy or role == "performer" else None


def _id3_credit_values(tags: ID3) -> dict[str, list[str]]:
    """读取演奏人员帧，兼容v2.3 IPLS转TIPL；制作、混音等职责不伪装成演奏者。"""
    result: dict[str, list[str]] = {}
    for frame_id in ("TMCL", "IPLS", "TIPL"):
        for frame in tags.getall(frame_id):
            for role, name in frame.people:
                role, name = str(role).strip(), str(name).strip()
                key = _id3_credit_field(tags, frame_id, role)
                if not name or not key:
                    continue
                value = f"{name} ({role})" if key == "performer" and role and role.casefold() != "performer" else name
                result.setdefault(key, []).append(value)
    return result


def _remove_id3_credit_aliases(tags: ID3, key: str) -> bool:
    """覆盖明确角色时清理其旧人员帧/自定义别名，保留制作人和其它职责。"""
    changed = False
    for frame_id in ("TMCL", "TIPL", "IPLS"):
        for frame in tags.getall(frame_id):
            people = [item for item in frame.people if _id3_credit_field(tags, frame_id, str(item[0]).strip()) != key]
            if people != frame.people:
                frame.people, changed = people, True
                if not people:
                    tags.delall(frame.HashKey)
    for frame in tags.getall("TXXX"):
        name = str(frame.desc).strip().casefold()
        if name == key or (key == "orchestra" and name == "ensemble") or (key == "performer" and name.startswith("performer:")):
            tags.delall(frame.HashKey)
            changed = True
    return changed


def _credit_tag_values(credits: dict[str, Any]) -> dict[str, list[str]]:
    """角色模型统一转换为文本标签，预检和写入使用同一乐器署名格式。"""
    return {
        "composer": credits["composers"], "conductor": credits["conductors"], "orchestra": credits["orchestras"],
        "performer": [f"{name} ({role})" if role != "performer" else name
                      for role, names in credits["performers"].items() for name in names],
    }


def _remove_mp4_credit_aliases(tags: MP4Tags, values: dict[str, list[str]]) -> bool:
    """纠正MP4角色时移除其大小写变体和ENSEMBLE别名，避免读回仍混有旧阵容。"""
    changed = False
    for atom in list(tags.keys()):
        name = atom.casefold().removeprefix("----:com.apple.itunes:")
        field = {"conductor": "conductor", "orchestra": "orchestra", "ensemble": "orchestra", "performer": "performer"}.get(name)
        if field in values:
            del tags[atom]
            changed = True
    return changed


def _mark_audio_evidence(meta: MetaMusic, audio: Any) -> MetaMusic:
    """记录实际标签和流参数的来源，名称推测不能获得标签级可信度。"""
    tag_fields = (
        "title", "artists", "album", "album_artist", "year", "original_year", "release_year",
        "disc_number", "track_number", "total_discs", "total_tracks", "version", "isrc",
        "media_source", "media_id", "musicbrainz_release_id", "musicbrainz_release_group_id",
        "musicbrainz_release_track_id", "album_type", "secondary_types", *MUSIC_CREDIT_FIELDS,
    )
    meta.field_sources = {key: "tag" for key in tag_fields if getattr(meta, key) not in (None, "", [], {})}
    for key in ("bit_depth", "sample_rate", "bitrate", "duration"):
        if getattr(meta, key) is not None:
            meta.field_sources[key] = "stream"
    info = getattr(audio, "info", None)
    detected = next((name for kind, name in (
        (FLAC, "FLAC"), (MP3, "MP3"), (WAVE, "WAV"), (AIFF, "AIFF"),
        (MonkeysAudio, "APE"), (DSF, "DSD"), (DSDIFF, "DSD"),
    ) if isinstance(audio, kind)), None)
    if isinstance(audio, ASF):
        _mark_asf_quality(meta, audio)
    elif detected:
        meta.apply_audio_quality(detected, overwrite=True, evidence_source="stream")
    elif meta.audio_format:
        source = "stream" if getattr(info, "codec", None) or getattr(info, "codec_description", None) else "filename"
        meta.field_sources.update(audio_format=source, audio_lossless=source)
    return meta


class AudioMetadataHelper:
    """读取和写入音频标签，并转换为标准音乐元数据。"""

    @classmethod
    def read(cls, path: Path) -> MetaMusic:
        """读取本地音频标签，并以完整文件名模式和目录线索补充缺失字段。"""
        tag_meta = cls.read_tags(path)
        meta = tag_meta.apply_path_context(path) if tag_meta else cls.read_filename(path)
        return _apply_audio_cue(path, meta)

    @classmethod
    def read_evidence(
            cls,
            path: Path,
    ) -> tuple[MetaMusic, Optional[MetaMusic], MetaMusic]:
        """分别返回合并元数据、纯标签元数据和纯文件名元数据。"""
        filename_meta = cls.read_filename(path)
        tag_meta = cls.read_tags(path) if path.exists() and path.is_file() else None
        if not tag_meta:
            return _apply_audio_cue(path, MetaMusic.from_dict(filename_meta.to_dict())), None, filename_meta
        merged_meta = MetaMusic.from_dict(tag_meta.to_dict()).apply_path_context(path)
        return _apply_audio_cue(path, merged_meta), tag_meta, filename_meta

    @classmethod
    def read_many(cls, paths: list[Path]) -> list[MetaMusic]:
        """批量读取一组音频路径的标签与文件名元数据。"""
        return [cls.read(path) for path in paths]

    @staticmethod
    def with_cue_context(path: Path, meta: MetaMusic) -> MetaMusic:
        """在隔离副本上附加 CUE 事实，批次缓存的纯标签不能被路径或索引污染。"""
        return _apply_audio_cue(path, MetaMusic.from_dict(meta.to_dict()).apply_path_context(path))

    @classmethod
    def read_tags(cls, path: Path) -> Optional[MetaMusic]:
        """只读取本地音频标签和流参数，不使用文件名或目录补齐。"""
        scan, signature = _audio_scan.get(), _audio_signature(path)
        if scan is None or not scan.active or signature is None:
            return cls._read_tags(path)
        with scan.lock:
            if not scan.active:
                return cls._read_tags(path)
            if signature in scan.tags:
                scan.tags.move_to_end(signature)
                return deepcopy(scan.tags[signature])
            meta = cls._read_tags(path)
            if signature == _audio_signature(path):
                scan.tags[signature] = deepcopy(meta)
                while len(scan.tags) > 1024:
                    scan.tags.popitem(last=False)
            return meta

    @classmethod
    def _read_tags(cls, path: Path) -> Optional[MetaMusic]:
        """解析实际容器的原生标签；扫描缓存只包裹此只读操作。"""
        try:
            audio = cls._open_audio(path)
        except Exception as err:
            logger.warning(f"读取音频标签失败：{path} - {err}")
            return None
        # FileType 的真值取决于标签数量；无标签的有效音频仍包含整专对位所需的时长。
        if audio is None:
            return None

        tags = cls._readable_tags(audio.tags)
        track_number, total_tracks = cls._number_pair(
            cls._first_of(tags, "tracknumber", "track"),
            cls._first_of(tags, "tracktotal", "totaltracks"),
        )
        disc_number, total_discs = cls._number_pair(
            cls._first_of(tags, "discnumber", "disc"),
            cls._first_of(tags, "disctotal", "totaldiscs"),
        )
        musicbrainz_id = cls._normalize_musicbrainz_id(
            cls._first_of(
                tags,
                "musicbrainz_trackid",
                "musicbrainz_recordingid",
                "musicbrainz_track_id",
                "musicbrainz_recording_id",
            )
        )
        info = getattr(audio, "info", None)
        original_year = cls._year(cls._first_of(tags, "originaldate", "originalyear"))
        release_year = cls._year(cls._first_of(tags, "date", "year"))
        release_types = [value for key in ("releasetype", "musicbrainz_albumtype", "musicbrainz_album_type")
                         for value in cls._values(tags, key)]
        album_type, secondary_types = parse_music_release_types(release_types, cls._first(tags, "compilation"))
        meta = MetaMusic(
            **cls._read_credits(tags),
            org_string=path.name,
            title=cls._first(tags, "title"),
            artists=cls._values(tags, "artist"),
            album=cls._first(tags, "album"),
            album_artist=cls._first_of(tags, "albumartist", "album artist"),
            album_type=album_type,
            secondary_types=secondary_types,
            year=release_year or original_year,
            original_year=original_year,
            release_year=release_year,
            disc_number=disc_number,
            track_number=track_number,
            total_discs=total_discs,
            total_tracks=total_tracks,
            version=cls._first(tags, "version") or cls._first(tags, "subtitle"),
            audio_format=cls._audio_format(path, info),
            bit_depth=cls._optional_int(getattr(info, "bits_per_sample", None)),
            sample_rate=cls._optional_int(getattr(info, "sample_rate", None)),
            bitrate=cls._optional_int(getattr(info, "bitrate", None)),
            duration=round(info.length) if info and getattr(info, "length", None) else None,
            isrc=cls._first(tags, "isrc"),
            media_source=MediaSource.MusicBrainz if musicbrainz_id else None,
            media_id=musicbrainz_id,
            music_type=MUSIC_ENTITY_RECORDING,
            musicbrainz_release_id=cls._normalize_musicbrainz_id(
                cls._first_of(tags, "musicbrainz_albumid", "musicbrainz_album_id")),
            musicbrainz_release_group_id=cls._normalize_musicbrainz_id(
                cls._first_of(tags, "musicbrainz_releasegroupid", "musicbrainz_release_group_id")),
            musicbrainz_release_track_id=cls._normalize_musicbrainz_id(
                cls._first_of(tags, "musicbrainz_releasetrackid", "musicbrainz_release_track_id")),
        )
        return _mark_audio_evidence(meta, audio)

    @staticmethod
    def _open_audio(path: Path) -> Any:
        """读取实际容器；错误扩展名重试不带后缀提示，保存时必须明确传入目标路径。"""
        try:
            return MutagenFile(path, easy=False)
        except MutagenError:
            with path.open("rb") as stream:
                return MutagenFile(fileobj=stream, filename="audio", easy=False)

    @staticmethod
    def _readable_tags(tags: Any) -> Any:
        """将原生ID3/MP4/ASF及Vorbis/APEv2映射到统一键，不修改音频。

        原生读取可保留 Easy 包装未注册的发行字段；Recording 的身份只取
        专用字段，不能误用 Release Track ID。
        """
        if isinstance(tags, MP4Tags):
            return _read_mp4_tags(tags)
        if isinstance(tags, ASFTags):
            return _read_asf_tags(tags)
        if isinstance(tags, APEv2):
            values = {key.casefold(): value for key, value in tags.items()}
            values.update({key.replace(" ", "_"): value for key, value in list(values.items())})
            return values
        if not isinstance(tags, ID3):
            return tags or {}
        fields = {
            "title": "TIT2", "artist": "TPE1", "album": "TALB",
            "albumartist": "TPE2", "date": "TDRC", "originaldate": "TDOR",
            "tracknumber": "TRCK", "discnumber": "TPOS", "isrc": "TSRC",
            "subtitle": "TIT3", "compilation": "TCMP", "composer": "TCOM", "conductor": "TPE3",
        }
        values = {
            key: [str(value) for frame in tags.getall(frame_id) for value in frame.text]
            for key, frame_id in fields.items()
        }
        for frame in tags.getall("TXXX"):
            key = str(frame.desc).casefold().replace(" ", "_")
            if not values.get(key):
                values[key] = [str(value) for value in frame.text]
        for key, people in _id3_credit_values(tags).items():
            values[key] = list(dict.fromkeys([*values.get(key, []), *people]))
        for frame in tags.getall("UFID"):
            if frame.owner == "http://musicbrainz.org":
                values["musicbrainz_trackid"] = [frame.data.decode("ascii", errors="replace")]
                break
        return values

    @classmethod
    def _read_credits(cls, tags: Any) -> dict[str, Any]:
        """把容器中的明确古典角色投影为独立字段，演奏乐器保留为映射键。"""
        performers: dict[str, list[str]] = {}
        for value in cls._values(tags, "performer"):
            role, name = _performer_pair(value)
            performers.setdefault(role, []).append(name)
        for key in tags.keys():
            if str(key).casefold().startswith("performer:"):
                performers.setdefault(str(key).split(":", 1)[1], []).extend(cls._values(tags, key))
        return music_credit_values(dict(composers=cls._values(tags, "composer"), conductors=cls._values(tags, "conductor"),
                                       orchestras=[*cls._values(tags, "orchestra"), *cls._values(tags, "ensemble")], performers=performers))

    @classmethod
    def read_lyrics(cls, path: Path) -> Optional[MusicLyrics]:
        """读取常见音频容器中的逐行同步歌词、纯文本歌词和 Lyricsfile 标签。"""
        try:
            audio = MutagenFile(path, easy=False)
        except Exception as err:
            logger.warning(f"读取内嵌歌词失败：{path} - {err}")
            return None
        if not audio or not audio.tags:
            return None
        tags = audio.tags
        synced = None
        plain = None
        lyricsfile = None

        getall = getattr(tags, "getall", None)
        if callable(getall):
            synced_frames = getall("SYLT")
            plain_frames = getall("USLT")
            synced = cls._sylt_to_lrc(synced_frames[0]) if synced_frames else None
            plain = str(plain_frames[0].text or "").strip() if plain_frames else None

        normalized = {
            str(key).casefold(): value
            for key, value in getattr(tags, "items", lambda: [])()
        }
        if isinstance(tags, ASFTags):
            normalized.update(_read_asf_tags(tags))
        synced = synced or cls._tag_text(
            normalized,
            "syncedlyrics",
            "synced lyrics",
            "lyrics_synced",
        )
        plain = plain or cls._tag_text(
            normalized,
            "lyrics",
            "unsyncedlyrics",
            "unsynced lyrics",
            "©lyr",
            "\xa9lyr",
        )
        lyricsfile = cls._tag_text(normalized, "lyricsfile", "lyricsfile.yaml")
        if not synced and not plain and not lyricsfile:
            return None
        return MusicLyrics(
            provider="embedded",
            plain_lyrics=plain,
            synced_lyrics=synced,
            lyricsfile=lyricsfile,
            match_score=100,
            provider_priority=100,
        )

    @staticmethod
    def _tag_text(tags: dict[str, Any], *keys: str) -> Optional[str]:
        """从不同容器的单值或列表标签中提取首个非空文本。"""
        for key in keys:
            value = tags.get(key.casefold())
            if isinstance(value, (list, tuple)) and value:
                value = value[0]
            if isinstance(value, (USLT, SYLT)):
                value = getattr(value, "text", None)
            text = str(value or "").strip()
            if text:
                return text
        return None

    @staticmethod
    def _sylt_to_lrc(frame: SYLT) -> Optional[str]:
        """把 ID3 SYLT 的毫秒时间戳转换为通用 LRC 行。"""
        output = []
        for text, timestamp in frame.text or []:
            if not str(text or "").strip():
                continue
            minutes, remainder = divmod(max(int(timestamp), 0), 60000)
            output.append(f"[{minutes:02d}:{remainder / 1000:05.2f}]{str(text).strip()}")
        return "\n".join(output) or None

    @staticmethod
    def read_filename(path: Path) -> MetaMusic:
        """只从文件名和目录结构解析音乐元数据。"""
        return MetaMusic(
            org_string=path.name,
            title=path.stem,
            audio_format=path.suffix.lstrip(".").upper() or None,
            field_sources={"audio_format": "filename", "audio_lossless": "filename"} if path.suffix else None,
        ).apply_path_context(path)

    @classmethod
    def write(
            cls,
            path: Path,
            music: Union[MetaMusic, MusicInfo],
            cover_data: Optional[bytes] = None,
            cover_mime: str = "image/jpeg",
            overwrite: bool = True,
            write_tags: bool = True,
            cover_overwrite: Optional[bool] = None,
    ) -> bool:
        """按独立策略写入标签与封面；链接目标仅在独立副本写入成功后替换。"""
        if not write_tags and not cover_data:
            return True
        try:
            audio = cls._open_audio(path)
            if audio is None:
                raise ValueError("文件不是受支持的音频")
            needs_tags = write_tags and cls._needs_tag_write(audio, music, overwrite)
            overwrite_cover = overwrite if cover_overwrite is None else cover_overwrite
            needs_cover = bool(cover_data) and (overwrite_cover or not cls._has_cover(audio))
            if not needs_tags and not needs_cover:
                return True
            with _audio_write_target(path) as write_path:
                audio = cls._open_audio(write_path)
                if audio is None:
                    raise ValueError("文件不是受支持的音频")
                if needs_tags:
                    cls._write_tag_values(audio, music, overwrite, write_path)
                if needs_cover and cover_data is not None:
                    cls._write_cover(
                        path=write_path,
                        cover_data=cover_data,
                        cover_mime=cover_mime,
                        overwrite=overwrite_cover,
                    )
            return True
        except Exception as err:
            logger.warning(f"写入音频标签失败：{path} - {err}")
            return False

    @classmethod
    def _needs_tag_write(cls, audio: Any, music: Union[MetaMusic, MusicInfo], overwrite: bool) -> bool:
        """先只读比较已有标签，完整元数据不应触发大音频文件的副本创建。"""
        return bool(cls._tag_updates(audio, music, overwrite))

    @classmethod
    def _tag_updates(cls, audio: Any, music: Union[MetaMusic, MusicInfo], overwrite: bool) -> dict[str, list[str]]:
        """预检和实际写入共用字段差异，年份模型不能抹掉标签中同年的完整日期。"""
        tags = cls._readable_tags(audio.tags)
        credits = _credit_tag_values(cls._read_credits(tags))
        aliases = {"albumartist": ("albumartist", "album artist"), "date": ("date", "year"),
                   "originaldate": ("originaldate", "originalyear"),
                   "tracknumber": ("tracknumber", "track"), "discnumber": ("discnumber", "disc"),
                   "musicbrainz_trackid": ("musicbrainz_trackid", "musicbrainz_track_id"),
                   "musicbrainz_albumid": ("musicbrainz_albumid", "musicbrainz_album_id"),
                   "musicbrainz_releasegroupid": ("musicbrainz_releasegroupid", "musicbrainz_release_group_id"),
                   "musicbrainz_releasetrackid": ("musicbrainz_releasetrackid", "musicbrainz_release_track_id")}
        totals = {"tracknumber": ("tracktotal", "totaltracks"), "discnumber": ("disctotal", "totaldiscs")}
        updates = {}
        for key, value in cls._tag_values(music).items():
            if value in (None, "", []):
                continue
            current = next((items for alias in aliases.get(key, (key,)) if (items := cls._values(tags, alias))), [])
            expected = value if isinstance(value, list) else [str(value)]
            if key in credits:
                current = credits[key]
                if set(current) == set(expected):
                    continue
            if current and key in totals and cls._number_pair(
                    current[0], cls._first_of(tags, *totals[key]),
            ) == cls._number_pair(expected[0]):
                # 组合与独立总数按相同位置语义比较，避免为等价标签复制整个音频。
                continue
            if key in {"date", "originaldate"} and current and len(expected[0]) == 4 and current[0].startswith(f"{expected[0]}-"):
                continue
            if not current or (overwrite and current != expected):
                updates[key] = expected
        return updates

    @staticmethod
    def _has_cover(audio: Any) -> bool:
        """按现有封面支持范围判定缺失策略，避免为已存在的图片复制整段音频。"""
        if isinstance(audio, FLAC):
            return bool(audio.pictures)
        tags = getattr(audio, "tags", None)
        if tags is None:
            return False
        if isinstance(audio, MonkeysAudio):
            return bool(tags.get("Cover Art (Front)"))
        if isinstance(audio, MP4):
            return bool(tags.get("covr"))
        if isinstance(audio, ASF):
            return bool(tags.get("WM/Picture"))
        return bool(hasattr(tags, "getall") and tags.getall("APIC"))

    @classmethod
    def _write_tag_values(cls, audio: Any, music: Union[MetaMusic, MusicInfo], overwrite: bool, path: Path) -> None:
        """按标签策略保存可支持的字段；空标签音频仍然是可写的有效容器。"""
        if audio.tags is None:
            audio.add_tags()
        values = cls._tag_updates(audio, music, overwrite)
        if isinstance(audio.tags, ID3):
            changed = cls._write_id3_tags(audio.tags, values, overwrite)
        elif isinstance(audio.tags, MP4Tags):
            changed = cls._write_mp4_tags(audio.tags, values, overwrite)
        elif isinstance(audio.tags, ASFTags):
            changed = _write_asf_tags(audio.tags, values)
        else:
            changed = cls._write_text_tags(audio, values, overwrite, path)
        if changed:
            audio.save(path)

    @classmethod
    def _write_text_tags(cls, audio: Any, values: dict[str, list[str]], overwrite: bool, path: Path) -> bool:
        """写入Vorbis/APEv2文本字段，并使用APE播放器通用的字段名。"""
        # APEv2的MusicBrainz字段沿用下划线名称，不能套用ID3 TXXX描述中的空格。
        ape_fields = {"albumartist": "Album Artist", "date": "Year", "originaldate": "Originalyear",
                      "tracknumber": "Track", "discnumber": "Disc"}
        changed = False
        if overwrite:
            for key in list(audio.tags.keys()):
                normalized = str(key).casefold()
                if ("performer" in values and normalized.startswith("performer:")) or ("orchestra" in values and normalized == "ensemble"):
                    del audio.tags[key]
                    changed = True
        for name, value in values.items():
            key = ape_fields.get(name, name) if isinstance(audio.tags, APEv2) else name
            if cls._values(audio.tags, key) == value or (not overwrite and audio.tags.get(key)):
                continue
            try:
                audio[key] = value
                changed = True
            except (KeyError, TypeError, ValueError) as err:
                logger.debug(f"音频格式不支持标签 {key}：{path} - {err}")
        return changed

    @staticmethod
    def _write_id3_tags(tags: ID3, values: dict[str, list[str]], overwrite: bool) -> bool:
        """按原生ID3帧写入WAV/DSF/MP3等容器，不把Easy字段名当作ID3帧名。"""
        fields = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
                  "date": "TDRC", "originaldate": "TDOR", "tracknumber": "TRCK", "discnumber": "TPOS", "isrc": "TSRC",
                  "composer": "TCOM", "conductor": "TPE3"}
        custom = {"musicbrainz_albumid": "MusicBrainz Album Id", "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
                  "musicbrainz_releasetrackid": "MusicBrainz Release Track Id", "musicbrainz_albumtype": "MusicBrainz Album Type",
                  "orchestra": "ORCHESTRA"}
        changed = False
        if overwrite:
            for key in values.keys() & {"composer", "conductor", "orchestra", "performer"}:
                changed = _remove_id3_credit_aliases(tags, key) or changed
        for key, value in values.items():
            if key == "musicbrainz_trackid":
                frame = UFID(owner="http://musicbrainz.org", data=value[0].encode("ascii"))
            elif key == "performer":
                frame = TMCL(encoding=3, people=[list(_performer_pair(item)) for item in value])
            elif key in fields:
                frame = Frames[fields[key]](encoding=3, text=value)
            elif key in custom:
                frame = TXXX(encoding=3, desc=custom[key], text=value)
            else:
                continue
            if tags.get(frame.HashKey) != frame and (overwrite or not tags.get(frame.HashKey)):
                tags.add(frame)
                changed = True
        return changed

    @classmethod
    def _write_mp4_tags(cls, tags: MP4Tags, values: dict[str, list[str]], overwrite: bool) -> bool:
        """写入MP4标准atom和文本freeform，保留曲序/总数及各类发行身份。"""
        atoms = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART", "date": "\xa9day", "composer": "\xa9wrt"}
        custom = {"musicbrainz_trackid": "MusicBrainz Track Id", "musicbrainz_albumid": "MusicBrainz Album Id",
                  "musicbrainz_releasegroupid": "MusicBrainz Release Group Id", "musicbrainz_releasetrackid": "MusicBrainz Release Track Id",
                  "musicbrainz_albumtype": "MusicBrainz Album Type", "originaldate": "ORIGINALDATE", "isrc": "ISRC",
                  "conductor": "CONDUCTOR", "orchestra": "ORCHESTRA", "performer": "PERFORMER"}
        changed = _remove_mp4_credit_aliases(tags, values) if overwrite else False
        encoded: list[str] | list[tuple[int, int]] | list[bytes]
        for key, value in values.items():
            if key in atoms:
                atom, encoded = atoms[key], value
            elif key in {"tracknumber", "discnumber"}:
                current, total = cls._number_pair(value[0])
                atom, encoded = ("trkn" if key == "tracknumber" else "disk"), [(current or 0, total or 0)]
            elif key in custom:
                atom = f"----:com.apple.iTunes:{custom[key]}"
                encoded = [item.encode("utf-8") for item in value]
            else:
                continue
            if tags.get(atom) != encoded and (overwrite or not tags.get(atom)):
                tags[atom] = encoded
                changed = True
        return changed

    @classmethod
    def _tag_values(cls, music: Union[MetaMusic, MusicInfo]) -> dict[str, Any]:
        """把标准音乐对象转换为容器无关的文本字段，身份与年份保持各自语义。"""
        track_number = cls._number_text(
            getattr(music, "track_number", None),
            getattr(music, "total_tracks", None),
        )
        disc_number = cls._number_text(
            getattr(music, "disc_number", None),
            getattr(music, "total_discs", None),
        )
        original_year = getattr(music, "original_year", None)
        release_year = getattr(music, "release_year", None)
        year = getattr(music, "year", None)
        # 来源只确认首发年份时写ORIGINALDATE，不能把展示year伪装成当前发行日期。
        date = release_year or (year if not original_year or str(year) != str(original_year) else None)
        credits = music_credit_values(music)
        return {
            **_credit_tag_values(credits),
            "title": getattr(music, "title", None),
            "artist": list(getattr(music, "artists", None) or []),
            "album": getattr(music, "album", None),
            "albumartist": getattr(music, "album_artist", None),
            "date": date,
            "originaldate": original_year,
            "tracknumber": track_number,
            "discnumber": disc_number,
            "isrc": getattr(music, "isrc", None),
            "musicbrainz_trackid": cls._musicbrainz_recording_id(music),
            "musicbrainz_albumid": cls._normalize_musicbrainz_id(getattr(music, "musicbrainz_release_id", None)),
            "musicbrainz_releasegroupid": cls._normalize_musicbrainz_id(getattr(music, "musicbrainz_release_group_id", None)),
            "musicbrainz_releasetrackid": cls._normalize_musicbrainz_id(getattr(music, "musicbrainz_release_track_id", None)),
        }

    @staticmethod
    def _musicbrainz_recording_id(
            music: Union[MetaMusic, MusicInfo],
    ) -> Optional[str]:
        """仅将 MusicBrainz 单曲身份写入 recording 标签，避免误写专辑 ID。"""
        if (
                getattr(music, "media_source", None) == MediaSource.MusicBrainz
                and (getattr(music, "music_type", None) or MUSIC_ENTITY_RECORDING)
                == MUSIC_ENTITY_RECORDING
        ):
            media_id = getattr(music, "media_id", None)
            return AudioMetadataHelper._normalize_musicbrainz_id(media_id)
        return None

    @staticmethod
    def _number_text(current: Optional[int], total: Optional[int]) -> Optional[str]:
        """把曲序或碟号转换为常见的 current/total 标签文本。"""
        if current is None:
            return None
        return f"{current}/{total}" if total else str(current)

    @classmethod
    def _write_cover(
            cls,
            path: Path,
            cover_data: bytes,
            cover_mime: str,
            overwrite: bool,
    ) -> None:
        """按容器写入内嵌封面，链接隔离由外层写入事务统一负责。"""
        audio = cls._open_audio(path)
        if isinstance(audio, (MonkeysAudio, ASF)):
            _write_binary_cover(audio, path, cover_data, cover_mime, overwrite)
            return
        if isinstance(audio, FLAC):
            if audio.pictures and not overwrite:
                return
            picture = Picture()
            picture.type = 3
            picture.mime = cover_mime
            picture.desc = "Cover"
            picture.data = cover_data
            if overwrite:
                audio.clear_pictures()
            audio.add_picture(picture)
            audio.save(path)
            return
        if isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()
            if audio.tags.get("covr") and not overwrite:
                return
            image_format = (
                MP4Cover.FORMAT_PNG
                if cover_mime == "image/png"
                else MP4Cover.FORMAT_JPEG
            )
            audio.tags["covr"] = [MP4Cover(cover_data, imageformat=image_format)]
            audio.save(path)
            return
        if isinstance(audio, (MP3, WAVE, DSF, AIFF, DSDIFF)) and audio.tags is None:
            audio.add_tags()
        tags = getattr(audio, "tags", None)
        if tags is not None and hasattr(tags, "add"):
            if tags.getall("APIC") and not overwrite:
                return
            if overwrite:
                tags.delall("APIC")
            tags.add(
                APIC(
                    encoding=3,
                    mime=cover_mime,
                    type=3,
                    desc="Cover",
                    data=cover_data,
                )
            )
            audio.save(path)

    @staticmethod
    def _values(tags: Any, key: str) -> list[str]:
        """从容器的统一文本字段或兼容标签中提取非空字符串列表。"""
        value = tags.get(key) if hasattr(tags, "get") else None
        if value is None or isinstance(value, APEBinaryValue):
            return []
        if isinstance(value, (list, tuple, APETextValue)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    @classmethod
    def _first(cls, tags: Any, key: str) -> Optional[str]:
        """返回指定音频标签的第一个非空值。"""
        values = cls._values(tags, key)
        return values[0] if values else None

    @classmethod
    def _first_of(cls, tags: Any, *keys: str) -> Optional[str]:
        """按顺序返回多个音频标签中的第一个非空值。"""
        for key in keys:
            if value := cls._first(tags, key):
                return value
        return None

    @staticmethod
    def _normalize_musicbrainz_id(value: Optional[str]) -> Optional[str]:
        """校验并规范化音频标签中的 MusicBrainz UUID。"""
        try:
            return str(UUID(str(value)))
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _number_pair(
            value: Optional[str],
            total_value: Optional[str] = None,
    ) -> tuple[Optional[int], Optional[int]]:
        """读取组合或独立曲数/碟数，优先有效组合总数并排除非正编号。"""
        parts = str(value or "").split("/", 1)
        current = AudioMetadataHelper._positive_int(parts[0])
        total = AudioMetadataHelper._positive_int(parts[1]) if len(parts) > 1 else None
        total = total or AudioMetadataHelper._positive_int(total_value)
        return current, total

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        """曲序、碟号和总数必须为正数，零及无效文本不构成位置证据。"""
        number = AudioMetadataHelper._optional_int(value)
        return number if number is not None and number > 0 else None

    @staticmethod
    def _year(value: Optional[str]) -> Optional[int]:
        """从完整或不完整日期标签中提取四位年份。"""
        if not value:
            return None
        return AudioMetadataHelper._optional_int(str(value)[:4])

    @staticmethod
    def _audio_format(path: Path, info: Any) -> Optional[str]:
        """结合扩展名与流编码识别音频格式，区分同为 M4A 容器的 AAC 和 ALAC。"""
        codec_text = " ".join(
            str(value or "")
            for value in (
                getattr(info, "codec", None),
                getattr(info, "codec_description", None),
            )
        ).casefold()
        codec_formats = (
            (("alac", "apple lossless"), "ALAC"),
            (("aac", "mp4a"), "AAC"),
            (("opus",), "OPUS"),
            (("vorbis",), "OGG"),
            (("flac",), "FLAC"),
        )
        for markers, audio_format in codec_formats:
            if any(marker in codec_text for marker in markers):
                return audio_format
        return path.suffix.lstrip(".").upper() or None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        """将音频技术参数安全转换为整数。"""
        try:
            return int(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None
