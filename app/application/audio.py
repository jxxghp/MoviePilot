import re
from dataclasses import asdict
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional, Union
from uuid import UUID

import chardet
from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.aiff import AIFF
from mutagen.apev2 import APEBinaryValue, APETextValue
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, SYLT, USLT
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4Tags
from mutagen.wave import WAVE

from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic, parse_music_release_types
from app.domain.music import MusicCueSheet, parse_music_cue
from app.runtime.log import logger
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource


def _read_cue_text(path: Path) -> str:
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
             "albumartist": "aART", "date": "\xa9day", "genre": "\xa9gen"}
    values = {key: [str(value) for value in tags.get(atom, [])] for key, atom in atoms.items()}
    for key, atom in (("tracknumber", "trkn"), ("discnumber", "disk")):
        positions = tags.get(atom) or []
        if positions:
            current, total = positions[0]
            values[key] = [f"{current}/{total}"]
    normalized = {key.casefold(): value for key, value in tags.items()}
    for name in ("MusicBrainz Track Id", "MusicBrainz Album Id", "MusicBrainz Release Group Id",
                 "MusicBrainz Release Track Id", "MusicBrainz Album Type", "ISRC", "ORIGINALDATE",
                 "ORIGINALYEAR", "VERSION", "SUBTITLE", "RELEASETYPE"):
        raw = normalized.get(f"----:com.apple.itunes:{name.casefold()}", [])
        values[name.casefold().replace(" ", "_")] = [
            value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
            for value in raw
        ]
    if "cpil" in tags:
        values["compilation"] = ["1" if tags["cpil"] else "0"]
    return values


def _mark_audio_evidence(meta: MetaMusic, audio: Any) -> MetaMusic:
    """记录实际标签和流参数的来源，名称推测不能获得标签级可信度。"""
    tag_fields = (
        "title", "artists", "album", "album_artist", "year", "original_year", "release_year",
        "disc_number", "track_number", "total_discs", "total_tracks", "version", "isrc",
        "media_source", "media_id", "musicbrainz_release_id", "musicbrainz_release_group_id",
        "musicbrainz_release_track_id", "album_type", "secondary_types",
    )
    meta.field_sources = {key: "tag" for key in tag_fields if getattr(meta, key) not in (None, "", [])}
    for key in ("bit_depth", "sample_rate", "bitrate", "duration"):
        if getattr(meta, key) is not None:
            meta.field_sources[key] = "stream"
    info = getattr(audio, "info", None)
    detected = next((name for kind, name in (
        (FLAC, "FLAC"), (MP3, "MP3"), (WAVE, "WAV"), (AIFF, "AIFF"),
        (MonkeysAudio, "APE"), (DSF, "DSD"), (DSDIFF, "DSD"),
    ) if isinstance(audio, kind)), None)
    if detected:
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
        try:
            try:
                audio = MutagenFile(path, easy=False)
            except MutagenError:
                # 错误后缀可能让 Mutagen 选错解析器；重试时保留流式读取，仅去掉扩展名提示。
                with path.open("rb") as stream:
                    audio = MutagenFile(fileobj=stream, filename="audio", easy=False)
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
    def _readable_tags(tags: Any) -> Any:
        """将原生 ID3/MP4 及 Vorbis/APEv2 映射到统一键，不修改音频。

        原生读取可保留 Easy 包装未注册的发行字段；Recording 的身份只取
        专用字段，不能误用 Release Track ID。
        """
        if isinstance(tags, MP4Tags):
            return _read_mp4_tags(tags)
        if not isinstance(tags, ID3):
            return tags or {}
        fields = {
            "title": "TIT2", "artist": "TPE1", "album": "TALB",
            "albumartist": "TPE2", "date": "TDRC", "originaldate": "TDOR",
            "tracknumber": "TRCK", "discnumber": "TPOS", "isrc": "TSRC",
            "subtitle": "TIT3", "compilation": "TCMP",
        }
        values = {
            key: [str(value) for frame in tags.getall(frame_id) for value in frame.text]
            for key, frame_id in fields.items()
        }
        for frame in tags.getall("TXXX"):
            key = str(frame.desc).casefold().replace(" ", "_")
            if not values.get(key):
                values[key] = [str(value) for value in frame.text]
        for frame in tags.getall("UFID"):
            if frame.owner == "http://musicbrainz.org":
                values["musicbrainz_trackid"] = [frame.data.decode("ascii", errors="replace")]
                break
        return values

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
        """按独立策略写入标准音乐标签，并为常见格式嵌入专辑封面。"""
        try:
            audio = MutagenFile(path, easy=True)
            if not audio:
                logger.warning(f"无法写入音频标签：{path}")
                return False
            if write_tags:
                if audio.tags is None:
                    audio.add_tags()
                for key, value in cls._tag_values(music).items():
                    if value in (None, "", []):
                        continue
                    if not overwrite and audio.tags.get(key):
                        continue
                    try:
                        audio[key] = value if isinstance(value, list) else [str(value)]
                    except (KeyError, TypeError, ValueError) as err:
                        logger.debug(f"音频格式不支持标签 {key}：{path} - {err}")
                audio.save()
            if cover_data:
                cls._write_cover(
                    path=path,
                    cover_data=cover_data,
                    cover_mime=cover_mime,
                    overwrite=(
                        overwrite
                        if cover_overwrite is None
                        else cover_overwrite
                    ),
                )
            return True
        except Exception as err:
            logger.warning(f"写入音频标签失败：{path} - {err}")
            return False

    @classmethod
    def _tag_values(cls, music: Union[MetaMusic, MusicInfo]) -> dict[str, Any]:
        """把标准音乐对象转换为 Mutagen Easy 标签字典。"""
        track_number = cls._number_text(
            getattr(music, "track_number", None),
            getattr(music, "total_tracks", None),
        )
        disc_number = cls._number_text(
            getattr(music, "disc_number", None),
            getattr(music, "total_discs", None),
        )
        return {
            "title": getattr(music, "title", None),
            "artist": list(getattr(music, "artists", None) or []),
            "album": getattr(music, "album", None),
            "albumartist": getattr(music, "album_artist", None),
            "date": getattr(music, "year", None),
            "tracknumber": track_number,
            "discnumber": disc_number,
            "isrc": getattr(music, "isrc", None),
            "musicbrainz_trackid": cls._musicbrainz_recording_id(music),
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
            return str(media_id) if media_id else None
        return None

    @staticmethod
    def _number_text(current: Optional[int], total: Optional[int]) -> Optional[str]:
        """把曲序或碟号转换为常见的 current/total 标签文本。"""
        if current is None:
            return None
        return f"{current}/{total}" if total else str(current)

    @staticmethod
    def _write_cover(
            path: Path,
            cover_data: bytes,
            cover_mime: str,
            overwrite: bool,
    ) -> None:
        """为 MP3、FLAC、MP4/M4A 和 APE 写入内嵌封面，其它格式保留标签写入结果。"""
        audio = MutagenFile(path)
        if isinstance(audio, MonkeysAudio):
            if audio.tags is None:
                audio.add_tags()
            cover_key = "Cover Art (Front)"
            if cover_key in audio.tags and not overwrite:
                return
            cover_filename = "cover.png" if cover_mime == "image/png" else "cover.jpg"
            audio.tags[cover_key] = APEBinaryValue(
                cover_filename.encode("ascii") + b"\x00" + cover_data
            )
            audio.save()
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
            audio.save()
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
            audio.save()
            return
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
            audio.save()

    @staticmethod
    def _values(tags: Any, key: str) -> list[str]:
        """从 Mutagen Easy 标签中提取非空字符串列表。"""
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
