from dataclasses import asdict, dataclass, field, fields
from typing import Any, Self

from app.schemas.types import MediaType

# 音乐可浏览实体类型：单曲（Recording）、专辑（Release Group）、艺术家（Artist）
MUSIC_ENTITY_RECORDING = "recording"
MUSIC_ENTITY_ALBUM = "album"
MUSIC_ENTITY_ARTIST = "artist"


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


def _aligned_list(value: object) -> list[str]:
    """保留原始位置的字符串列表，用于与艺术家名称按下标对应的 ID 列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item or "") for item in value]
    return [str(value or "")]


def _optional_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _year_of(release_date: object) -> int | None:
    """从 MusicBrainz 可变精度日期（YYYY / YYYY-MM / YYYY-MM-DD）提取年份。"""
    text = str(release_date or "")[:4]
    return int(text) if text.isdigit() else None


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
    # 音乐实体类型，用于区分单曲、专辑和艺术家三类可浏览对象
    music_type: str = MUSIC_ENTITY_RECORDING
    title: str | None = None
    artists: list[str] = field(default_factory=list)
    # 艺术家标准 ID，顺序与 artists 一致，供详情页关联跳转
    artist_ids: list[str] = field(default_factory=list)
    album: str | None = None
    album_artist: str | None = None
    # 所属专辑标准 ID（MusicBrainz Release Group）
    album_id: str | None = None
    # 专辑主类型：Album、EP、Single 等
    album_type: str | None = None
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
    genres: list[str] = field(default_factory=list)
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
        values["artist_ids"] = _aligned_list(values.get("artist_ids"))
        values["genres"] = _string_list(values.get("genres"))
        values["names"] = _string_list(values.get("names"))
        values["music_type"] = str(values.get("music_type") or MUSIC_ENTITY_RECORDING)
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


@dataclass
class MusicRelease:
    """音乐专辑下的单个发行版本（MusicBrainz Release）。"""

    media_id: str | None = None
    title: str | None = None
    date: str | None = None
    country: str | None = None
    status: str | None = None
    packaging: str | None = None
    formats: list[str] = field(default_factory=list)
    track_count: int | None = None
    cover_url: str | None = None

    @property
    def year(self) -> int | None:
        """返回发行版本年份。"""
        return _year_of(self.date)

    def to_dict(self) -> dict[str, Any]:
        """转换为可传输的字典。"""
        payload = asdict(self)
        payload["year"] = self.year
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复发行版本信息。"""
        values = _init_values(cls, data)
        values["formats"] = _string_list(values.get("formats"))
        values["track_count"] = _optional_int(values.get("track_count"))
        return cls(**values)


@dataclass
class MusicAlbumInfo:
    """标准化音乐专辑信息（MusicBrainz Release Group）。"""

    type: MediaType = field(default=MediaType.MUSIC, init=False)
    music_type: str = field(default=MUSIC_ENTITY_ALBUM, init=False)
    source: str | None = None
    media_id: str | None = None
    title: str | None = None
    artists: list[str] = field(default_factory=list)
    artist_ids: list[str] = field(default_factory=list)
    # 专辑主类型：Album、EP、Single、Broadcast、Other
    album_type: str | None = None
    # 专辑副类型：Live、Compilation、Soundtrack、Remix 等
    secondary_types: list[str] = field(default_factory=list)
    release_date: str | None = None
    cover_url: str | None = None
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    rating: float = 0.0
    rating_votes: int | None = None
    detail_link: str | None = None
    # 专辑内的音乐，按碟号和音轨号排序
    tracks: list[MusicInfo] = field(default_factory=list)
    # 同一专辑下的其它发行版本
    releases: list[MusicRelease] = field(default_factory=list)
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def artist(self) -> str:
        """返回兼容现有展示组件的艺术家文本。"""
        return " / ".join(self.artists)

    @property
    def year(self) -> int | None:
        """返回专辑首次发行年份。"""
        return _year_of(self.release_date)

    @property
    def category(self) -> str:
        """返回专辑主类型与副类型组合成的分类文本。"""
        return " / ".join(part for part in [self.album_type, *self.secondary_types] if part)

    @property
    def track_count(self) -> int:
        """返回专辑内已解析的音乐数量。"""
        return len(self.tracks)

    @property
    def duration(self) -> int | None:
        """返回专辑内所有音乐时长之和。"""
        durations = [track.duration for track in self.tracks if track.duration]
        return sum(durations) if durations else None

    @property
    def title_year(self) -> str:
        """返回包含年份的专辑展示标题。"""
        if not self.title:
            return ""
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def poster_path(self) -> str | None:
        """返回兼容现有媒体卡片的封面地址。"""
        return self.cover_url

    @property
    def backdrop_path(self) -> str | None:
        """返回兼容现有详情页背景的封面地址。"""
        return self.cover_url

    @property
    def overview(self) -> str:
        """返回专辑摘要，供卡片和通知复用。"""
        parts = [self.artist, self.category, self.release_date, " / ".join(self.genres[:3])]
        return " · ".join(part for part in parts if part)

    def to_dict(self) -> dict[str, Any]:
        """转换为兼容前端 MediaInfo 结构的字典。"""
        payload = {
            key: value
            for key, value in asdict(self).items()
            if key not in {"tracks", "releases", "type"}
        }
        payload.update(
            {
                "type": self.type.value,
                "artist": self.artist,
                "album": self.title,
                "year": self.year,
                "category": self.category,
                "duration": self.duration,
                "total_tracks": self.track_count,
                "title_year": self.title_year,
                "poster_path": self.poster_path,
                "backdrop_path": self.backdrop_path,
                "mediaid_prefix": self.source,
                "overview": self.overview,
                "vote_average": self.rating,
                "tracks": [track.to_dict() for track in self.tracks],
                "releases": [release.to_dict() for release in self.releases],
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复标准化专辑信息。"""
        _validate_music_type(data.get("type"))
        values = _init_values(cls, data)
        for key in ("artists", "secondary_types", "genres", "tags"):
            values[key] = _string_list(values.get(key))
        values["artist_ids"] = _aligned_list(values.get("artist_ids"))
        values["rating"] = _optional_float(values.get("rating"))
        values["rating_votes"] = _optional_int(values.get("rating_votes"))
        values["tracks"] = [
            item if isinstance(item, MusicInfo) else MusicInfo.from_dict(item)
            for item in data.get("tracks") or []
        ]
        values["releases"] = [
            item if isinstance(item, MusicRelease) else MusicRelease.from_dict(item)
            for item in data.get("releases") or []
        ]
        values["raw_data"] = dict(values.get("raw_data") or {})
        return cls(**values)

    def to_music_info(self) -> MusicInfo:
        """转换为专辑卡片使用的音乐信息，供列表接口统一返回。"""
        return MusicInfo(
            source=self.source,
            media_id=self.media_id,
            music_type=MUSIC_ENTITY_ALBUM,
            title=self.title,
            artists=list(self.artists),
            artist_ids=list(self.artist_ids),
            album=self.title,
            album_artist=self.artist or None,
            album_id=self.media_id,
            album_type=self.album_type,
            year=self.year,
            release_date=self.release_date,
            total_tracks=self.track_count or None,
            duration=self.duration,
            cover_url=self.cover_url,
            category=self.category,
            genres=list(self.genres),
            names=[name for name in (self.title,) if name],
            detail_link=self.detail_link,
        )


@dataclass
class MusicArtistInfo:
    """标准化音乐艺术家信息（MusicBrainz Artist）。"""

    type: MediaType = field(default=MediaType.MUSIC, init=False)
    music_type: str = field(default=MUSIC_ENTITY_ARTIST, init=False)
    source: str | None = None
    media_id: str | None = None
    name: str | None = None
    sort_name: str | None = None
    # MusicBrainz 消歧义说明，同名艺术家依靠该字段区分
    disambiguation: str | None = None
    # 艺术家类型：Person、Group、Orchestra、Choir、Character、Other
    artist_type: str | None = None
    gender: str | None = None
    country: str | None = None
    area: str | None = None
    begin_date: str | None = None
    end_date: str | None = None
    ended: bool = False
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    # 关联艺术家场景下的关系文本，例如乐队成员、子团体
    relation: str | None = None
    image_url: str | None = None
    detail_link: str | None = None
    # 外部站点链接，键为关系类型，值为地址
    external_links: dict[str, str] = field(default_factory=dict)
    album_count: int | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str | None:
        """返回兼容通用媒体展示组件的标题。"""
        return self.name

    @property
    def life_span(self) -> str:
        """返回艺术家活跃时间区间文本。"""
        if not self.begin_date and not self.end_date:
            return ""
        end = self.end_date or ("" if self.ended else "…")
        return f"{self.begin_date or '?'} - {end}" if end else (self.begin_date or "")

    @property
    def overview(self) -> str:
        """返回艺术家摘要，供卡片和详情页复用。"""
        parts = [
            self.artist_type,
            self.disambiguation,
            self.area or self.country,
            self.life_span,
            " / ".join(self.genres[:3]),
        ]
        return " · ".join(part for part in parts if part)

    @property
    def poster_path(self) -> str | None:
        """返回兼容现有卡片的艺术家图片地址。"""
        return self.image_url

    def to_dict(self) -> dict[str, Any]:
        """转换为可传输的字典。"""
        payload = {key: value for key, value in asdict(self).items() if key != "type"}
        payload.update(
            {
                "type": self.type.value,
                "title": self.title,
                "life_span": self.life_span,
                "overview": self.overview,
                "poster_path": self.poster_path,
                "mediaid_prefix": self.source,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复标准化艺术家信息。"""
        _validate_music_type(data.get("type"))
        values = _init_values(cls, data)
        for key in ("genres", "tags", "aliases"):
            values[key] = _string_list(values.get(key))
        values["ended"] = bool(values.get("ended"))
        values["album_count"] = _optional_int(values.get("album_count"))
        values["external_links"] = {
            str(key): str(value)
            for key, value in (values.get("external_links") or {}).items()
            if value
        }
        values["raw_data"] = dict(values.get("raw_data") or {})
        return cls(**values)
