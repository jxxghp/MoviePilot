"""
媒体来源的领域策略。

这里只留「按什么规则挑来源、什么算可订阅的音乐实体」这类策略——它们依赖运行期配置
（注入的 SEARCH_SOURCE 提供者）与业务约定，会随产品决策变化。

身份的**表示规则**（别名归一、ID 去空白拒零、成对写入、媒体键前缀）不随策略变化，
已迁至 app/schemas/media.py，与两个身份 Mixin 作伴。持久化层因此不必为一条表示规则
反向依赖领域层。本模块不做 re-export：同一个符号只应有一条 import 路径。
"""
from typing import Callable, Optional, Tuple, Union

from app.schemas.media import normalize_media_source
from app.schemas.types import (
    MUSIC_ENTITY_TYPES,
    MUSIC_SUBSCRIBABLE_TYPES,
    MediaSource,
    MediaSourceSelection,
)

MUSIC_MEDIA_SOURCE_ORDER = (
    MediaSource.MusicBrainz,
    MediaSource.TheAudioDB,
    MediaSource.DoubanMusic,
)
MUSIC_MEDIA_SOURCES = frozenset(MUSIC_MEDIA_SOURCE_ORDER)
_search_source_provider: Callable[[], object] = lambda: None


def configure_search_source_provider(provider: Callable[[], object]) -> None:
    """注入默认媒体来源配置，领域选择逻辑不直接依赖平台 settings。"""
    global _search_source_provider
    _search_source_provider = provider


def normalize_music_type(
        value: Optional[object],
        *,
        allow_artist: bool = True,
) -> Optional[str]:
    """规范化音乐实体类型，非法值返回 None。"""
    normalized = str(value or "").strip().lower()
    allowed = MUSIC_ENTITY_TYPES if allow_artist else MUSIC_SUBSCRIBABLE_TYPES
    return normalized if normalized in allowed else None


def is_music_media_source(
        source: Optional[Union[MediaSource, str]],
) -> bool:
    """判断单个请求级来源是否为内置音乐元数据源。"""
    return normalize_media_source(source) in MUSIC_MEDIA_SOURCES


def parse_media_source_selection(value: Optional[str]) -> Tuple[MediaSource, ...]:
    """
    解析 HTTP 查询参数中的逗号分隔来源，并转换为有序枚举集合。

    :param value: 逗号分隔的来源值；空值表示未显式选择来源
    :return: 去重后的媒体来源枚举元组
    :raises ValueError: 包含格式非法的来源标识
    """
    if not value:
        return ()
    sources: list[MediaSource] = []
    invalid_sources: list[str] = []
    for item in str(value).split(","):
        raw_source = item.strip()
        if not raw_source:
            continue
        source = normalize_media_source(raw_source)
        if not source:
            invalid_sources.append(raw_source)
        elif source not in sources:
            sources.append(source)
    if invalid_sources:
        raise ValueError(f"不支持的媒体数据源：{', '.join(invalid_sources)}")
    return tuple(sources)


def is_media_source_selected(
        media_source: Optional[MediaSourceSelection],
        source_key: MediaSource,
) -> bool:
    """
    判断请求级媒体数据源集合是否包含当前模块。

    :param media_source: 请求级媒体数据源枚举或枚举元组，空表示不作限制
    :param source_key: 当前模块对应的数据源标识
    :return: 是否包含
    """
    if not media_source:
        return True
    selected_sources = (
        (media_source,)
        if isinstance(media_source, MediaSource)
        else media_source
    )
    return source_key in selected_sources


def is_media_source_enabled(
        media_source: Optional[MediaSourceSelection],
        source_key: MediaSource,
) -> bool:
    """
    判断媒体搜索时数据源是否启用：请求级来源集合优先，未指定时回退到
    全局 SEARCH_SOURCE 多来源配置，两者均未配置时全部启用。

    :param media_source: 请求级媒体数据源枚举或枚举元组
    :param source_key: 当前模块对应的数据源标识
    :return: 是否启用
    """
    if media_source:
        return is_media_source_selected(media_source, source_key)
    configured_search_sources = _search_source_provider()
    if configured_search_sources:
        configured_sources = {
            normalize_media_source(item)
            for item in str(configured_search_sources).split(",")
        }
        return source_key in configured_sources
    return True
