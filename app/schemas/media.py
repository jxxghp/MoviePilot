"""
媒体身份的规范化与校验。

「media_source 与 media_id 必须成对、非零、去空白」这条不变量在两处生效：DTO 侧由
下方两个 Mixin 在 pydantic 校验期表达，持久化侧由 app/db/models/_identity.py 在写库前
表达。两者共用本模块的原语，形态不同但规则同源。

这些原语此前住在 app/domain/media.py。它们只依赖 MediaSource 与字符串，是身份的
**表示规则**而非领域策略——真正的策略（按配置选来源、判音乐实体类型）仍留在
app/domain/media.py。放在这里，持久化层就不必为了一条表示规则去依赖领域层。
"""
from typing import Any, Optional, Tuple, Union

from pydantic import model_validator

from app.schemas.types import MediaSource

MEDIA_SOURCE_ALIASES = {
    "tmdb": MediaSource.TMDB,
    "themoviedb": MediaSource.TMDB,
    "douban": MediaSource.Douban,
    "bangumi": MediaSource.Bangumi,
    "anilist": MediaSource.AniList,
    "imdb": MediaSource.IMDb,
    "tvdb": MediaSource.TVDB,
    "musicbrainz": MediaSource.MusicBrainz,
    "theaudiodb": MediaSource.TheAudioDB,
    "audio_db": MediaSource.TheAudioDB,
    "doubanmusic": MediaSource.DoubanMusic,
    "douban_music": MediaSource.DoubanMusic,
    "bilibili": MediaSource.Bilibili,
    "mangguodiscover": MediaSource.MangoTV,
    "mango_tv": MediaSource.MangoTV,
    "migu": MediaSource.MiguVideo,
    "migu_video": MediaSource.MiguVideo,
    "tencentvideodiscover": MediaSource.TencentVideo,
    "tencent_video": MediaSource.TencentVideo,
    "iqiyi": MediaSource.Iqiyi,
    "iqiyidiscover": MediaSource.Iqiyi,
}

MEDIA_SOURCE_PREFIXES = {
    MediaSource.TMDB: "tmdb",
    MediaSource.Douban: "douban",
    MediaSource.Bangumi: "bangumi",
    MediaSource.AniList: "anilist",
    MediaSource.IMDb: "imdb",
    MediaSource.TVDB: "tvdb",
    MediaSource.MusicBrainz: "musicbrainz",
    MediaSource.TheAudioDB: "theaudiodb",
    MediaSource.DoubanMusic: "doubanmusic",
    MediaSource.Bilibili: "bilibili",
    MediaSource.MangoTV: "mangguodiscover",
    MediaSource.MiguVideo: "migu",
    MediaSource.TencentVideo: "tencentvideodiscover",
    MediaSource.Iqiyi: "iqiyidiscover",
}


def normalize_media_source(
        source: Optional[Union[MediaSource, str]],
) -> Optional[MediaSource]:
    """将内置别名或插件扩展标识规范化为 MediaSource。"""
    if not source:
        return None
    if isinstance(source, MediaSource):
        return source
    normalized = str(source).strip().casefold()
    builtin_source = MEDIA_SOURCE_ALIASES.get(normalized)
    if builtin_source:
        return builtin_source
    try:
        return MediaSource(normalized)
    except ValueError:
        return None


def parse_media_key(
        media_key: Optional[str],
) -> Tuple[Optional[MediaSource], Optional[str]]:
    """解析带来源前缀的媒体键，返回规范化数据源与原生 ID。"""
    if not media_key or ":" not in str(media_key):
        return None, None
    prefix, media_id = str(media_key).split(":", 1)
    source = normalize_media_source(prefix)
    media_id = media_id.strip()
    if not source or not media_id or media_id == "0":
        return None, None
    return source, media_id


def build_media_key(
        media_source: Optional[Union[MediaSource, str]],
        media_id: Optional[Any],
) -> str:
    """构造 API 使用的带来源前缀媒体键。"""
    normalized_source = normalize_media_source(media_source)
    normalized_id = str(media_id).strip() if media_id is not None else ""
    if not normalized_source or not normalized_id or normalized_id == "0":
        return ""
    prefix = MEDIA_SOURCE_PREFIXES.get(normalized_source, normalized_source.value)
    return f"{prefix}:{normalized_id}"


def resolve_media_identity(
        media: Any = None,
        media_source: Optional[Union[MediaSource, str]] = None,
        media_id: Optional[Any] = None,
) -> Tuple[Optional[MediaSource], Optional[str]]:
    """
    从统一媒体对象或显式字段解析主媒体身份。

    :param media: 包含 ``media_source`` 和 ``media_id`` 的媒体对象
    :param media_source: 显式媒体来源
    :param media_id: 显式来源原生 ID
    :return: 枚举化来源和字符串 ID；任一字段无效时返回空身份
    """
    normalized_source = normalize_media_source(media_source)
    if media_source is not None or media_id is not None:
        normalized_id = str(media_id).strip() if media_id is not None else ""
        if normalized_source and normalized_id and normalized_id != "0":
            return normalized_source, normalized_id
        return None, None

    if media is None:
        return None, None
    normalized_source = normalize_media_source(
        getattr(media, "media_source", None)
        if not isinstance(media, dict)
        else media.get("media_source")
    )
    object_media_id = (
        getattr(media, "media_id", None)
        if not isinstance(media, dict)
        else media.get("media_id")
    )
    if normalized_source and object_media_id is not None:
        normalized_id = str(object_media_id).strip()
        if normalized_id and normalized_id != "0":
            return normalized_source, normalized_id
    return None, None


def normalize_media_identity_payload(
        payload: dict[str, Any],
        *,
        include_empty: bool = False,
) -> dict[str, Any]:
    """
    规范化字典中的媒体身份，保证来源与 ID 始终成对写入。

    :param payload: 待写入或传输的字段字典
    :param include_empty: 字典未声明身份字段时，是否仍补充空身份
    :return: 复制后的规范字典；非法、半对或零值身份会被清空
    """
    normalized = dict(payload)
    has_identity = "media_source" in normalized or "media_id" in normalized
    if not has_identity and not include_empty:
        return normalized
    media_source, media_id = resolve_media_identity(
        media_source=normalized.get("media_source"),
        media_id=normalized.get("media_id"),
    )
    normalized["media_source"] = media_source.value if media_source else None
    normalized["media_id"] = media_id
    return normalized


class OptionalMediaIdentityMixin:
    """为可选媒体身份模型统一校验内置或插件来源与原生 ID 的成对约束。"""

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_media_source(cls, value: object) -> object:
        """兼容媒体身份重构前的旧缓存/插件数据：将旧 ``source`` 键迁移为 ``media_source``。

        旧版本推荐等缓存以 ``source`` 键序列化媒体来源，与 ``media_id`` 成对出现；
        仅当缺失新键、旧值可解析为规范枚举时才迁移，避免误判其他模型同名字段。
        """
        if (
            isinstance(value, dict)
            and "source" in value
            and "media_source" not in value
            and "media_id" in value
            and value.get("source")
        ):
            try:
                MediaSource(value["source"])
            except ValueError:
                return value
            normalized = dict(value)
            normalized["media_source"] = value["source"]
            return normalized
        return value

    @model_validator(mode="after")
    def _validate_optional_media_identity(self):
        """规范化 ID，并拒绝显式半对、空白或零值身份。"""
        source_provided = "media_source" in self.model_fields_set
        id_provided = "media_id" in self.model_fields_set
        if source_provided != id_provided:
            raise ValueError("media_source 和 media_id 必须同时提供")
        normalized_id = (
            str(self.media_id).strip()
            if self.media_id is not None
            else None
        )
        if bool(self.media_source) != bool(normalized_id):
            raise ValueError("media_source 和 media_id 必须同时提供")
        if normalized_id == "0":
            raise ValueError("media_id 不能为 0")
        # 校验器内部的规范化不能伪装成请求显式提交字段，否则 PATCH 会误清空存量身份。
        object.__setattr__(self, "media_id", normalized_id)
        return self


class RequiredMediaIdentityMixin:
    """为必填媒体身份模型统一校验内置或插件来源与原生 ID。"""

    @model_validator(mode="after")
    def _validate_required_media_identity(self):
        """去除 ID 两端空白，并拒绝空白或零值身份。"""
        normalized_id = str(self.media_id).strip()
        if not normalized_id or normalized_id == "0":
            raise ValueError("media_id 必须是非零的来源原生 ID")
        self.media_id = normalized_id
        return self
