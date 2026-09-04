from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID

from mutagen import File as MutagenFile
from mutagen.apev2 import APEBinaryValue
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, SYLT, USLT
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp4 import MP4, MP4Cover

from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic
from app.runtime.log import logger
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource


class AudioMetadataHelper:
    """读取和写入音频标签，并转换为标准音乐元数据。"""

    @classmethod
    def read(cls, path: Path) -> MetaMusic:
        """读取本地音频标签，并以完整文件名模式和目录线索补充缺失字段。"""
        tag_meta = cls.read_tags(path)
        if tag_meta:
            return tag_meta.apply_path_context(path)
        return cls.read_filename(path)

    @classmethod
    def read_evidence(
            cls,
            path: Path,
    ) -> tuple[MetaMusic, Optional[MetaMusic], MetaMusic]:
        """分别返回合并元数据、纯标签元数据和纯文件名元数据。"""
        filename_meta = cls.read_filename(path)
        tag_meta = cls.read_tags(path) if path.exists() and path.is_file() else None
        if not tag_meta:
            return filename_meta, None, filename_meta
        merged_meta = MetaMusic.from_dict(tag_meta.to_dict()).apply_path_context(path)
        return merged_meta, tag_meta, filename_meta

    @classmethod
    def read_many(cls, paths: list[Path]) -> list[MetaMusic]:
        """批量读取一组音频路径的标签与文件名元数据。"""
        return [cls.read(path) for path in paths]

    @classmethod
    def read_tags(cls, path: Path) -> Optional[MetaMusic]:
        """只读取本地音频标签和流参数，不使用文件名或目录补齐。"""
        try:
            audio = MutagenFile(path, easy=True)
        except Exception as err:
            logger.warning(f"读取音频标签失败：{path} - {err}")
            return None
        if not audio:
            return None

        tags = audio.tags or {}
        track_number, total_tracks = cls._number_pair(
            cls._first_of(tags, "tracknumber", "track")
        )
        disc_number, total_discs = cls._number_pair(
            cls._first_of(tags, "discnumber", "disc")
        )
        musicbrainz_id = cls._normalize_musicbrainz_id(
            cls._first_of(
                tags,
                "musicbrainz_trackid",
                "musicbrainz_recordingid",
            )
        )
        info = getattr(audio, "info", None)
        return MetaMusic(
            org_string=path.name,
            title=cls._first(tags, "title"),
            artists=cls._values(tags, "artist"),
            album=cls._first(tags, "album"),
            album_artist=cls._first_of(tags, "albumartist", "album artist"),
            year=cls._year(cls._first_of(tags, "date", "year", "originaldate")),
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
        )

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
                and getattr(music, "music_type", MUSIC_ENTITY_RECORDING)
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
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
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
    def _number_pair(value: Optional[str]) -> tuple[Optional[int], Optional[int]]:
        """解析 track/disc 标签中的当前编号和总数。"""
        if not value:
            return None, None
        parts = str(value).split("/", 1)
        current = AudioMetadataHelper._optional_int(parts[0])
        total = AudioMetadataHelper._optional_int(parts[1]) if len(parts) > 1 else None
        return current, total

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
