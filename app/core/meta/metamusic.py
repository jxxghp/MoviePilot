import re
from typing import Any, Optional

from app.core.meta.metabase import MetaBase
from app.schemas.types import MediaType


_AUDIO_FORMAT_PATTERN = re.compile(
    r"(?<![A-Z])(?P<format>DSD(?:64|128|256|512)?|DSF|DFF|FLAC|ALAC|APE|WAV|WAVE|AIFF?|PCM|"
    r"MP3|AAC|M4A|OGG|VORBIS|OPUS|WMA)(?![A-Z])",
    re.IGNORECASE,
)
_BIT_DEPTH_PATTERN = re.compile(r"(?<!\d)(?P<value>16|20|24|32)\s*(?:-?bit|bits?|位)(?!\w)", re.IGNORECASE)
_SAMPLE_RATE_PATTERN = re.compile(
    r"(?<!\d)(?P<value>44(?:\.1)?|48|88(?:\.2)?|96|176(?:\.4)?|192|352(?:\.8)?|384|705(?:\.6)?|768)"
    r"\s*k(?:hz)?(?!\w)",
    re.IGNORECASE,
)
_BITRATE_PATTERN = re.compile(
    r"(?<!\d)(?P<value>\d{2,4})\s*k(?:bps?|b(?:it)?/?s?)?(?![a-z])",
    re.IGNORECASE,
)
_LOSSLESS_PATTERN = re.compile(r"(?<!\w)(?:lossless|无损|无损音质)(?!\w)", re.IGNORECASE)
_HIRES_PATTERN = re.compile(r"(?<!\w)(?:hi[ ._-]?res(?:olution)?|高解析|高分辨率音频)(?!\w)", re.IGNORECASE)

_AUDIO_FORMAT_ALIASES = {
    "WAVE": "WAV",
    "AIF": "AIFF",
    "VORBIS": "OGG",
    "M4A": "AAC",
    "DSF": "DSD",
    "DFF": "DSD",
}
_LOSSLESS_AUDIO_FORMATS = frozenset({"DSD", "FLAC", "ALAC", "APE", "WAV", "AIFF", "PCM"})
_LOSSY_AUDIO_FORMATS = frozenset({"MP3", "AAC", "OGG", "OPUS", "WMA"})


def normalize_audio_format(value: Any) -> Optional[str]:
    """将音频格式名称归一为订阅筛选和展示使用的规范值。"""
    text = str(value or "").strip().upper()
    if not text:
        return None
    match = _AUDIO_FORMAT_PATTERN.search(text)
    if not match:
        return text
    normalized = match.group("format").upper()
    if normalized.startswith("DSD"):
        return "DSD"
    return _AUDIO_FORMAT_ALIASES.get(normalized, normalized)


def infer_audio_lossless(audio_format: Any, explicit: Optional[bool] = None) -> Optional[bool]:
    """根据格式推断是否无损；显式识别结果优先于格式推断。"""
    if explicit is not None:
        return bool(explicit)
    normalized = normalize_audio_format(audio_format)
    if normalized in _LOSSLESS_AUDIO_FORMATS:
        return True
    if normalized in _LOSSY_AUDIO_FORMATS:
        return False
    return None


def parse_audio_quality(value: Any) -> dict[str, Any]:
    """从资源标题或描述中提取声明的格式、位深、采样率和码率。"""
    text = str(value or "")
    format_match = _AUDIO_FORMAT_PATTERN.search(text)
    bit_depth_match = _BIT_DEPTH_PATTERN.search(text)
    sample_rate_match = _SAMPLE_RATE_PATTERN.search(text)
    bitrate_match = _BITRATE_PATTERN.search(text)
    audio_format = normalize_audio_format(format_match.group("format")) if format_match else None
    bit_depth = int(bit_depth_match.group("value")) if bit_depth_match else None
    sample_rate = (
        int(float(sample_rate_match.group("value")) * 1000)
        if sample_rate_match
        else None
    )
    bitrate = int(bitrate_match.group("value")) * 1000 if bitrate_match else None
    explicit_lossless = True if (_LOSSLESS_PATTERN.search(text) or _HIRES_PATTERN.search(text)) else None
    return {
        "audio_format": audio_format,
        "audio_lossless": infer_audio_lossless(audio_format, explicit_lossless),
        "bit_depth": bit_depth,
        "sample_rate": sample_rate,
        "bitrate": bitrate,
    }


def audio_quality_tier(
        audio_format: Any,
        audio_lossless: Optional[bool] = None,
        bit_depth: Optional[int] = None,
        sample_rate: Optional[int] = None,
        bitrate: Optional[int] = None,
) -> Optional[str]:
    """返回 hires、lossless 或 lossy 音质等级，未知参数返回 None。"""
    normalized = normalize_audio_format(audio_format)
    lossless = infer_audio_lossless(normalized, audio_lossless)
    if normalized == "DSD" or (lossless and ((bit_depth or 0) >= 24 or (sample_rate or 0) >= 88200)):
        return "hires"
    if lossless:
        return "lossless"
    if lossless is False or normalized or bitrate:
        return "lossy"
    return None


def audio_quality_score(
        audio_format: Any,
        audio_lossless: Optional[bool] = None,
        bit_depth: Optional[int] = None,
        sample_rate: Optional[int] = None,
        bitrate: Optional[int] = None,
) -> int:
    """将音乐音质换算为 0 至 100 的稳定洗版优先级。"""
    normalized = normalize_audio_format(audio_format)
    lossless = infer_audio_lossless(normalized, audio_lossless)
    if normalized == "DSD" or (lossless and (bit_depth or 0) >= 24 and (sample_rate or 0) >= 192000):
        return 100
    if lossless:
        score = 86
        if (bit_depth or 0) >= 24:
            score += 5
        elif (bit_depth or 0) >= 16:
            score += 2
        if (sample_rate or 0) >= 176400:
            score += 7
        elif (sample_rate or 0) >= 88200:
            score += 5
        elif sample_rate:
            score += 2
        return min(score, 99)
    if bitrate:
        kbps = bitrate // 1000
        if kbps >= 320:
            return 80
        if kbps >= 256:
            return 70
        if kbps >= 192:
            return 60
        if kbps >= 128:
            return 50
        return 40
    return 35 if normalized in _LOSSY_AUDIO_FORMATS else 0


def format_audio_quality(
        audio_format: Any,
        audio_lossless: Optional[bool] = None,
        bit_depth: Optional[int] = None,
        sample_rate: Optional[int] = None,
        bitrate: Optional[int] = None,
) -> Optional[str]:
    """将音频技术参数格式化为适合识别结果和通知展示的紧凑文本。"""
    parts: list[str] = []
    normalized = normalize_audio_format(audio_format)
    if normalized:
        parts.append(normalized)
    if bit_depth:
        parts.append(f"{bit_depth}-bit")
    if sample_rate:
        rate = sample_rate / 1000
        parts.append(f"{rate:g} kHz")
    if bitrate:
        parts.append(f"{round(bitrate / 1000):,} kbps")
    if not parts:
        tier = audio_quality_tier(audio_format, audio_lossless, bit_depth, sample_rate, bitrate)
        if tier:
            parts.append({"hires": "Hi-Res", "lossless": "Lossless", "lossy": "Lossy"}[tier])
    return " · ".join(parts) or None


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
        audio_lossless: Optional[bool] = None,
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
        self.audio_format = normalize_audio_format(audio_format)
        self.audio_lossless = infer_audio_lossless(self.audio_format, audio_lossless)
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
    def audio_quality(self) -> Optional[str]:
        """返回 hires、lossless 或 lossy 音质等级。"""
        return audio_quality_tier(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

    @property
    def audio_quality_score(self) -> int:
        """返回订阅洗版使用的音质优先级。"""
        return audio_quality_score(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

    @property
    def audio_specs(self) -> Optional[str]:
        """返回识别结果和通知使用的格式化音频参数。"""
        return format_audio_quality(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

    def apply_audio_quality(self, value: Any, overwrite: bool = False) -> None:
        """从资源文本补充音质参数，默认保留文件标签读取到的实际值。"""
        parsed = parse_audio_quality(value)
        for key, parsed_value in parsed.items():
            if parsed_value is not None and (overwrite or getattr(self, key, None) is None):
                setattr(self, key, parsed_value)
        self.audio_format = normalize_audio_format(self.audio_format)
        self.audio_lossless = infer_audio_lossless(self.audio_format, self.audio_lossless)

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
            "audio_lossless": self.audio_lossless,
            "audio_quality": self.audio_quality,
            "audio_quality_score": self.audio_quality_score,
            "audio_specs": self.audio_specs,
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
            audio_lossless=data.get("audio_lossless"),
            bit_depth=_optional_int(data.get("bit_depth")),
            sample_rate=_optional_int(data.get("sample_rate")),
            bitrate=_optional_int(data.get("bitrate")),
            duration=_optional_int(data.get("duration")),
            isrc=data.get("isrc"),
            media_source=data.get("media_source"),
            media_id=data.get("media_id"),
        )
