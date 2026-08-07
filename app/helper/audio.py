from pathlib import Path
from typing import Any, Optional

from mutagen import File as MutagenFile

from app.core.music import MusicMeta
from app.log import logger


class AudioMetadataHelper:
    """读取音频标签和技术参数并转换为标准 MusicMeta。"""

    @classmethod
    def read(cls, path: Path) -> MusicMeta:
        """读取本地音频文件标签；读取失败时返回基于文件名的最小元数据。"""
        fallback = MusicMeta(
            org_string=path.name,
            title=path.stem,
            audio_format=path.suffix.lstrip(".").upper() or None,
        )
        try:
            audio = MutagenFile(path, easy=True)
        except Exception as err:
            logger.warning(f"读取音频标签失败：{path} - {err}")
            return fallback
        if not audio:
            return fallback

        tags = audio.tags or {}
        track_number, total_tracks = cls._number_pair(cls._first(tags, "tracknumber"))
        disc_number, total_discs = cls._number_pair(cls._first(tags, "discnumber"))
        info = getattr(audio, "info", None)
        return MusicMeta(
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
        )

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
