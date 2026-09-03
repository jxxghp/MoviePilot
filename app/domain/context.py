import re
import typing
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Self, Set, Union

import yaml  # type: ignore[import-untyped]
from yaml.constructor import ConstructorError  # type: ignore[import-untyped]
from yaml.events import AliasEvent  # type: ignore[import-untyped]
from yaml.nodes import MappingNode  # type: ignore[import-untyped]

from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import (
    MetaMusic,
    audio_quality_score,
    audio_quality_tier,
    format_audio_quality,
    infer_audio_lossless,
    normalize_audio_format,
)
from app.domain.metainfo import MetaInfo
from app.foundation import temporal as time_tools
from app.schemas.category import ClassificationFactValue, ClassificationResult
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_ARTIST,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
)


class _LyricsfileSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """在 SafeLoader 基础上拒绝 Lyricsfile 规范禁止的重复映射键。"""

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        """构造映射并在解析阶段拒绝重复键。"""
        if not isinstance(node, MappingNode):
            raise ConstructorError(None, None, "Lyricsfile 映射节点无效", node.start_mark)
        keys = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicated = key in keys
            except TypeError as err:
                raise ConstructorError(None, None, "Lyricsfile 映射键必须可哈希", key_node.start_mark) from err
            if duplicated:
                raise ConstructorError(None, None, f"Lyricsfile 存在重复键：{key}", key_node.start_mark)
            keys.add(key)
        return typing.cast(
            dict[Any, Any], super().construct_mapping(node, deep=deep)
        )


def configure_tmdb_image_url_builder(
    builder: Callable[[str], Optional[str]],
) -> None:
    """兼容旧入口，将 TMDB 图片地址构造器交给来源投影 owner。"""
    from app.domain.projection.tmdb import configure_image_url_builder

    configure_image_url_builder(builder)


def _project_tmdb(
    state: Mapping[str, Any],
    info: Mapping[str, Any],
) -> dict[str, Any]:
    """按需加载 TMDB 投影 owner，避免领域上下文冷导入全部来源实现。"""
    from app.domain.projection.tmdb import project

    return project(state, info)


def _project_douban(
    state: Mapping[str, Any],
    info: Mapping[str, Any],
) -> dict[str, Any]:
    """按需加载豆瓣投影 owner，避免领域上下文冷导入全部来源实现。"""
    from app.domain.projection.douban import project

    return project(state, info)


def _project_bangumi(
    state: Mapping[str, Any],
    info: Mapping[str, Any],
) -> dict[str, Any]:
    """按需加载 Bangumi 投影 owner，避免领域上下文冷导入全部来源实现。"""
    from app.domain.projection.bangumi import project

    return project(state, info)


def _project_anilist(
    state: Mapping[str, Any],
    info: Mapping[str, Any],
) -> dict[str, Any]:
    """按需加载 AniList 投影 owner，避免领域上下文冷导入全部来源实现。"""
    from app.domain.projection.anilist import project

    return project(state, info)


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
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _music_optional_float(value: object) -> float:
    """将音乐评分安全转换为浮点数，空值与异常返回 0.0。"""
    if value in {None, ""}:
        return 0.0
    try:
        return float(str(value))
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


def _classification_result(value: object) -> ClassificationResult | None:
    """将分类结果字典恢复为独立模型，避免领域对象共享可变标签列表。"""
    if value is None:
        return None
    if isinstance(value, ClassificationResult):
        return typing.cast(ClassificationResult, value.model_copy(deep=True))
    if isinstance(value, Mapping):
        return typing.cast(
            ClassificationResult,
            ClassificationResult.model_validate(dict(value)),
        )
    raise TypeError("分类结果必须是 ClassificationResult、字典或空值")


def _classification_payload(value: ClassificationResult | None) -> dict[str, Any] | None:
    """把分类结果转换为可持久化字典。"""
    return value.model_dump(mode="json") if value else None


def _music_category_tokens(value: object) -> list[str]:
    """拆分并规范化音乐来源分类文本，供旧载荷语义判定。"""
    return [
        re.sub(r"[\s_-]+", " ", part).strip().casefold()
        for part in re.split(r"[/\|,;、，]+", str(value or ""))
        if part.strip()
    ]


def _legacy_music_category_is_metadata(
    category: object,
    *,
    album_type: object = None,
    secondary_types: object = None,
    genres: object = None,
    tags: object = None,
    classification: ClassificationResult | None = None,
) -> bool:
    """判断旧音乐 category 是否是来源描述，而不是人工库分类。

    旧音乐模型曾把专辑类型、流派写入 category，同时订阅人工分类也可能
    覆盖同一字段。优先用分类结果确认库分类；其余仅在文本能由标准音乐
    元数据解释时判为来源描述，无法解释时保守保留为库分类。
    """
    category_tokens = _music_category_tokens(category)
    if not category_tokens:
        return False

    selection = None
    if classification:
        selection = classification.effective or classification.recommended
    if selection and selection.category_path:
        category_text = " / ".join(category_tokens)
        path_tokens = _music_category_tokens(" / ".join(selection.category_path))
        if category_text in {
            " / ".join(path_tokens),
            path_tokens[-1] if path_tokens else "",
        }:
            return False

    evidence = [str(album_type or "")]
    for value in (secondary_types, genres, tags):
        evidence.extend(_music_string_list(value))
    evidence_tokens = {
        token
        for value in evidence
        for token in _music_category_tokens(value)
    }
    category_text = " ".join(category_tokens)
    if any(
        token and (token in category_text or category_text in token)
        for token in evidence_tokens
    ):
        return True

    source_descriptors = {
        "album",
        "ep",
        "single",
        "broadcast",
        "other",
        "live",
        "compilation",
        "soundtrack",
        "spoken word",
        "interview",
        "audiobook",
        "audio book",
        "audio drama",
        "remix",
        "dj mix",
        "mixtape",
        "street",
        "demo",
        "person",
        "group",
        "orchestra",
        "choir",
        "character",
    }
    return all(token in source_descriptors for token in category_tokens)


def _resolve_music_categories(
    *,
    category: object,
    library_category: object,
    metadata_category: object,
    album_type: object = None,
    secondary_types: object = None,
    genres: object = None,
    tags: object = None,
    classification: ClassificationResult | None = None,
) -> tuple[str, str]:
    """解析新旧音乐分类字段，新字段存在时禁止来源描述升级为库分类。"""
    compatible = str(category or "").strip()
    library = str(library_category or "").strip()
    metadata = str(metadata_category or "").strip()
    if library or metadata:
        return library or compatible, metadata
    if not compatible:
        return "", ""
    if _legacy_music_category_is_metadata(
        compatible,
        album_type=album_type,
        secondary_types=secondary_types,
        genres=genres,
        tags=tags,
        classification=classification,
    ):
        return "", compatible
    return compatible, ""


@dataclass
class MusicLyrics:
    """标准化单曲歌词候选，保留来源匹配度和 Lyricsfile 原始内容。"""

    provider: str
    provider_id: str | None = None
    instrumental: bool = False
    plain_lyrics: str | None = None
    synced_lyrics: str | None = None
    lyricsfile: str | None = None
    language: str | None = None
    match_score: int = 0
    provider_priority: int = 0

    _lyricsfile_max_bytes = 1024 * 1024
    _lyricsfile_max_lines = 10000
    _lyricsfile_max_nodes = 50000
    _lyricsfile_max_depth = 20

    def __post_init__(self) -> None:
        """补全 Lyricsfile 中可安全降级为 LRC 或纯文本的内容。"""
        if not self.lyricsfile:
            return
        parsed = self._parse_lyricsfile(self.lyricsfile)
        if not parsed:
            return
        metadata_value = parsed.get("metadata")
        metadata: dict[str, Any] = (
            metadata_value if isinstance(metadata_value, dict) else {}
        )
        self.instrumental = self.instrumental or bool(
            metadata.get("instrumental", parsed.get("instrumental"))
        )
        self.language = self.language or self._optional_text(
            metadata.get("language", parsed.get("language"))
        )
        if not self.synced_lyrics:
            self.synced_lyrics = self._lyricsfile_to_lrc(parsed)
        if not self.plain_lyrics:
            self.plain_lyrics = self._lyricsfile_to_plain(parsed)

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

    @property
    def quality_rank(self) -> int:
        """返回可比较的歌词质量等级，逐字同步高于逐行同步和纯文本。"""
        if self.lyricsfile and self._lyricsfile_has_words(self.lyricsfile):
            return 4
        if self.synced_lyrics:
            return 3
        if self.plain_lyrics:
            return 1
        if self.instrumental:
            return 1
        return 0

    @property
    def identity_key(self) -> tuple[str, str, str]:
        """构造候选去重键，兼容未提供来源 ID 的插件结果。"""
        return (
            self.provider.casefold(),
            self.provider_id or "",
            self.content or self.lyricsfile or "",
        )

    @classmethod
    def _parse_lyricsfile(cls, content: str) -> dict[str, Any] | None:
        """安全解析受大小和行数约束的 Lyricsfile YAML，并拒绝锚点别名。"""
        if len(content.encode("utf-8")) > cls._lyricsfile_max_bytes:
            return None
        try:
            if any(isinstance(event, AliasEvent) for event in yaml.parse(content)):
                return None
            payload = yaml.load(content, Loader=_LyricsfileSafeLoader)
        except yaml.YAMLError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != "1.0" or not isinstance(payload.get("metadata"), dict):
            return None
        if not cls._lyricsfile_value_is_safe(payload):
            return None
        lines = payload.get("lines")
        if isinstance(lines, list) and len(lines) > cls._lyricsfile_max_lines:
            return None
        return payload if cls._lyricsfile_structure_is_valid(payload) else None

    @classmethod
    def _lyricsfile_structure_is_valid(cls, payload: dict[str, Any]) -> bool:
        """校验 Lyricsfile 1.0 的必需元数据、歌词形状和毫秒时间范围。"""
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return False
        if not cls._optional_text(metadata.get("title")) or not cls._optional_text(metadata.get("artist")):
            return False
        lines = payload.get("lines")
        plain = payload.get("plain")
        if lines is not None and not isinstance(lines, list):
            return False
        if plain is not None and not isinstance(plain, str):
            return False
        if metadata.get("instrumental") is True:
            return not lines and not str(plain or "").strip()
        if not lines and not str(plain or "").strip():
            return False
        return all(cls._lyricsfile_line_is_valid(line) for line in lines or [])

    @classmethod
    def _lyricsfile_line_is_valid(cls, line: Any) -> bool:
        """校验单行与逐字时间戳，拒绝布尔值伪装整数和倒序区间。"""
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            return False
        start = line.get("start_ms")
        end = line.get("end_ms")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            return False
        if end is not None and (
                isinstance(end, bool) or not isinstance(end, int) or end < start
        ):
            return False
        words = line.get("words")
        if words is None:
            return True
        if not isinstance(words, list):
            return False
        for word in words:
            if not isinstance(word, dict) or not isinstance(word.get("text"), str):
                return False
            word_start = word.get("start_ms")
            word_end = word.get("end_ms")
            if isinstance(word_start, bool) or not isinstance(word_start, int) or word_start < 0:
                return False
            if word_end is not None and (
                    isinstance(word_end, bool)
                    or not isinstance(word_end, int)
                    or word_end < word_start
            ):
                return False
        return True

    @classmethod
    def _lyricsfile_value_is_safe(cls, value: Any, depth: int = 0, count: Optional[list[int]] = None) -> bool:
        """限制 YAML 解析后的类型、深度和节点数，避免外部文档消耗过量资源。"""
        if depth > cls._lyricsfile_max_depth:
            return False
        counter = count if count is not None else [0]
        counter[0] += 1
        if counter[0] > cls._lyricsfile_max_nodes:
            return False
        if value is None or isinstance(value, (str, int, bool)):
            return True
        if isinstance(value, list):
            return all(cls._lyricsfile_value_is_safe(item, depth + 1, counter) for item in value)
        if isinstance(value, dict):
            return all(
                isinstance(key, str)
                and cls._lyricsfile_value_is_safe(item, depth + 1, counter)
                for key, item in value.items()
            )
        return False

    @classmethod
    def _lyricsfile_has_words(cls, content: str) -> bool:
        """判断 Lyricsfile 是否包含逐字时间轴。"""
        payload = cls._parse_lyricsfile(content)
        return bool(
            payload
            and any(
                isinstance(line, dict) and isinstance(line.get("words"), list)
                for line in payload.get("lines") or []
            )
        )

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        """把可选标量规范化为非空文本。"""
        text = str(value or "").strip()
        return text or None

    @classmethod
    def _lyricsfile_to_plain(cls, payload: dict[str, Any]) -> str | None:
        """从 Lyricsfile plain 或时间行生成播放器可读纯文本。"""
        plain = cls._optional_text(payload.get("plain"))
        if plain:
            return plain
        texts = []
        for line in payload.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = cls._optional_text(line.get("text"))
            if not text and isinstance(line.get("words"), list):
                text = "".join(
                    str(word.get("text") or "")
                    for word in line["words"]
                    if isinstance(word, dict)
                ).strip() or None
            if text:
                texts.append(text)
        return "\n".join(texts) or None

    @classmethod
    def _lyricsfile_to_lrc(cls, payload: dict[str, Any]) -> str | None:
        """把 Lyricsfile 行级毫秒时间轴转换为通用 LRC。"""
        output = []
        for line in payload.get("lines") or []:
            if not isinstance(line, dict):
                continue
            start_ms = _music_optional_int(line.get("start_ms"))
            if start_ms is None:
                continue
            start_ms = max(start_ms, 0)
            text = cls._optional_text(line.get("text"))
            if not text and isinstance(line.get("words"), list):
                text = "".join(
                    str(word.get("text") or "")
                    for word in line["words"]
                    if isinstance(word, dict)
                ).strip() or None
            if not text:
                continue
            minutes, remainder = divmod(start_ms, 60000)
            seconds = remainder / 1000
            output.append(f"[{minutes:02d}:{seconds:05.2f}]{text}")
        return "\n".join(output) or None

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
    # 专辑副类型：Live、Compilation、Soundtrack 等
    secondary_types: list[str] = field(default_factory=list)
    year: int | None = None
    release_date: str | None = None
    release_status: str | None = None
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
    # 兼容字段，过渡期始终与 library_category 同步
    category: str = ""
    # 自动分类或人工覆盖后的媒体库相对目录分类
    library_category: str = ""
    # 数据源提供的专辑类型、流派等描述性分类
    metadata_category: str = ""
    classification: ClassificationResult | None = None
    # 插件来源提交的受控扩展分类事实，统一收口时由宿主注册表校验
    classification_facts: dict[str, ClassificationFactValue] = field(default_factory=dict)
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    artist_country: str | None = None
    names: list[str] = field(default_factory=list)
    detail_link: str | None = None
    listen_count: int | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)
    # 内部标记：是否命中本地识别缓存，不参与序列化；与 MediaInfo 保持一致，
    # 显式声明以保留 getattr(..., False) 的默认值语义（__getattr__ 兜底会覆盖它）
    recognize_cache_hit = False

    def __post_init__(self) -> None:
        """规范化媒体身份，并兼容拆分旧音乐分类字段。"""
        self.media_source, self.media_id = resolve_media_identity(media=self)
        classification = _classification_result(self.classification)
        library_category, metadata_category = _resolve_music_categories(
            category=self.category,
            library_category=self.library_category,
            metadata_category=self.metadata_category,
            album_type=self.album_type,
            secondary_types=self.secondary_types,
            genres=self.genres,
            tags=self.tags,
            classification=classification,
        )
        self.__dict__["classification"] = classification
        self.__dict__["library_category"] = library_category
        self.__dict__["category"] = library_category
        self.__dict__["metadata_category"] = metadata_category
        self.__dict__["_category_semantics_ready"] = True

    def __setattr__(self, name: str, value: Any) -> None:
        """兼容旧调用方直接写 category，并在初始化后同步库分类字段。"""
        if (
            name in {"category", "library_category"}
            and self.__dict__.get("_category_semantics_ready")
        ):
            normalized = str(value or "").strip()
            self.__dict__["category"] = normalized
            self.__dict__["library_category"] = normalized
            return
        self.__dict__[name] = value

    def __getattr__(self, name: str) -> None:
        """影视专用字段兜底返回 None：音乐模型不存在这些字段，避免下游逐点安全访问。

        兼容影视媒体共享的通用读写路径（通知、整理、历史等），任何缺失字段按
        空值处理；真实字段仍由类定义接管，不受影响。dunder 特殊方法除外——
        copy/pickle 等机制依赖 hasattr 探测 __setstate__ 等钩子，兜底返回 None
        会导致探测误判而调用失败。注意这会掩盖属性名拼写错误，属于权衡取舍。
        """
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return None

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

    def set_library_category(self, category: str | None) -> None:
        """设置媒体库分类，并同步过渡期兼容 category 字段。"""
        self.library_category = category or ""

    def clear(self) -> None:
        """清理不参与队列展示和持久化的上游原始响应。"""
        self.raw_data.clear()

    def to_dict(self) -> dict[str, Any]:
        """转换为统一媒体身份的 Context 外层字典。"""
        payload = asdict(self)
        payload["classification"] = _classification_payload(self.classification)
        payload["category"] = self.library_category
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
        values["classification"] = _classification_result(values.get("classification"))
        values["media_source"] = normalize_media_source(values.get("media_source"))
        values["artists"] = _music_string_list(values.get("artists") or data.get("artist"))
        values["artist_ids"] = _music_aligned_list(values.get("artist_ids"))
        for key in ("secondary_types", "genres", "tags"):
            values[key] = _music_string_list(values.get(key))
        values["names"] = _music_string_list(values.get("names"))
        values["music_type"] = str(values.get("music_type") or MUSIC_ENTITY_RECORDING)
        values["audio_format"] = normalize_audio_format(values.get("audio_format"))
        values["audio_lossless"] = infer_audio_lossless(
            values.get("audio_format"), values.get("audio_lossless")
        )
        values["raw_data"] = dict(values.get("raw_data") or {})
        if "library_category" in data or "metadata_category" in data:
            library_value = (
                data.get("library_category")
                if "library_category" in data
                else data.get("category")
            )
            values["library_category"] = str(library_value or "")
            values["metadata_category"] = str(data.get("metadata_category") or "")
            values["category"] = values["library_category"]
        else:
            library_category, metadata_category = _resolve_music_categories(
                category=data.get("category"),
                library_category=None,
                metadata_category=None,
                album_type=values.get("album_type"),
                secondary_types=values.get("secondary_types"),
                genres=values.get("genres"),
                tags=values.get("tags"),
                classification=values.get("classification"),
            )
            values["category"] = library_category
            values["library_category"] = library_category
            values["metadata_category"] = metadata_category
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
            year=_music_optional_int(meta.year),
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
    # 自动分类或人工覆盖后的媒体库相对目录分类
    library_category: str = ""
    # 数据源提供的专辑类型和副类型描述
    metadata_category: str = ""
    classification: ClassificationResult | None = None
    classification_facts: dict[str, ClassificationFactValue] = field(default_factory=dict)
    release_date: str | None = None
    release_status: str | None = None
    artist_country: str | None = None
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
        """规范化媒体身份，并补全专辑描述分类和分类结果。"""
        self.media_source, self.media_id = resolve_media_identity(media=self)
        self.library_category = str(self.library_category or "").strip()
        self.metadata_category = str(self.metadata_category or "").strip() or (
            " / ".join(
                part for part in [self.album_type, *self.secondary_types] if part
            )
        )
        self.classification = _classification_result(self.classification)

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
        """返回兼容字段，其语义始终等同媒体库分类。"""
        return self.library_category

    @category.setter
    def category(self, value: str | None) -> None:
        """兼容旧调用方直接写 category，并同步媒体库分类。"""
        self.set_library_category(value)

    def set_library_category(self, category: str | None) -> None:
        """设置媒体库分类，来源描述分类保持不变。"""
        self.library_category = str(category or "").strip()

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
        parts = [
            self.artist,
            self.metadata_category,
            self.release_date,
            " / ".join(self.genres[:3]),
        ]
        return " · ".join(part for part in parts if part)

    def to_dict(self) -> dict[str, Any]:
        """转换为兼容前端 MediaInfo 结构的字典。"""
        payload = {
            key: value
            for key, value in asdict(self).items()
            if key not in {"tracks", "releases", "type"}
        }
        payload["classification"] = _classification_payload(self.classification)
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
        values["classification"] = _classification_result(values.get("classification"))
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
        if "library_category" in data or "metadata_category" in data:
            library_value = (
                data.get("library_category")
                if "library_category" in data
                else data.get("category")
            )
            values["library_category"] = str(library_value or "")
            values["metadata_category"] = str(data.get("metadata_category") or "")
        else:
            library_category, metadata_category = _resolve_music_categories(
                category=data.get("category"),
                library_category=None,
                metadata_category=None,
                album_type=values.get("album_type"),
                secondary_types=values.get("secondary_types"),
                genres=values.get("genres"),
                tags=values.get("tags"),
                classification=values.get("classification"),
            )
            values["library_category"] = library_category
            values["metadata_category"] = metadata_category
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
            secondary_types=list(self.secondary_types),
            year=self.year,
            release_date=self.release_date,
            release_status=self.release_status,
            total_tracks=self.track_count or None,
            duration=self.duration,
            cover_url=self.cover_url,
            library_category=self.library_category,
            metadata_category=self.metadata_category,
            classification=self.classification,
            genres=list(self.genres),
            tags=list(self.tags),
            artist_country=self.artist_country,
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
    # 自动分类或人工覆盖后的媒体库相对目录分类
    library_category: str = ""
    # 数据源提供的艺术家类型等描述性分类
    metadata_category: str = ""
    classification: ClassificationResult | None = None
    classification_facts: dict[str, ClassificationFactValue] = field(default_factory=dict)
    # 关联艺术家场景下的关系文本，例如乐队成员、子团体
    relation: str | None = None
    image_url: str | None = None
    detail_link: str | None = None
    # 外部站点链接，键为关系类型，值为地址
    external_links: dict[str, str] = field(default_factory=dict)
    album_count: int | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范化媒体身份，并拆分艺术家描述分类与媒体库分类。"""
        self.media_source, self.media_id = resolve_media_identity(media=self)
        self.library_category = str(self.library_category or "").strip()
        self.metadata_category = str(
            self.metadata_category or self.artist_type or ""
        ).strip()
        self.classification = _classification_result(self.classification)

    @property
    def category(self) -> str:
        """返回兼容字段，其语义始终等同媒体库分类。"""
        return self.library_category

    @category.setter
    def category(self, value: str | None) -> None:
        """兼容旧调用方直接写 category，并同步媒体库分类。"""
        self.set_library_category(value)

    def set_library_category(self, category: str | None) -> None:
        """设置媒体库分类，来源艺术家类型保持不变。"""
        self.library_category = str(category or "").strip()

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
        payload["classification"] = _classification_payload(self.classification)
        payload.update(
            {
                "type": self.type.value,
                "category": self.category,
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
            library_category=self.library_category,
            metadata_category=self.metadata_category,
            classification=self.classification,
            classification_facts=dict(self.classification_facts),
            genres=list(self.genres),
            tags=list(self.tags),
            artist_country=self.country,
            names=[name for name in [self.name, *self.aliases] if name],
            detail_link=self.detail_link,
            raw_data=dict(self.raw_data),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """从字典恢复标准化艺术家信息。"""
        _validate_music_type(data.get("type"))
        values = _music_init_values(cls, data)
        values["classification"] = _classification_result(values.get("classification"))
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
        values["library_category"] = str(
            data.get("library_category") or data.get("category") or ""
        )
        values["metadata_category"] = str(
            data.get("metadata_category") or ""
        )
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

    def __post_init__(self) -> None:
        """将种子声明的媒体身份规范化为统一成对字段。"""
        self.media_source, self.media_id = resolve_media_identity(media=self)

    def __setattr__(self, name: str, value: Any):
        """直接写入种子运行时字段，保留历史动态属性行为。"""
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
        self.media_source, self.media_id = resolve_media_identity(media=self)

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
        return time_tools.format_remaining(self.freedate)

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

    def to_dict(self) -> dict[str, Any]:
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
        """直接写入字幕运行时字段，保留历史动态属性行为。"""
        self.__dict__[name] = value

    def from_dict(self, data: dict):
        """
        从字典中初始化。
        """
        for key, value in data.items():
            setattr(self, key, value)

    def to_dict(self) -> dict[str, Any]:
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
    # 兼容字段，过渡期始终与 library_category 同步
    category: str = ""
    # 自动分类或人工覆盖后的媒体库相对目录分类
    library_category: str = ""
    # 数据源提供的描述性分类
    metadata_category: str = ""
    classification: ClassificationResult | None = None
    # 插件来源提交的受控扩展分类事实，统一收口时由宿主注册表校验
    classification_facts: dict[str, ClassificationFactValue] = field(default_factory=dict)
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
    adult: Optional[bool] = None
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
        """规范化媒体来源、来源投影和新旧分类字段。"""
        self.media_source, self.media_id = resolve_media_identity(media=self)
        # 设置媒体信息
        if self.tmdb_info:
            self.set_tmdb_info(self.tmdb_info)
        if self.douban_info:
            self.set_douban_info(self.douban_info)
        if self.bangumi_info:
            self.set_bangumi_info(self.bangumi_info)
        if self.anilist_info:
            self.set_anilist_info(self.anilist_info)
        self.media_source, self.media_id = resolve_media_identity(media=self)
        library_category = str(self.library_category or self.category or "").strip()
        self.__dict__["library_category"] = library_category
        self.__dict__["category"] = library_category
        self.__dict__["metadata_category"] = str(
            self.metadata_category or ""
        ).strip()
        self.__dict__["classification"] = _classification_result(
            self.classification
        )
        self.__dict__["_category_semantics_ready"] = True

    def __setattr__(self, name: str, value: Any):
        """写入动态字段，并兼容旧调用方直接覆盖 category。"""
        if (
            name in {"category", "library_category"}
            and self.__dict__.get("_category_semantics_ready")
        ):
            normalized = str(value or "").strip()
            self.__dict__["category"] = normalized
            self.__dict__["library_category"] = normalized
            return
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
            if key in properties or key in {
                "category",
                "library_category",
                "metadata_category",
                "classification",
                "_category_semantics_ready",
            }:
                continue
            setattr(self, key, value)
        if "metadata_category" in data:
            self.metadata_category = str(data.get("metadata_category") or "").strip()
        if "classification" in data:
            self.classification = _classification_result(data.get("classification"))
        if "library_category" in data:
            self.set_library_category(data.get("library_category"))
        elif "category" in data:
            self.set_library_category(data.get("category"))
        self.media_source, self.media_id = resolve_media_identity(media=self)
        if isinstance(self.type, str):
            self.type = MediaType(self.type)

    def set_category(self, cat: str):
        """兼容旧入口，委托统一媒体库分类写入方法。"""
        self.set_library_category(cat)

    def set_library_category(self, category: str | None) -> None:
        """设置媒体库分类，并同步过渡期兼容 category 字段。"""
        self.library_category = category or ""

    def _apply_source_projection(self, values: dict[str, Any]) -> None:
        """把来源 owner 返回的字段映射应用到当前领域对象。"""
        for name, value in values.items():
            setattr(self, name, value)

    def set_tmdb_info(self, info: dict):
        """通过 TMDB 来源 owner 初始化统一媒体字段。"""
        self._apply_source_projection(_project_tmdb(vars(self), info))

    def set_douban_info(self, info: dict):
        """通过豆瓣来源 owner 初始化统一媒体字段。"""
        self._apply_source_projection(_project_douban(vars(self), info))

    @staticmethod
    def get_bangumi_media_type(info: dict) -> MediaType:
        """兼容旧方法，委托 Bangumi 来源 owner 解析媒体类型。"""
        from app.domain.projection.bangumi import resolve_media_type

        return resolve_media_type(info)

    def set_bangumi_info(self, info: dict) -> None:
        """通过 Bangumi 来源 owner 初始化统一媒体字段。"""
        self._apply_source_projection(_project_bangumi(vars(self), info))

    @staticmethod
    def get_anilist_media_type(info: dict) -> MediaType:
        """兼容旧方法，委托 AniList 来源 owner 解析媒体类型。"""
        from app.domain.projection.anilist import resolve_media_type

        return resolve_media_type(info)

    @staticmethod
    def _anilist_date(date_info: dict) -> Optional[str]:
        """兼容旧私有补丁点，委托 AniList owner 格式化模糊日期。"""
        from app.domain.projection.anilist import format_date

        return format_date(date_info)

    @staticmethod
    def _anilist_chinese_title(info: dict) -> Optional[str]:
        """兼容旧私有补丁点，委托 AniList owner 选择中文标题。"""
        from app.domain.projection.anilist import select_chinese_title

        return select_chinese_title(info)

    def set_anilist_info(self, info: dict) -> None:
        """通过 AniList 来源 owner 初始化统一媒体字段。"""
        self._apply_source_projection(_project_anilist(vars(self), info))

    @property
    def title_year(self):
        """返回带年份的媒体标题。"""
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
        """返回适合消息展示的评分星级文本。"""
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
        dicts.pop("_category_semantics_ready", None)
        dicts["category"] = self.library_category
        dicts["library_category"] = self.library_category
        dicts["metadata_category"] = self.metadata_category
        dicts["classification"] = _classification_payload(self.classification)
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

    def clear(self) -> None:
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
    # 下载链实际提交的剧集集合；None 表示尚未执行下载选择。
    selected_episodes: Optional[List[int]] = None
    # 下载层确认候选资源覆盖完整目标范围，供订阅事实写入判断整包资源。
    confirmed_full_coverage: bool = False

    def to_dict(self) -> dict[str, Any]:
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
            "selected_episodes": self.selected_episodes,
            "confirmed_full_coverage": self.confirmed_full_coverage,
        }
