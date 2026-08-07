from dataclasses import asdict, dataclass, field, fields
from typing import Any, Self

from app.schemas.types import MediaType


def _validate_music_type(value: object) -> None:
    if value in {None, MediaType.MUSIC, MediaType.MUSIC.value, "music"}:
        return
    raise ValueError(f"不支持的音乐媒体类型：{value}")


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _optional_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _init_values(model: type, data: dict[str, Any]) -> dict[str, Any]:
    init_names = {item.name for item in fields(model) if item.init}
    return {key: value for key, value in data.items() if key in init_names}


@dataclass
class MusicMeta:
    """音乐名称及音频文件解析结果。"""

    type: MediaType = field(default=MediaType.MUSIC, init=False)
    org_string: str | None = None
    title: str | None = None
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    disc_number: int | None = None
    track_number: int | None = None
    total_discs: int | None = None
    total_tracks: int | None = None
    version: str | None = None
    audio_format: str | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    bitrate: int | None = None
    duration: int | None = None
    isrc: str | None = None
    media_source: str | None = None
    media_id: str | None = None

    @property
    def name(self) -> str:
        """返回搜索和展示使用的音乐名称。"""
        return self.album or self.title or ""

    @property
    def artist(self) -> str:
        """返回兼容现有展示组件的艺术家文本。"""
        return " / ".join(self.artists)

    @property
    def season(self) -> None:
        """音乐没有季信息，兼容下载与事件链的通用访问。"""
        return None

    @property
    def begin_season(self) -> None:
        """音乐没有起始季，兼容整理作业分组。"""
        return None

    @property
    def end_season(self) -> None:
        """音乐没有结束季，兼容整理元数据比较。"""
        return None

    @property
    def begin_episode(self) -> None:
        """音乐没有起始集，兼容整理预览。"""
        return None

    @property
    def end_episode(self) -> None:
        """音乐没有结束集，兼容整理预览。"""
        return None

    @property
    def episode(self) -> None:
        """音乐没有集信息，兼容下载与历史记录的通用访问。"""
        return None

    @property
    def season_list(self) -> list[int]:
        """音乐返回空季列表，避免通用下载链访问视频专属字段。"""
        return []

    @property
    def episode_list(self) -> list[int]:
        """音乐返回空集列表，避免通用下载链访问视频专属字段。"""
        return []

    @property
    def season_episode(self) -> str:
        """音乐没有季集展示文本。"""
        return ""

    @property
    def part(self) -> None:
        """音乐不使用影视分段字段。"""
        return None

    @property
    def apply_words(self) -> list[str]:
        """音乐当前不应用影视自定义识别词。"""
        return []

    @property
    def resource_team(self) -> None:
        """音乐当前不使用影视制作组字段。"""
        return None

    @property
    def customization(self) -> None:
        """音乐当前不使用影视自定义占位符。"""
        return None

    @property
    def tmdbid(self) -> None:
        """音乐不使用 TMDB ID。"""
        return None

    @property
    def doubanid(self) -> None:
        """音乐不使用豆瓣 ID。"""
        return None

    @property
    def bangumiid(self) -> None:
        """音乐不使用 Bangumi ID。"""
        return None

    @property
    def anilistid(self) -> None:
        """音乐不使用 AniList ID。"""
        return None

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化和传输的字典。"""
        payload = asdict(self)
        payload["type"] = self.type.value
        payload["artist"] = self.artist
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复音乐解析结果。"""
        _validate_music_type(data.get("type"))
        values = _init_values(cls, data)
        values["artists"] = _string_list(values.get("artists") or data.get("artist"))
        for key in (
            "year",
            "disc_number",
            "track_number",
            "total_discs",
            "total_tracks",
            "bit_depth",
            "sample_rate",
            "bitrate",
            "duration",
        ):
            values[key] = _optional_int(values.get(key))
        return cls(**values)


@dataclass
class MusicInfo:
    """标准化音乐元数据信息。"""

    type: MediaType = field(default=MediaType.MUSIC, init=False)
    source: str | None = None
    media_id: str | None = None
    title: str | None = None
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    release_date: str | None = None
    disc_number: int | None = None
    track_number: int | None = None
    total_tracks: int | None = None
    duration: int | None = None
    isrc: str | None = None
    cover_url: str | None = None
    lyrics: str | None = None
    version: str | None = None
    category: str = ""
    names: list[str] = field(default_factory=list)
    detail_link: str | None = None
    listen_count: int | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def artist(self) -> str:
        """返回兼容现有展示组件的艺术家文本。"""
        return " / ".join(self.artists)

    @property
    def tmdb_id(self) -> None:
        """音乐不使用 TMDB ID，兼容现有下载历史字段。"""
        return None

    @property
    def imdb_id(self) -> None:
        """音乐不使用 IMDB ID，兼容现有下载历史字段。"""
        return None

    @property
    def tvdb_id(self) -> None:
        """音乐不使用 TVDB ID，兼容现有下载历史字段。"""
        return None

    @property
    def douban_id(self) -> None:
        """音乐不使用豆瓣 ID，兼容现有下载历史字段。"""
        return None

    @property
    def bangumi_id(self) -> None:
        """音乐不使用 Bangumi ID，兼容现有下载历史字段。"""
        return None

    @property
    def anilist_id(self) -> None:
        """音乐不使用 AniList ID，兼容现有下载历史字段。"""
        return None

    @property
    def episode_group(self) -> None:
        """音乐没有剧集组，兼容现有下载历史字段。"""
        return None

    @property
    def season(self) -> None:
        """音乐没有季信息，兼容失败冷却和目录逻辑。"""
        return None

    @property
    def vote_average(self) -> float:
        """音乐当前没有评分字段，兼容订阅统计与持久化。"""
        return 0.0

    @property
    def overview(self) -> str:
        """返回兼容订阅描述字段的音乐摘要。"""
        parts = [self.artist, self.album, self.version]
        return " · ".join(part for part in parts if part)

    @property
    def title_year(self) -> str:
        """返回包含年份的展示标题。"""
        if not self.title:
            return ""
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def poster_path(self) -> str | None:
        """返回兼容现有媒体卡片的封面地址。"""
        return self.cover_url

    @property
    def backdrop_path(self) -> str | None:
        """返回兼容现有下载卡片的背景地址。"""
        return self.cover_url

    def get_message_image(self, default: bool | None = None) -> str | None:
        """返回通知消息使用的音乐封面。"""
        return self.cover_url

    def get_poster_image(self, default: bool | None = None) -> str | None:
        """返回海报位使用的音乐封面。"""
        return self.cover_url

    def get_backdrop_image(self, default: bool = False) -> str | None:
        """返回背景图位使用的音乐封面。"""
        return self.cover_url

    def clear(self) -> None:
        """清理不参与队列展示和持久化的上游原始响应。"""
        self.raw_data.clear()

    def to_dict(self) -> dict[str, Any]:
        """转换为兼容现有 Context 外层结构的字典。"""
        payload = asdict(self)
        payload.update(
            {
                "type": self.type.value,
                "artist": self.artist,
                "title_year": self.title_year,
                "poster_path": self.poster_path,
                "backdrop_path": self.backdrop_path,
                "mediaid_prefix": self.source,
                "overview": self.overview,
                "vote_average": self.vote_average,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复标准化音乐元数据。"""
        _validate_music_type(data.get("type"))
        values = _init_values(cls, data)
        values["artists"] = _string_list(values.get("artists") or data.get("artist"))
        values["names"] = _string_list(values.get("names"))
        values["raw_data"] = dict(values.get("raw_data") or {})
        for key in (
            "year",
            "disc_number",
            "track_number",
            "total_tracks",
            "duration",
            "listen_count",
        ):
            values[key] = _optional_int(values.get(key))
        return cls(**values)
