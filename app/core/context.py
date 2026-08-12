import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional, Set, Union, Self

from app.core.config import settings
from app.core.meta import MetaBase, MetaMusic
from app.core.meta.metamusic import (
    audio_quality_score,
    audio_quality_tier,
    format_audio_quality,
    infer_audio_lossless,
    normalize_audio_format,
)
from app.core.metainfo import MetaInfo
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_ARTIST,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
)
from app.utils.media import normalize_media_source
from app.utils.string import StringUtils

BANGUMI_MOVIE_PLATFORMS = frozenset({"movie", "电影", "剧场版"})
ANILIST_MOVIE_FORMATS = frozenset({"MOVIE"})
ANILIST_CHINESE_TITLE_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
ANILIST_JAPANESE_KANA_PATTERN = re.compile(r"[\u3040-\u30ff]")


def _validate_music_type(value: object) -> None:
    """校验音乐模型类型字段，仅接受音乐或空值。"""
    if value in {None, MediaType.MUSIC, MediaType.MUSIC.value, "music"}:
        return
    raise ValueError(f"不支持的音乐媒体类型：{value}")


def _music_string_list(value: object) -> list[str]:
    """将音乐标签原始值归一为非空字符串列表，兼容单值、列表与逗号分隔。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _music_aligned_list(value: object) -> list[str]:
    """保留原始位置的字符串列表，用于与艺术家名称按下标对应的 ID 列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item or "") for item in value]
    return [str(value or "")]


def _music_optional_int(value: object) -> int | None:
    """将音乐技术参数安全转换为整数，空值与非数字返回 None。"""
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _music_optional_float(value: object) -> float:
    """将音乐评分安全转换为浮点数，空值与异常返回 0.0。"""
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _music_year_of(release_date: object) -> int | None:
    """从 MusicBrainz 可变精度日期（YYYY / YYYY-MM / YYYY-MM-DD）提取年份。"""
    text = str(release_date or "")[:4]
    return int(text) if text.isdigit() else None


def _music_init_values(model: type, data: dict[str, Any]) -> dict[str, Any]:
    """按 dataclass 可初始化字段过滤字典，避免传入非法构造参数。"""
    init_names = {item.name for item in fields(model) if item.init}
    return {key: value for key, value in data.items() if key in init_names}


@dataclass
class MusicLyrics:
    """标准化单曲歌词，区分同步歌词、纯文本歌词和纯音乐结果。"""

    provider: str
    provider_id: str | None = None
    instrumental: bool = False
    plain_lyrics: str | None = None
    synced_lyrics: str | None = None

    @property
    def content(self) -> str | None:
        """优先返回同步歌词，不存在时回退到纯文本歌词。"""
        return self.synced_lyrics or self.plain_lyrics

    @property
    def extension(self) -> str | None:
        """根据歌词内容返回适合播放器扫描的旁挂文件扩展名。"""
        if self.synced_lyrics:
            return ".lrc"
        if self.plain_lyrics:
            return ".txt"
        return None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从模块或插件返回字典恢复标准歌词对象。"""
        values = _music_init_values(cls, data)
        values["provider"] = str(values.get("provider") or "")
        values["provider_id"] = (
            str(values["provider_id"])
            if values.get("provider_id") is not None
            else None
        )
        values["instrumental"] = bool(values.get("instrumental"))
        return cls(**values)


@dataclass
class MusicInfo:
    """标准化音乐元数据信息。"""

    type: MediaType = field(default=MediaType.MUSIC, init=False)
    media_source: MediaSource | None = None
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
    audio_format: str | None = None
    audio_lossless: bool | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    bitrate: int | None = None
    category: str = ""
    genres: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    detail_link: str | None = None
    listen_count: int | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """将构造参数中的数据源规范化为统一枚举。"""
        self.media_source = normalize_media_source(self.media_source)

    @property
    def artist(self) -> str:
        """返回兼容现有展示组件的艺术家文本。"""
        return " / ".join(self.artists)

    @property
    def audio_quality(self) -> str | None:
        """返回 hires、lossless 或 lossy 音质等级。"""
        return audio_quality_tier(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

    @property
    def audio_quality_score(self) -> int:
        """返回音乐订阅洗版使用的音质优先级。"""
        return audio_quality_score(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

    @property
    def audio_specs(self) -> str | None:
        """返回识别结果和通知使用的格式化音频参数。"""
        return format_audio_quality(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

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
        """转换为统一媒体身份的 Context 外层字典。"""
        payload = asdict(self)
        payload.update(
            {
                "type": self.type.value,
                "media_source": (
                    self.media_source.value
                    if isinstance(self.media_source, MediaSource)
                    else self.media_source
                ),
                "artist": self.artist,
                "title_year": self.title_year,
                "poster_path": self.poster_path,
                "backdrop_path": self.backdrop_path,
                "overview": self.overview,
                "vote_average": self.vote_average,
                "audio_quality": self.audio_quality,
                "audio_quality_score": self.audio_quality_score,
                "audio_specs": self.audio_specs,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复标准化音乐元数据。"""
        _validate_music_type(data.get("type"))
        values = _music_init_values(cls, data)
        values["media_source"] = normalize_media_source(values.get("media_source"))
        values["artists"] = _music_string_list(values.get("artists") or data.get("artist"))
        values["artist_ids"] = _music_aligned_list(values.get("artist_ids"))
        values["genres"] = _music_string_list(values.get("genres"))
        values["names"] = _music_string_list(values.get("names"))
        values["music_type"] = str(values.get("music_type") or MUSIC_ENTITY_RECORDING)
        values["audio_format"] = normalize_audio_format(values.get("audio_format"))
        values["audio_lossless"] = infer_audio_lossless(
            values.get("audio_format"), values.get("audio_lossless")
        )
        values["raw_data"] = dict(values.get("raw_data") or {})
        for key in (
            "year",
            "disc_number",
            "track_number",
            "total_tracks",
            "duration",
            "listen_count",
            "bit_depth",
            "sample_rate",
            "bitrate",
        ):
            values[key] = _music_optional_int(values.get(key))
        return cls(**values)

    @classmethod
    def from_meta(cls, meta: MetaMusic) -> Self:
        """将文件名和音频标签解析结果转换为无远端依赖的标准音乐信息。"""
        return cls(
            media_source=normalize_media_source(meta.media_source),
            media_id=meta.media_id,
            title=meta.title,
            artists=list(meta.artists),
            album=meta.album,
            album_artist=meta.album_artist,
            year=meta.year,
            disc_number=meta.disc_number,
            track_number=meta.track_number,
            total_tracks=meta.total_tracks,
            duration=meta.duration,
            isrc=meta.isrc,
            version=meta.version,
            audio_format=meta.audio_format,
            audio_lossless=meta.audio_lossless,
            bit_depth=meta.bit_depth,
            sample_rate=meta.sample_rate,
            bitrate=meta.bitrate,
            names=[name for name in (meta.title, meta.album) if name],
        )


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
        return _music_year_of(self.date)

    def to_dict(self) -> dict[str, Any]:
        """转换为可传输的字典。"""
        payload = asdict(self)
        payload["year"] = self.year
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复发行版本信息。"""
        values = _music_init_values(cls, data)
        values["formats"] = _music_string_list(values.get("formats"))
        values["track_count"] = _music_optional_int(values.get("track_count"))
        return cls(**values)


@dataclass
class MusicAlbumInfo:
    """标准化音乐专辑信息（MusicBrainz Release Group）。"""

    type: MediaType = field(default=MediaType.MUSIC, init=False)
    music_type: str = field(default=MUSIC_ENTITY_ALBUM, init=False)
    media_source: MediaSource | None = None
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

    def __post_init__(self) -> None:
        """将构造参数中的数据源规范化为统一枚举。"""
        self.media_source = normalize_media_source(self.media_source)

    @property
    def artist(self) -> str:
        """返回兼容现有展示组件的艺术家文本。"""
        return " / ".join(self.artists)

    @property
    def year(self) -> int | None:
        """返回专辑首次发行年份。"""
        return _music_year_of(self.release_date)

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
                "media_source": (
                    self.media_source.value
                    if isinstance(self.media_source, MediaSource)
                    else self.media_source
                ),
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
        values = _music_init_values(cls, data)
        values["media_source"] = normalize_media_source(values.get("media_source"))
        for key in ("artists", "secondary_types", "genres", "tags"):
            values[key] = _music_string_list(values.get(key))
        values["artist_ids"] = _music_aligned_list(values.get("artist_ids"))
        values["rating"] = _music_optional_float(values.get("rating"))
        values["rating_votes"] = _music_optional_int(values.get("rating_votes"))
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
            media_source=self.media_source,
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
    media_source: MediaSource | None = None
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

    def __post_init__(self) -> None:
        """将构造参数中的数据源规范化为统一枚举。"""
        self.media_source = normalize_media_source(self.media_source)

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
                "media_source": (
                    self.media_source.value
                    if isinstance(self.media_source, MediaSource)
                    else self.media_source
                ),
            }
        )
        return payload

    def to_music_info(self) -> MusicInfo:
        """转换为统一搜索列表使用的音乐信息，但不赋予下载或订阅语义。"""
        return MusicInfo(
            media_source=self.media_source,
            media_id=self.media_id,
            music_type=MUSIC_ENTITY_ARTIST,
            title=self.name,
            cover_url=self.image_url,
            version=self.disambiguation,
            category=self.artist_type or "",
            genres=list(self.genres),
            names=[name for name in [self.name, *self.aliases] if name],
            detail_link=self.detail_link,
            raw_data=dict(self.raw_data),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复标准化艺术家信息。"""
        _validate_music_type(data.get("type"))
        values = _music_init_values(cls, data)
        values["media_source"] = normalize_media_source(values.get("media_source"))
        for key in ("genres", "tags", "aliases"):
            values[key] = _music_string_list(values.get(key))
        values["ended"] = bool(values.get("ended"))
        values["album_count"] = _music_optional_int(values.get("album_count"))
        values["external_links"] = {
            str(key): str(value)
            for key, value in (values.get("external_links") or {}).items()
            if value
        }
        values["raw_data"] = dict(values.get("raw_data") or {})
        return cls(**values)


@dataclass
class TorrentInfo:
    """
    种子搜索结果信息。
    """

    # 站点ID
    site: int = None
    # 站点名称
    site_name: str = None
    # 站点Cookie
    site_cookie: str = None
    # 站点UA
    site_ua: str = None
    # 站点是否使用代理
    site_proxy: bool = False
    # 站点优先级
    site_order: int = 0
    # 站点下载器
    site_downloader: str = None
    # 种子名称
    title: str = None
    # 种子副标题
    description: str = None
    # 种子页面声明的媒体身份
    media_source: MediaSource = None
    media_id: str = None
    # 种子链接
    enclosure: str = None
    # 详情页面
    page_url: str = None
    # 种子大小
    size: float = 0.0
    # 做种者
    seeders: int = 0
    # 下载者
    peers: int = 0
    # 完成者
    grabs: int = 0
    # 发布时间
    pubdate: str = None
    # 已过时间
    date_elapsed: str = None
    # 免费截止时间
    freedate: str = None
    # 上传因子
    uploadvolumefactor: float = None
    # 下载因子
    downloadvolumefactor: float = None
    # HR
    hit_and_run: bool = False
    # 种子标签
    labels: list = field(default_factory=list)
    # 种子优先级
    pri_order: int = 0
    # 种子分类 电影/电视剧/音乐
    category: str = None

    def __setattr__(self, name: str, value: Any):
        self.__dict__[name] = value

    def __get_properties(self):
        """
        获取属性列表
        """
        property_names = []
        for member_name in dir(self.__class__):
            member = getattr(self.__class__, member_name)
            if isinstance(member, property):
                property_names.append(member_name)
        return property_names

    def from_dict(self, data: dict):
        """
        从字典中初始化
        """
        properties = self.__get_properties()
        for key, value in data.items():
            if key in properties:
                continue
            setattr(self, key, value)
        self.media_source = normalize_media_source(self.media_source)
        if self.media_id is not None:
            self.media_id = str(self.media_id)

    @staticmethod
    def get_free_string(upload_volume_factor: float, download_volume_factor: float) -> str:
        """
        计算促销类型
        """
        if upload_volume_factor is None or download_volume_factor is None:
            return "未知"
        free_strs = {
            "1.00 1.00": "普通",
            "1.00 0.00": "免费",
            "2.00 1.00": "2X",
            "4.00 1.00": "4X",
            "2.00 0.00": "2X免费",
            "4.00 0.00": "4X免费",
            "1.00 0.50": "50%",
            "2.00 0.50": "2X 50%",
            "1.00 0.70": "70%",
            "1.00 0.30": "30%",
            "1.00 0.75": "75%",
            "1.00 0.25": "25%"
        }
        return free_strs.get('%.2f %.2f' % (upload_volume_factor, download_volume_factor), "未知")

    @property
    def volume_factor(self):
        """
        返回促销信息
        """
        return self.get_free_string(self.uploadvolumefactor, self.downloadvolumefactor)

    @property
    def freedate_diff(self):
        """
        返回免费剩余时间
        """
        if not self.freedate:
            return ""
        return StringUtils.diff_time_str(self.freedate)

    def pub_minutes(self) -> float:
        """
        返回发布时间距离当前时间的分钟数
        """
        if not self.pubdate:
            return 0
        try:
            pub_date = datetime.strptime(self.pubdate, "%Y-%m-%d %H:%M:%S")
            now_datetime = datetime.now()
            return (now_datetime - pub_date).total_seconds() // 60
        except Exception as e:
            print(f"种子发布时间获取失败: {e}")
            return 0

    def to_dict(self):
        """
        返回字典
        """
        dicts = vars(self).copy()
        dicts["media_source"] = (
            self.media_source.value
            if isinstance(self.media_source, MediaSource)
            else self.media_source
        )
        dicts["media_id"] = str(self.media_id) if self.media_id is not None else None
        dicts["volume_factor"] = self.volume_factor
        dicts["freedate_diff"] = self.freedate_diff
        return dicts


@dataclass
class SubtitleInfo:
    """
    字幕搜索结果信息。
    """

    # 站点ID
    site: int = None
    # 站点名称
    site_name: str = None
    # 站点Cookie
    site_cookie: str = None
    # 站点UA
    site_ua: str = None
    # 站点是否使用代理
    site_proxy: bool = False
    # 站点优先级
    site_order: int = 0
    # 字幕标题
    title: str = None
    # 字幕描述
    description: str = None
    # 字幕下载链接
    enclosure: str = None
    # 详情页面
    page_url: str = None
    # 语言
    language: str = None
    # 语言图标
    language_icon: str = None
    # 字幕大小
    size: float = 0.0
    # 发布时间
    pubdate: str = None
    # 已过时间
    date_elapsed: str = None
    # 点击/下载次数
    grabs: int = 0
    # 上传者
    uploader: str = None
    # 举报页面
    report_url: str = None
    # 种子ID
    torrent_id: str = None
    # 字幕ID
    subtitle_id: str = None
    # 下载文件名
    file_name: str = None

    def __build_meta_info(self) -> Optional[dict]:
        """
        从字幕标题、文件名和描述中识别可展示的季集信息。
        """
        for title in (self.title, self.file_name, self.description):
            if not title:
                continue
            try:
                meta_dict = MetaInfo(title=title, subtitle=self.description).to_dict()
            except Exception:
                continue
            if meta_dict.get("season_episode") or meta_dict.get("episode_list"):
                return meta_dict
        return None

    def __setattr__(self, name: str, value: Any):
        self.__dict__[name] = value

    def from_dict(self, data: dict):
        """
        从字典中初始化。
        """
        for key, value in data.items():
            setattr(self, key, value)

    def to_dict(self):
        """
        返回字典。
        """
        dicts = vars(self).copy()
        meta_info = self.__build_meta_info()
        if meta_info:
            dicts["meta_info"] = meta_info
            dicts["season_episode"] = meta_info.get("season_episode")
            dicts["episode_list"] = meta_info.get("episode_list")
        return dicts


@dataclass
class MediaInfo:
    """
    统一媒体信息，负责聚合各元数据源的标准字段
    """

    # 内部标记：是否命中本地识别缓存，不参与序列化
    recognize_cache_hit = False
    # 媒体主身份来源
    media_source: MediaSource = None
    # 当前数据源原生 ID
    media_id: str = None
    # 请求级刮削来源；为空时使用系统设置
    scrape_source: str = None
    # 类型 电影、电视剧
    type: MediaType = None
    # 媒体标题
    title: str = None
    # 英文标题
    en_title: str = None
    # 香港标题
    hk_title: str = None
    # 台湾标题
    tw_title: str = None
    # 新加坡标题
    sg_title: str = None
    # 年份
    year: str = None
    # 季
    season: int = None
    # 数据源返回的辅助 ID，仅作为元数据输出，不参与通用身份传递或持久化
    tmdb_id: int = None
    imdb_id: str = None
    tvdb_id: int = None
    tvdb_slug: str = None
    douban_id: str = None
    bangumi_id: int = None
    anilist_id: int = None
    anidb_id: int = None
    # 合集ID
    collection_id: int = None
    # 媒体原语种
    original_language: str = None
    # 媒体原发行标题
    original_title: str = None
    # 媒体发行日期
    release_date: str = None
    # 背景图片
    backdrop_path: str = None
    # 海报图片
    poster_path: str = None
    # LOGO
    logo_path: str = None
    # 评分
    vote_average: float = None
    # 描述
    overview: str = None
    # 风格ID
    genre_ids: list = field(default_factory=list)
    # 所有别名和译名
    names: list = field(default_factory=list)
    # 各季的剧集清单信息
    seasons: Dict[int, list] = field(default_factory=dict)
    # 各季详情
    season_info: List[dict] = field(default_factory=list)
    # 各季的年份
    season_years: dict = field(default_factory=dict)
    # 二级分类
    category: str = ""
    # TMDB INFO
    tmdb_info: dict = field(default_factory=dict)
    # 豆瓣 INFO
    douban_info: dict = field(default_factory=dict)
    # Bangumi INFO
    bangumi_info: dict = field(default_factory=dict)
    # AniList INFO
    anilist_info: dict = field(default_factory=dict)
    # 导演
    directors: List[dict] = field(default_factory=list)
    # 演员
    actors: List[dict] = field(default_factory=list)
    # 是否成人内容
    adult: bool = False
    # 创建人
    created_by: list = field(default_factory=list)
    # 集时长
    episode_run_time: list = field(default_factory=list)
    # 风格
    genres: List[dict] = field(default_factory=list)
    # 首播日期
    first_air_date: str = None
    # 首页
    homepage: str = None
    # 语种
    languages: list = field(default_factory=list)
    # 最后上映日期
    last_air_date: str = None
    # 流媒体平台
    networks: list = field(default_factory=list)
    # 集数
    number_of_episodes: int = None
    # 季数
    number_of_seasons: int = None
    # 原产国
    origin_country: list = field(default_factory=list)
    # 原名
    original_name: str = None
    # 出品公司
    production_companies: list = field(default_factory=list)
    # 出品国
    production_countries: list = field(default_factory=list)
    # 语种
    spoken_languages: list = field(default_factory=list)
    # 所有发行日期
    release_dates: list = field(default_factory=list)
    # 状态
    status: str = None
    # 标签
    tagline: str = None
    # 评价数量
    vote_count: int = None
    # 流行度
    popularity: float = None
    # 时长
    runtime: int = None
    # 下一集
    next_episode_to_air: dict = field(default_factory=dict)
    # 内容分级
    content_rating: str = None
    # 全部剧集组
    episode_groups: List[dict] = field(default_factory=list)
    # 剧集组
    episode_group: str = None

    def __post_init__(self):
        """规范化媒体来源，并从各来源原始数据初始化统一字段。"""
        self.media_source = normalize_media_source(self.media_source)
        # 设置媒体信息
        if self.tmdb_info:
            self.set_tmdb_info(self.tmdb_info)
        if self.douban_info:
            self.set_douban_info(self.douban_info)
        if self.bangumi_info:
            self.set_bangumi_info(self.bangumi_info)
        if self.anilist_info:
            self.set_anilist_info(self.anilist_info)
        self.media_source = normalize_media_source(self.media_source)

    def __setattr__(self, name: str, value: Any):
        self.__dict__[name] = value

    def __get_properties(self):
        """
        获取属性列表
        """
        property_names = []
        for member_name in dir(self.__class__):
            member = getattr(self.__class__, member_name)
            if isinstance(member, property):
                property_names.append(member_name)
        return property_names

    def from_dict(self, data: dict):
        """
        从字典中初始化
        """
        properties = self.__get_properties()
        for key, value in data.items():
            if key in properties:
                continue
            setattr(self, key, value)
        self.media_source = normalize_media_source(self.media_source)
        if isinstance(self.type, str):
            self.type = MediaType(self.type)

    def set_category(self, cat: str):
        """
        设置二级分类
        """
        self.category = cat or ""

    def set_tmdb_info(self, info: dict):
        """
        初始化媒信息
        """

        def __directors_actors(tmdbinfo: dict) -> Tuple[List[dict], List[dict]]:
            """
            查询导演和演员
            :param tmdbinfo: TMDB元数据
            :return: 导演列表，演员列表
            """
            """
            "cast": [
              {
                "adult": false,
                "gender": 2,
                "id": 3131,
                "known_for_department": "Acting",
                "name": "Antonio Banderas",
                "original_name": "Antonio Banderas",
                "popularity": 60.896,
                "profile_path": "/iWIUEwgn2KW50MssR7tdPeFoRGW.jpg",
                "cast_id": 2,
                "character": "Puss in Boots (voice)",
                "credit_id": "6052480e197de4006bb47b9a",
                "order": 0
              }
            ],
            "crew": [
              {
                "adult": false,
                "gender": 2,
                "id": 5524,
                "known_for_department": "Production",
                "name": "Andrew Adamson",
                "original_name": "Andrew Adamson",
                "popularity": 9.322,
                "profile_path": "/qqIAVKAe5LHRbPyZUlptsqlo4Kb.jpg",
                "credit_id": "63b86b2224b33300a0585bf1",
                "department": "Production",
                "job": "Executive Producer"
              }
            ]
            """
            if not tmdbinfo:
                return [], []
            _credits = tmdbinfo.get("credits")
            if not _credits:
                return [], []
            directors = []
            actors = []
            for cast in _credits.get("cast") or []:
                if cast.get("known_for_department") == "Acting":
                    actors.append(cast)
            for crew in _credits.get("crew") or []:
                if crew.get("job") in ["Director", "Writer", "Editor", "Producer"]:
                    directors.append(crew)
            return directors, actors

        if not info:
            return
        # 来源
        self.media_source = MediaSource.TMDB
        # 本体
        self.tmdb_info = info
        # 类型
        if isinstance(info.get('media_type'), MediaType):
            self.type = info.get('media_type')
        elif info.get('media_type'):
            self.type = MediaType.MOVIE if info.get("media_type") == "movie" else MediaType.TV
        else:
            self.type = MediaType.MOVIE if info.get("title") else MediaType.TV
        # 当前来源原生 ID
        self.media_id = str(info.get('id')) if info.get('id') is not None else None
        self.tmdb_id = info.get('id')
        if not self.media_id:
            return
        if info.get("external_ids"):
            self.tvdb_id = info.get("external_ids", {}).get("tvdb_id")
            self.imdb_id = info.get("external_ids", {}).get("imdb_id")
        # 合集ID
        self.collection_id = info.get('collection_id')
        # 评分
        self.vote_average = round(float(info.get('vote_average')), 1) if info.get('vote_average') else 0
        # 描述
        self.overview = info.get('overview')
        # 风格
        self.genre_ids = info.get('genre_ids') or []
        # 原语种
        self.original_language = info.get('original_language')
        # 英文标题
        self.en_title = info.get('en_title')
        # 香港标题
        self.hk_title = info.get('hk_title')
        # 台湾标题
        self.tw_title = info.get('tw_title')
        # 新加坡标题
        self.sg_title = info.get('sg_title')
        if self.type == MediaType.MOVIE:
            # 标题
            self.title = info.get('title')
            # 原标题
            self.original_title = info.get('original_title')
            # 发行日期
            self.release_date = info.get('release_date')
            if self.release_date:
                # 年份
                self.year = self.release_date[:4]
            # 所有发行日期
            self.release_dates = [
                {
                    "date": release_date.get("release_date"),
                    "iso_code": result.get("iso_3166_1"),
                    "note": release_date.get("note"),
                    "type": release_date.get("type"),
                }
                for result in info.get("release_dates", {}).get("results", [])
                for release_date in result.get("release_dates", [])
                if release_date.get("release_date")
            ]
        else:
            # 电视剧
            self.title = info.get('name')
            # 原标题
            self.original_title = info.get('original_name')
            # 发行日期
            self.release_date = info.get('first_air_date')
            if self.release_date:
                # 年份
                self.year = self.release_date[:4]
            # 季集信息
            if info.get('seasons'):
                self.season_info = info.get('seasons')
                for seainfo in info.get('seasons'):
                    # 季
                    season = seainfo.get("season_number")
                    if season is None:
                        continue
                    # 集
                    episode_count = seainfo.get("episode_count")
                    self.seasons[season] = list(range(1, episode_count + 1))
                    # 年份
                    air_date = seainfo.get("air_date")
                    if air_date:
                        self.season_years[season] = air_date[:4]
            # 剧集组
            if info.get("episode_groups"):
                self.episode_groups = info.pop("episode_groups").get("results") or []

        # 海报
        if path := info.get('poster_path'):
            self.poster_path = settings.TMDB_IMAGE_URL(path)
        # 背景
        if path := info.get('backdrop_path'):
            self.backdrop_path = settings.TMDB_IMAGE_URL(path)
        # 导演和演员
        self.directors, self.actors = __directors_actors(info)
        # 别名和译名
        self.names = info.get('names') or []
        # 剩余属性赋值
        for key, value in info.items():
            if not value:
                continue
            if not hasattr(self, key):
                continue
            current_value = getattr(self, key)
            if current_value:
                continue
            if current_value is None:
                setattr(self, key, value)
            elif type(current_value) is type(value):
                setattr(self, key, value)

    def set_douban_info(self, info: dict):
        """
        初始化豆瓣信息
        """
        if not info:
            return
        # 来源
        self.media_source = MediaSource.Douban
        # 本体
        self.douban_info = info
        # 豆瓣ID
        self.media_id = str(info.get("id")) if info.get("id") is not None else None
        self.douban_id = self.media_id
        # 类型
        if not self.type:
            if isinstance(info.get('media_type'), MediaType):
                self.type = info.get('media_type')
            elif info.get("subtype"):
                self.type = MediaType.MOVIE if info.get("subtype") == "movie" else MediaType.TV
            elif info.get("target_type"):
                self.type = MediaType.MOVIE if info.get("target_type") == "movie" else MediaType.TV
            elif info.get("type_name"):
                self.type = MediaType(info.get("type_name"))
            elif info.get("uri"):
                self.type = MediaType.MOVIE if "/movie/" in info.get("uri") else MediaType.TV
            elif info.get("type") and info.get("type") in ["movie", "tv"]:
                self.type = MediaType.MOVIE if info.get("type") == "movie" else MediaType.TV
        # 标题
        if not self.title:
            self.title = info.get("title")
        # 英文标题，暂时不支持
        if not self.en_title:
            self.en_title = info.get('original_title')
        # 原语种标题
        if not self.original_title:
            self.original_title = info.get("original_title")
        # 年份
        if not self.year:
            self.year = info.get("year")[:4] if info.get("year") else None
            if not self.year and info.get("extra"):
                self.year = info.get("extra").get("year")
        # 识别标题中的季
        meta = MetaInfo(info.get("title"))
        # 季
        if self.season is None:
            self.season = meta.begin_season
            if self.season is not None:
                self.type = MediaType.TV
            elif not self.type:
                self.type = MediaType.MOVIE
        # 评分
        if not self.vote_average:
            rating = info.get("rating")
            if rating:
                vote_average = float(rating.get("value"))
            else:
                vote_average = 0
            self.vote_average = vote_average
        # 发行日期
        if not self.release_date:
            if info.get("release_date"):
                self.release_date = info.get("release_date")
            elif info.get("pubdate") and isinstance(info.get("pubdate"), list):
                release_date = info.get("pubdate")[0]
                if release_date:
                    match = re.search(r'\d{4}-\d{2}-\d{2}', release_date)
                    if match:
                        self.release_date = match.group()
        # 海报
        if not self.poster_path:
            if info.get("pic"):
                self.poster_path = info.get("pic", {}).get("large")
            if not self.poster_path and info.get("cover_url"):
                # imageView2/0/q/80/w/9999/h/120/format/webp ->  imageView2/1/w/500/h/750/format/webp
                self.poster_path = re.sub(r'imageView2/\d/q/\d+/w/\d+/h/\d+/format/webp', 'imageView2/1/w/500/h/750/format/webp', info.get("cover_url"))
            if not self.poster_path and info.get("cover"):
                if info.get("cover").get("url"):
                    self.poster_path = info.get("cover").get("url")
                else:
                    self.poster_path = info.get("cover").get("large", {}).get("url")
        # 简介
        if not self.overview:
            self.overview = info.get("intro") or info.get("card_subtitle") or ""
            if not self.overview:
                if info.get("extra", {}).get("info"):
                    extra_info = info.get("extra").get("info")
                    if extra_info:
                        self.overview = "，".join(["：".join(item) for item in extra_info])
        # 从简介中提取年份
        if self.overview and not self.year:
            match = re.search(r'\d{4}', self.overview)
            if match:
                self.year = match.group()
        # 导演和演员
        if not self.directors:
            self.directors = info.get("directors") or []
        if not self.actors:
            self.actors = info.get("actors") or []
        # 别名
        if not self.names:
            akas = info.get("aka")
            if akas:
                self.names = [re.sub(r'\([港台豆友译名]+\)', "", aka) for aka in akas]
        # 剧集
        if self.type == MediaType.TV and not self.seasons:
            meta = MetaInfo(info.get("title"))
            season = meta.begin_season if meta.begin_season is not None else 1
            episodes_count = info.get("episodes_count")
            if episodes_count:
                self.seasons[season] = list(range(1, episodes_count + 1))
        # 季年份
        if self.type == MediaType.TV and not self.season_years:
            season = self.season if self.season is not None else 1
            self.season_years = {
                season: self.year
            }
        # 风格
        if not self.genres:
            self.genres = [{"id": genre, "name": genre} for genre in info.get("genres") or []]
        # 时长
        if not self.runtime and info.get("durations"):
            # 查找数字
            match = re.search(r'\d+', info.get("durations")[0])
            if match:
                self.runtime = int(match.group())
        # 国家
        if not self.production_countries:
            self.production_countries = [{"id": country, "name": country} for country in info.get("countries") or []]
        # 剩余属性赋值
        for key, value in info.items():
            if not value:
                continue
            if not hasattr(self, key):
                continue
            current_value = getattr(self, key)
            if current_value:
                continue
            if current_value is None:
                setattr(self, key, value)
            elif type(current_value) is type(value):
                setattr(self, key, value)

    @staticmethod
    def get_bangumi_media_type(info: dict) -> MediaType:
        """
        根据Bangumi媒介平台获取标准媒体类型，未知平台兼容回退为电视剧

        :param info: Bangumi条目信息
        :return: 标准媒体类型
        """
        platform = str(info.get("platform") or "").strip().casefold()
        if platform in BANGUMI_MOVIE_PLATFORMS:
            return MediaType.MOVIE
        return MediaType.TV

    def set_bangumi_info(self, info: dict) -> None:
        """
        初始化Bangumi信息
        """
        if not info:
            return
        # 来源
        self.media_source = MediaSource.Bangumi
        # 本体
        self.bangumi_info = info
        # Bangumi ID
        self.media_id = str(info.get("id")) if info.get("id") is not None else None
        self.bangumi_id = info.get("id")
        # 类型
        if not self.type:
            self.type = self.get_bangumi_media_type(info)
        # 标题
        if not self.title:
            self.title = info.get("name_cn") or info.get("name")
        # 原语种标题
        if not self.original_title:
            self.original_title = info.get("name")
        # 识别标题中的季
        meta = MetaInfo(self.title)
        # 季
        if self.season is None:
            self.season = meta.begin_season
        # 评分
        if not self.vote_average:
            rating = info.get("rating")
            if rating:
                vote_average = float(rating.get("score"))
            else:
                vote_average = 0
            self.vote_average = vote_average
        # 发行日期
        if not self.release_date:
            self.release_date = info.get("date") or info.get("air_date")
            # 年份
            if not self.year:
                self.year = self.release_date[:4] if self.release_date else None
        # 海报
        if not self.poster_path:
            if info.get("images"):
                self.poster_path = info.get("images", {}).get("large")
            if not self.poster_path and info.get("image"):
                self.poster_path = info.get("image")
        # 简介
        if not self.overview:
            self.overview = info.get("summary")
        # 别名
        if not self.names:
            infobox = info.get("infobox")
            if infobox:
                akas = [item.get("value") for item in infobox if item.get("key") == "别名"]
                if akas:
                    if isinstance(akas[0], list):
                        self.names = [aka.get("v") if isinstance(aka, dict) else aka for aka in akas[0]]
                    elif isinstance(akas[0], str):
                        self.names = [akas[0]]

        # 剧集
        if self.type == MediaType.TV and not self.seasons:
            meta = MetaInfo(self.title)
            season = meta.begin_season if meta.begin_season is not None else 1
            episodes_count = info.get("total_episodes") or info.get("eps")
            if episodes_count:
                self.seasons[season] = list(range(1, episodes_count + 1))
                self.number_of_episodes = episodes_count
                self.number_of_seasons = 1
        # 风格
        if not self.genres:
            self.genres = [
                {"id": tag.get("name"), "name": tag.get("name")}
                for tag in info.get("tags") or []
                if tag.get("name")
            ]
        # 制作公司与导演
        if info.get("infobox"):
            companies = []
            directors = []
            for item in info.get("infobox"):
                values = item.get("value")
                if not isinstance(values, list):
                    values = [values]
                normalized_values = [
                    value.get("v") if isinstance(value, dict) else value
                    for value in values
                    if value
                ]
                if item.get("key") in {"动画制作", "制作"}:
                    companies.extend({"name": value} for value in normalized_values)
                elif item.get("key") == "导演":
                    directors.extend({"name": value} for value in normalized_values)
            if companies and not self.production_companies:
                self.production_companies = companies
            if directors and not self.directors:
                self.directors = directors
        # 演员
        if not self.actors:
            self.actors = info.get("actors") or []

    @staticmethod
    def get_anilist_media_type(info: dict) -> MediaType:
        """
        根据 AniList 发布格式获取标准媒体类型。

        :param info: AniList 媒体信息
        :return: 标准媒体类型
        """
        return (
            MediaType.MOVIE
            if str(info.get("format") or "").upper() in ANILIST_MOVIE_FORMATS
            else MediaType.TV
        )

    @staticmethod
    def _anilist_date(date_info: dict) -> Optional[str]:
        """
        将 AniList 模糊日期转换为标准日期文本。

        :param date_info: AniList FuzzyDate 字段
        :return: YYYY、YYYY-MM 或 YYYY-MM-DD 日期文本
        """
        if not date_info or not date_info.get("year"):
            return None
        values = [str(date_info.get("year"))]
        if date_info.get("month"):
            values.append(str(date_info.get("month")).zfill(2))
        if date_info.get("day"):
            values.append(str(date_info.get("day")).zfill(2))
        return "-".join(values)

    @staticmethod
    def _anilist_chinese_title(info: dict) -> Optional[str]:
        """
        从 anilist-chinese 注入的标题和别名中选择中文标题。

        :param info: AniList 媒体信息
        :return: 中文标题，未找到时返回 None
        """
        translated_title = (info.get("title") or {}).get("chinese")
        if not translated_title:
            return None
        if (
            ANILIST_CHINESE_TITLE_PATTERN.search(str(translated_title))
            and not ANILIST_JAPANESE_KANA_PATTERN.search(str(translated_title))
        ):
            return str(translated_title)
        for synonym in reversed(info.get("synonyms") or []):
            if (
                ANILIST_CHINESE_TITLE_PATTERN.search(str(synonym))
                and not ANILIST_JAPANESE_KANA_PATTERN.search(str(synonym))
            ):
                return str(synonym)
        return str(translated_title)

    def set_anilist_info(self, info: dict) -> None:
        """
        初始化 AniList 媒体信息。

        :param info: AniList 媒体详情
        """
        if not info:
            return
        self.media_source = MediaSource.AniList
        self.anilist_info = info
        self.media_id = str(info.get("id")) if info.get("id") is not None else None
        self.anilist_id = info.get("id")
        self.type = self.type or self.get_anilist_media_type(info)

        titles = info.get("title") or {}
        self.title = (
            self.title
            or self._anilist_chinese_title(info)
            or titles.get("native")
            or titles.get("romaji")
            or titles.get("english")
        )
        self.en_title = self.en_title or titles.get("english")
        self.original_title = self.original_title or titles.get("native") or titles.get("romaji")
        self.names = list(
            dict.fromkeys(
                value
                for value in [
                    titles.get("english"),
                    titles.get("romaji"),
                    titles.get("native"),
                    *(info.get("synonyms") or []),
                ]
                if value and value != self.title
            )
        )

        self.release_date = self.release_date or self._anilist_date(info.get("startDate") or {})
        self.first_air_date = self.first_air_date or self.release_date
        self.last_air_date = self.last_air_date or self._anilist_date(info.get("endDate") or {})
        self.year = self.year or (
            str(info.get("startDate", {}).get("year"))
            if info.get("startDate", {}).get("year")
            else str(info.get("seasonYear")) if info.get("seasonYear") else None
        )

        cover = info.get("coverImage") or {}
        self.poster_path = self.poster_path or cover.get("extraLarge") or cover.get("large")
        self.backdrop_path = self.backdrop_path or info.get("bannerImage")
        self.overview = self.overview or re.sub(
            r"<[^>]+>",
            "",
            str(info.get("description") or "").replace("<br>", "\n").replace("<br />", "\n"),
        ).strip()
        self.vote_average = self.vote_average or (
            round(float(info.get("averageScore")) / 10, 1)
            if info.get("averageScore") is not None
            else 0
        )
        self.popularity = self.popularity or info.get("popularity")
        self.runtime = self.runtime or info.get("duration")
        self.adult = self.adult or bool(info.get("isAdult"))
        self.status = self.status or info.get("status")
        self.original_language = self.original_language or (
            "ja" if info.get("countryOfOrigin") == "JP" else None
        )
        self.origin_country = self.origin_country or (
            [info.get("countryOfOrigin")] if info.get("countryOfOrigin") else []
        )
        self.production_companies = self.production_companies or [
            {"name": studio.get("name")}
            for studio in info.get("studios", {}).get("nodes") or []
            if studio.get("name")
        ]
        self.genres = self.genres or [
            {"id": genre, "name": genre} for genre in info.get("genres") or []
        ]
        self.actors = self.actors or info.get("actors") or []
        self.directors = self.directors or info.get("directors") or []

        if self.season is None:
            self.season = MetaInfo(self.title).begin_season if self.title else None
        episodes_count = info.get("episodes")
        if self.type == MediaType.TV and episodes_count:
            season = self.season if self.season is not None else 1
            self.seasons[season] = list(range(1, episodes_count + 1))
            self.number_of_episodes = episodes_count
            self.number_of_seasons = 1
            if self.year:
                self.season_years[season] = self.year

        for external_link in info.get("externalLinks") or []:
            if str(external_link.get("site") or "").casefold() != "anidb":
                continue
            match = re.search(r"\d+", external_link.get("url") or "")
            if match:
                self.anidb_id = int(match.group())
                break

    @property
    def title_year(self):
        if self.title:
            return "%s (%s)" % (self.title, self.year) if self.year else self.title
        return ""

    @property
    def detail_link(self):
        """
        TMDB媒体详情页地址
        """
        if self.media_source == MediaSource.TMDB and self.media_id:
            if self.type == MediaType.MOVIE:
                return f"https://www.themoviedb.org/movie/{self.media_id}"
            else:
                return f"https://www.themoviedb.org/tv/{self.media_id}"
        if self.media_source == MediaSource.Douban and self.media_id:
            return f"https://movie.douban.com/subject/{self.media_id}"
        if self.media_source == MediaSource.Bangumi and self.media_id:
            return f"https://bgm.tv/subject/{self.media_id}"
        if self.media_source == MediaSource.AniList and self.media_id:
            return f"https://anilist.co/anime/{self.media_id}"
        if self.media_source == MediaSource.IMDb and self.media_id:
            return f"https://www.imdb.com/title/{self.media_id}"
        if self.media_source == MediaSource.TVDB and self.media_id:
            return f"https://thetvdb.com/search?query={self.media_id}"
        return ""

    @property
    def stars(self):
        """
        返回评分星星个数
        """
        if not self.vote_average:
            return ""
        return "".rjust(int(self.vote_average), "★")

    @property
    def vote_star(self):
        if self.vote_average:
            return "评分：%s" % self.stars
        return ""

    def get_backdrop_image(self, default: bool = False):
        """
        返回背景图片地址
        """
        if self.backdrop_path:
            return self.backdrop_path.replace("original", "w500")
        return default or ""

    def get_message_image(self, default: Optional[bool] = None):
        """
        返回消息图片地址
        """
        if self.backdrop_path:
            return self.backdrop_path.replace("original", "w500")
        return self.get_poster_image(default=default)

    def get_poster_image(self, default: Optional[bool] = None):
        """
        返回海报图片地址
        """
        if self.poster_path:
            return self.poster_path.replace("original", "w500")
        return default or ""

    def get_overview_string(self, max_len: Optional[int] = 140):
        """
        返回带限定长度的简介信息
        :param max_len: 内容长度
        :return:
        """
        overview = str(self.overview).strip()
        placeholder = ' ...'
        max_len = max(len(placeholder), max_len - len(placeholder))
        overview = (overview[:max_len] + placeholder) if len(overview) > max_len else overview
        return overview

    def to_dict(self):
        """
        返回字典
        """
        dicts = vars(self).copy()
        dicts["type"] = self.type.value if self.type else None
        dicts["detail_link"] = self.detail_link
        dicts["title_year"] = self.title_year
        dicts["tmdb_info"] = None
        dicts["douban_info"] = None
        dicts["bangumi_info"] = None
        dicts["anilist_info"] = None
        dicts["media_source"] = (
            self.media_source.value
            if isinstance(self.media_source, MediaSource)
            else self.media_source
        )
        dicts["media_id"] = str(self.media_id) if self.media_id is not None else None
        return dicts

    def clear(self):
        """
        去除多余数据，减小体积
        """
        self.tmdb_info = {}
        self.douban_info = {}
        self.bangumi_info = {}
        self.anilist_info = {}
        self.seasons = {}
        self.genres = []
        self.season_info = []
        self.names = []
        self.actors = []
        self.directors = []
        self.production_companies = []
        self.production_countries = []
        self.spoken_languages = []
        self.networks = []
        self.next_episode_to_air = {}
        self.episode_groups = []


@dataclass
class Context:
    """
    上下文对象
    """

    # 识别信息
    meta_info: Optional[MetaBase] = None
    # 媒体信息
    media_info: Optional[Union[MediaInfo, MusicInfo]] = None
    # 种子信息
    torrent_info: TorrentInfo = None
    # 媒体识别失败次数
    media_recognize_fail_count: int = 0
    # 候选资源来源：rss、spider、search、unknown。
    resource_source: str = "unknown"
    # 候选匹配来源：MediaSource 枚举值、title、unknown。
    match_source: str = "unknown"
    # 候选自身是否已经识别出有效媒体 ID。
    candidate_recognized: bool = False
    # 当前 media_info 是否为目标媒体回填，而不是候选自身识别结果。
    media_info_is_target: bool = False
    # 调用方对本候选允许下载的剧集集合，None 表示不限制，空集合表示拒绝交付任何集。
    allowed_episodes: Optional[Set[int]] = None
    # 下载层确认候选资源覆盖完整目标范围，供订阅事实写入判断整包资源。
    confirmed_full_coverage: bool = False

    def to_dict(self):
        """
        转换为字典
        """
        return {
            "meta_info": self.meta_info.to_dict() if self.meta_info else None,
            "torrent_info": self.torrent_info.to_dict() if self.torrent_info else None,
            "media_info": self.media_info.to_dict() if self.media_info else None,
            "media_recognize_fail_count": self.media_recognize_fail_count,
            "resource_source": self.resource_source,
            "match_source": self.match_source,
            "candidate_recognized": self.candidate_recognized,
            "media_info_is_target": self.media_info_is_target,
            # 保留 None / 空集 / 非空集 三态语义，避免下游误把"显式拒绝"当成"不限制"。
            "allowed_episodes": sorted(self.allowed_episodes) if self.allowed_episodes is not None else None,
            "confirmed_full_coverage": self.confirmed_full_coverage,
        }
