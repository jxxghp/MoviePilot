from pathlib import Path
from typing import Any, Optional, Union

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC
from mutagen.mp4 import MP4, MP4Cover

from app.core.context import MusicInfo
from app.core.meta import MetaMusic
from app.log import logger


class AudioMetadataHelper:
    """读取和写入音频标签，并转换为标准音乐元数据。"""

    @classmethod
    def read(cls, path: Path) -> MetaMusic:
        """读取本地音频标签，并以完整文件名模式和目录线索补充缺失字段。"""
        def filename_fallback() -> MetaMusic:
            """构造无标签结果，完整文件名解析只在确有需要时执行。"""
            return MetaMusic(
                org_string=path.name,
                title=path.stem,
                audio_format=path.suffix.lstrip(".").upper() or None,
            ).apply_path_context(path)

        try:
            audio = MutagenFile(path, easy=True)
        except Exception as err:
            logger.warning(f"读取音频标签失败：{path} - {err}")
            return filename_fallback()
        if not audio:
            return filename_fallback()

        tags = audio.tags or {}
        track_number, total_tracks = cls._number_pair(cls._first(tags, "tracknumber"))
        disc_number, total_discs = cls._number_pair(cls._first(tags, "discnumber"))
        info = getattr(audio, "info", None)
        return MetaMusic(
            org_string=path.name,
            title=cls._first(tags, "title") or path.stem,
            artists=cls._values(tags, "artist"),
            album=cls._first(tags, "album"),
            album_artist=cls._first(tags, "albumartist"),
            year=cls._year(cls._first(tags, "date") or cls._first(tags, "originaldate")),
            disc_number=disc_number,
            track_number=track_number,
            total_discs=total_discs,
            total_tracks=total_tracks,
            version=cls._first(tags, "version") or cls._first(tags, "subtitle"),
            audio_format=path.suffix.lstrip(".").upper() or None,
            bit_depth=cls._optional_int(getattr(info, "bits_per_sample", None)),
            sample_rate=cls._optional_int(getattr(info, "sample_rate", None)),
            bitrate=cls._optional_int(getattr(info, "bitrate", None)),
            duration=round(info.length) if info and getattr(info, "length", None) else None,
            isrc=cls._first(tags, "isrc"),
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
        }

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
        """为 MP3、FLAC 和 MP4/M4A 写入内嵌封面，其它格式保留标签写入结果。"""
        audio = MutagenFile(path)
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
    def _optional_int(value: Any) -> Optional[int]:
        """将音频技术参数安全转换为整数。"""
        try:
            return int(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None
