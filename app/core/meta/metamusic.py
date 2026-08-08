from typing import Any, Optional

from app.core.meta.metabase import MetaBase
from app.schemas.types import MediaType


def _optional_int(value: Any) -> Optional[int]:
    """将音频技术参数安全转换为整数，空值与非数字返回 None。"""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    """将标签原始值归一为非空字符串列表，兼容单值、列表与逗号分隔。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)]


class MetaMusic(MetaBase):
    """音乐文件名及音频标签解析结果，作为 MetaBase 的音乐分支实现。"""

    def __init__(
        self,
        org_string: Optional[str] = None,
        title: Optional[str] = None,
        artists: Optional[list[str]] = None,
        album: Optional[str] = None,
        album_artist: Optional[str] = None,
        year: Optional[int] = None,
        disc_number: Optional[int] = None,
        track_number: Optional[int] = None,
        total_discs: Optional[int] = None,
        total_tracks: Optional[int] = None,
        version: Optional[str] = None,
        audio_format: Optional[str] = None,
        bit_depth: Optional[int] = None,
        sample_rate: Optional[int] = None,
        bitrate: Optional[int] = None,
        duration: Optional[int] = None,
        isrc: Optional[str] = None,
        media_source: Optional[str] = None,
        media_id: Optional[str] = None,
    ):
        # 音乐无季集概念，仅复用 MetaBase 的基础字段初始化，不触发副标题季集识别
        super().__init__(title or org_string or "")
        self.type = MediaType.MUSIC
        self.org_string = org_string
        self.title = title
        self.artists = list(artists) if artists else []
        self.album = album
        self.album_artist = album_artist
        self.year = year
        self.disc_number = disc_number
        self.track_number = track_number
        self.total_discs = total_discs
        self.total_tracks = total_tracks
        self.version = version
        self.audio_format = audio_format
        self.bit_depth = bit_depth
        self.sample_rate = sample_rate
        self.bitrate = bitrate
        self.duration = duration
        self.isrc = isrc
        self.media_source = media_source
        self.media_id = media_id

    @property
    def name(self) -> str:
        """返回搜索和展示使用的音乐名称，优先专辑名其次标题。"""
        return self.album or self.title or ""

    @name.setter
    def name(self, value: Optional[str]) -> None:
        """辅助识别链回写标题时落到 title 字段，保持音乐名称可写。"""
        self.title = value or None

    @property
    def original_name(self) -> str:
        """返回未经过通用识别词处理的原始名称，兼容影视识别链的公共访问。"""
        return self.org_string or self.title or self.album or ""

    @property
    def artist(self) -> str:
        """返回兼容现有展示组件的艺术家文本。"""
        return " / ".join(self.artists)

    @property
    def season(self) -> None:
        """音乐没有季信息，兼容下载与事件链的通用访问。"""
        return None

    @property
    def episode(self) -> None:
        """音乐没有集信息，兼容下载与历史记录的通用访问。"""
        return None

    @property
    def apply_words(self) -> list[str]:
        """音乐当前不应用影视自定义识别词。"""
        return []

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化和传输的字典，字段集与 schemas.MusicMeta 对齐。"""
        return {
            "type": self.type.value,
            "org_string": self.org_string,
            "title": self.title,
            "artists": list(self.artists),
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "year": self.year,
            "disc_number": self.disc_number,
            "track_number": self.track_number,
            "total_discs": self.total_discs,
            "total_tracks": self.total_tracks,
            "version": self.version,
            "audio_format": self.audio_format,
            "bit_depth": self.bit_depth,
            "sample_rate": self.sample_rate,
            "bitrate": self.bitrate,
            "duration": self.duration,
            "isrc": self.isrc,
            "media_source": self.media_source,
            "media_id": self.media_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetaMusic":
        """从字典恢复音乐解析结果，兼容 artists/artist 两种键。"""
        raw_type = data.get("type")
        if raw_type not in (None, MediaType.MUSIC, MediaType.MUSIC.value, "music"):
            raise ValueError(f"不支持的音乐媒体类型：{raw_type}")
        return cls(
            org_string=data.get("org_string"),
            title=data.get("title"),
            artists=_string_list(data.get("artists") or data.get("artist")),
            album=data.get("album"),
            album_artist=data.get("album_artist"),
            year=_optional_int(data.get("year")),
            disc_number=_optional_int(data.get("disc_number")),
            track_number=_optional_int(data.get("track_number")),
            total_discs=_optional_int(data.get("total_discs")),
            total_tracks=_optional_int(data.get("total_tracks")),
            version=data.get("version"),
            audio_format=data.get("audio_format"),
            bit_depth=_optional_int(data.get("bit_depth")),
            sample_rate=_optional_int(data.get("sample_rate")),
            bitrate=_optional_int(data.get("bitrate")),
            duration=_optional_int(data.get("duration")),
            isrc=data.get("isrc"),
            media_source=data.get("media_source"),
            media_id=data.get("media_id"),
        )
