"""从标准媒体对象构造来源无关的分类事实。"""

import re
from collections.abc import Mapping
from enum import Enum
from typing import TypeAlias, cast

from app.domain.context import MediaInfo, MusicAlbumInfo, MusicArtistInfo, MusicInfo
from app.schemas.category import (
    ClassificationFacts,
    ClassificationFactValue,
    ClassificationIdentityFacts,
    ClassificationMediaFacts,
    ClassificationMediaType,
    ClassificationMusicFacts,
)

ClassificationMedia: TypeAlias = MediaInfo | MusicInfo | MusicAlbumInfo | MusicArtistInfo
"""可以投影为标准分类事实的领域媒体对象。"""

_TMDB_GENRE_KEYS = {
    "12": "adventure",
    "14": "fantasy",
    "16": "animation",
    "18": "drama",
    "27": "horror",
    "28": "action",
    "35": "comedy",
    "36": "history",
    "37": "western",
    "53": "thriller",
    "80": "crime",
    "99": "documentary",
    "878": "science_fiction",
    "9648": "mystery",
    "10402": "music",
    "10749": "romance",
    "10751": "family",
    "10752": "war",
    "10762": "kids",
    "10764": "reality",
    "10767": "talk",
    "10770": "tv_movie",
}

_GENRE_KEY_ALIASES = {
    "action": "action",
    "动作": "action",
    "adventure": "adventure",
    "冒险": "adventure",
    "animation": "animation",
    "anime": "animation",
    "动画": "animation",
    "动漫": "animation",
    "comedy": "comedy",
    "喜剧": "comedy",
    "crime": "crime",
    "犯罪": "crime",
    "documentary": "documentary",
    "纪录": "documentary",
    "纪录片": "documentary",
    "drama": "drama",
    "剧情": "drama",
    "family": "family",
    "家庭": "family",
    "fantasy": "fantasy",
    "奇幻": "fantasy",
    "history": "history",
    "历史": "history",
    "horror": "horror",
    "恐怖": "horror",
    "kids": "kids",
    "children": "kids",
    "儿童": "kids",
    "music": "music",
    "音乐": "music",
    "mystery": "mystery",
    "悬疑": "mystery",
    "reality": "reality",
    "reality tv": "reality",
    "game show": "reality",
    "真人秀": "reality",
    "综艺": "reality",
    "romance": "romance",
    "爱情": "romance",
    "science fiction": "science_fiction",
    "sci fi": "science_fiction",
    "科幻": "science_fiction",
    "talk": "talk",
    "talk show": "talk",
    "脱口秀": "talk",
    "thriller": "thriller",
    "惊悚": "thriller",
    "tv movie": "tv_movie",
    "电视电影": "tv_movie",
    "war": "war",
    "战争": "war",
    "western": "western",
    "西部": "western",
    "classical": "classical",
    "古典": "classical",
    "electronic": "electronic",
    "电子": "electronic",
    "folk": "folk",
    "民谣": "folk",
    "hip hop": "hip_hop",
    "hip hop rap": "hip_hop",
    "说唱": "hip_hop",
    "jazz": "jazz",
    "爵士": "jazz",
    "pop": "pop",
    "流行": "pop",
    "rock": "rock",
    "摇滚": "rock",
    "soundtrack": "soundtrack",
    "原声": "soundtrack",
}

_COUNTRY_CODE_ALIASES = {
    "中国": "CN",
    "中国大陆": "CN",
    "内地": "CN",
    "china": "CN",
    "香港": "HK",
    "中国香港": "HK",
    "hong kong": "HK",
    "台湾": "TW",
    "中国台湾": "TW",
    "taiwan": "TW",
    "澳门": "MO",
    "中国澳门": "MO",
    "macao": "MO",
    "日本": "JP",
    "japan": "JP",
    "韩国": "KR",
    "南韩": "KR",
    "south korea": "KR",
    "republic of korea": "KR",
    "朝鲜": "KP",
    "north korea": "KP",
    "美国": "US",
    "united states": "US",
    "united states of america": "US",
    "英国": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "法国": "FR",
    "france": "FR",
    "德国": "DE",
    "germany": "DE",
    "意大利": "IT",
    "italy": "IT",
    "西班牙": "ES",
    "spain": "ES",
    "俄罗斯": "RU",
    "russia": "RU",
    "加拿大": "CA",
    "canada": "CA",
    "澳大利亚": "AU",
    "australia": "AU",
    "新西兰": "NZ",
    "new zealand": "NZ",
    "印度": "IN",
    "india": "IN",
    "泰国": "TH",
    "thailand": "TH",
    "新加坡": "SG",
    "singapore": "SG",
    "马来西亚": "MY",
    "malaysia": "MY",
    "越南": "VN",
    "vietnam": "VN",
    "印度尼西亚": "ID",
    "indonesia": "ID",
    "菲律宾": "PH",
    "philippines": "PH",
    "巴西": "BR",
    "brazil": "BR",
    "墨西哥": "MX",
    "mexico": "MX",
}


def build_classification_facts(
    media: ClassificationMedia,
    *,
    extensions: Mapping[str, Mapping[str, ClassificationFactValue]] | None = None,
) -> ClassificationFacts:
    """构造规则求值器唯一接收的标准事实，并保留主媒体身份。"""
    media_source = _enum_text(getattr(media, "media_source", None))
    media_id = _optional_text(getattr(media, "media_id", None))
    if not media_source or not media_id:
        raise ValueError("分类事实要求完整的 media_source 与 media_id")

    media_type = _classification_media_type(getattr(media, "type", None))
    is_music = media_type == "音乐"
    media_facts = ClassificationMediaFacts(
        type=media_type,
        title=_optional_text(getattr(media, "title", None)),
        year=_optional_year(getattr(media, "year", None)),
        language=None if is_music else _optional_text(getattr(media, "original_language", None)),
        countries=_music_countries(media) if is_music else _video_countries(media),
        genre_keys=_classification_genre_keys(media),
        genre_names=_genre_names(getattr(media, "genres", None)),
        adult=None if is_music else _optional_bool(getattr(media, "adult", None)),
        runtime=None if is_music else _video_runtime(media),
        content_rating=None if is_music else _optional_text(getattr(media, "content_rating", None)),
        companies=None if is_music else _named_values(getattr(media, "production_companies", None)),
        networks=None if is_music else _named_values(getattr(media, "networks", None)),
    )
    return ClassificationFacts(
        identity=ClassificationIdentityFacts(
            media_source=media_source,
            media_id=media_id,
        ),
        media=media_facts,
        music=_music_facts(media) if is_music else None,
        extensions={
            str(source): {str(key): value for key, value in values.items()}
            for source, values in (extensions or {}).items()
        },
    )


def _classification_media_type(value: object) -> ClassificationMediaType:
    """把领域枚举或字符串归一为分类模型支持的媒体类型。"""
    normalized = _enum_text(value)
    if normalized not in {"电影", "电视剧", "音乐"}:
        raise ValueError(f"不支持的分类媒体类型：{normalized or value}")
    return cast(ClassificationMediaType, normalized)


def _music_facts(media: ClassificationMedia) -> ClassificationMusicFacts:
    """从音乐标准字段构造音乐专用事实，不读取 raw_data。"""
    return ClassificationMusicFacts(
        entity_type=_optional_text(getattr(media, "music_type", None)),
        album_type=_optional_text(getattr(media, "album_type", None)),
        secondary_types=_optional_string_list(getattr(media, "secondary_types", None)),
        genres=_genre_names(getattr(media, "genres", None)),
        tags=_optional_string_list(getattr(media, "tags", None)),
        artists=_optional_string_list(getattr(media, "artists", None)),
        artist_country=_country_code(
            getattr(media, "artist_country", None) or getattr(media, "country", None)
        ),
        release_status=_optional_text(getattr(media, "release_status", None)),
    )


def _music_countries(media: ClassificationMedia) -> list[str] | None:
    """将音乐艺术家国家作为通用国家事实投影。"""
    country = _country_code(getattr(media, "artist_country", None) or getattr(media, "country", None))
    return [country] if country else None


def _video_countries(media: ClassificationMedia) -> list[str] | None:
    """优先使用来源投影的国家代码，缺失时读取标准制作国家结构。"""
    origin = _country_codes(getattr(media, "origin_country", None))
    if origin:
        return origin
    countries: list[str] = []
    for item in getattr(media, "production_countries", None) or []:
        value = _item_value(item, "iso_3166_1") or _item_value(item, "name")
        text = _country_code(value)
        if text and text not in countries:
            countries.append(text)
    return countries or None


def _video_runtime(media: ClassificationMedia) -> int | None:
    """返回电影时长或电视剧首个有效单集时长。"""
    runtime = _optional_int(getattr(media, "runtime", None))
    if runtime is not None:
        return runtime
    for value in getattr(media, "episode_run_time", None) or []:
        runtime = _optional_int(value)
        if runtime is not None:
            return runtime
    return None


def _classification_genre_keys(media: ClassificationMedia) -> list[str] | None:
    """把来源 Genre ID、类型名和音乐流派投影为稳定跨来源键。"""
    values: list[str] = []
    for item in _optional_string_list(
        getattr(media, "classification_genre_keys", None)
    ) or []:
        _append_unique(values, item)
    for item in getattr(media, "genre_ids", None) or []:
        if key := _TMDB_GENRE_KEYS.get(str(item)):
            _append_unique(values, key)
    for item in _genre_names(getattr(media, "genres", None)) or []:
        if key := _GENRE_KEY_ALIASES.get(_normalized_label(item)):
            _append_unique(values, key)
    return values or None


def _genre_names(value: object) -> list[str] | None:
    """兼容字符串、字典和 Pydantic 摘要的来源类型名称。"""
    if not isinstance(value, (list, tuple, set)):
        return None
    names: list[str] = []
    for item in value:
        candidate = _item_value(item, "name") if not isinstance(item, str) else item
        text = _optional_text(candidate)
        if text and text not in names:
            names.append(text)
    return names or None


def _named_values(value: object) -> list[str] | None:
    """从标准公司或电视网摘要提取稳定名称列表。"""
    return _genre_names(value)


def _country_codes(value: object) -> list[str] | None:
    """将来源国家集合规范化为稳定代码，并保持未知值可用于精确规则。"""
    values = value if isinstance(value, (list, tuple, set)) else [value]
    countries: list[str] = []
    for item in values:
        if country := _country_code(item):
            _append_unique(countries, country)
    return countries or None


def _country_code(value: object) -> str | None:
    """把常见国家代码或中英文名称规范化为 ISO 3166-1 alpha-2。"""
    text = _optional_text(value)
    if not text:
        return None
    if len(text) == 2 and text.isascii() and text.isalpha():
        return text.upper()
    return _COUNTRY_CODE_ALIASES.get(_normalized_label(text), text)


def _normalized_label(value: object) -> str:
    """生成适合静态别名表匹配的大小写与分隔符无关文本。"""
    return re.sub(r"[\s_&/+-]+", " ", str(value).strip().casefold()).strip()


def _append_unique(values: list[str], value: str) -> None:
    """按首次出现顺序追加非重复规范值。"""
    if value and value not in values:
        values.append(value)


def _item_value(item: object, key: str) -> object:
    """读取映射或标准对象上的单个字段。"""
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def _enum_text(value: object) -> str:
    """将字符串枚举和普通标量转换为无空白文本。"""
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip() if value is not None else ""


def _optional_text(value: object) -> str | None:
    """将可选标量归一为非空文本。"""
    text = _enum_text(value)
    return text or None


def _optional_string_list(value: object) -> list[str] | None:
    """将可选集合归一为稳定去重的非空字符串列表。"""
    if value is None:
        return None
    values = value if isinstance(value, (list, tuple, set)) else [value]
    output: list[str] = []
    for item in values:
        text = _optional_text(item)
        if text and text not in output:
            output.append(text)
    return output or None


def _optional_year(value: object) -> int | None:
    """从整数或可变精度日期文本提取年份。"""
    text = _enum_text(value)[:4]
    return int(text) if text.isdigit() else None


def _optional_int(value: object) -> int | None:
    """将整数兼容值归一为整数，无效值返回 None。"""
    if value in {None, ""} or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _optional_bool(value: object) -> bool | None:
    """只接受真实布尔值，避免来源字符串产生隐式真值。"""
    return value if isinstance(value, bool) else None
