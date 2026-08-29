"""跨媒体来源身份投影与匹配 owner。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Final, Optional, cast

from app.chain.media.contract import _MediaOwnerBase
from app.domain.metainfo import MetaInfo
from app.domain.projection.bangumi import resolve_media_type as resolve_bangumi_media_type
from app.schemas.event import (
    MediaRecognizeConvertEventData as _SchemaMediaRecognizeConvertEventData,
)
from app.schemas.media import normalize_media_source, resolve_media_identity
from app.schemas.types import ChainEventType, MediaSource, MediaType

_ProjectionPayload = dict[str, Any]
_ProjectionSource = Mapping[str, Any]


class _ProjectionRule(Enum):
    """标识内置身份投影规则或插件事件兜底。"""

    DOUBAN_TO_TMDB = auto()
    BANGUMI_TO_TMDB = auto()
    TMDB_TO_DOUBAN = auto()
    BANGUMI_TO_DOUBAN = auto()
    EVENT = auto()


_PROJECTION_RULES: Final = {
    (MediaSource.Douban, MediaSource.TMDB): _ProjectionRule.DOUBAN_TO_TMDB,
    (MediaSource.Bangumi, MediaSource.TMDB): _ProjectionRule.BANGUMI_TO_TMDB,
    (MediaSource.TMDB, MediaSource.Douban): _ProjectionRule.TMDB_TO_DOUBAN,
    (MediaSource.Bangumi, MediaSource.Douban): _ProjectionRule.BANGUMI_TO_DOUBAN,
}
_NUMERIC_SOURCE_RULES: Final = {
    _ProjectionRule.BANGUMI_TO_TMDB,
    _ProjectionRule.TMDB_TO_DOUBAN,
    _ProjectionRule.BANGUMI_TO_DOUBAN,
}


@dataclass(frozen=True)
class _ProjectionPlan:
    """保存一次身份投影的标准化输入和确定规则。"""

    rule: _ProjectionRule
    source: MediaSource
    target: MediaSource
    media_id: str
    media_type: Optional[MediaType]
    season: Optional[int]


@dataclass(frozen=True)
class _TmdbMatch:
    """描述由来源详情生成的 TMDB 名称匹配请求。"""

    names: tuple[str, ...]
    year: Optional[str]
    media_type: MediaType
    season: Optional[int]
    include_season: bool = False


@dataclass(frozen=True)
class _DoubanMatch:
    """描述由来源详情生成的豆瓣匹配请求。"""

    name: str
    year: Optional[str]
    media_type: Optional[MediaType]
    season: Optional[int] = None
    imdb_id: Optional[str] = None


_ProjectionMatch = _TmdbMatch | _DoubanMatch


def _optional_text(value: object) -> Optional[str]:
    """把来源字段标准化为非空文本，避免把空值传给标题解析器。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique_names(values: Sequence[object]) -> tuple[str, ...]:
    """按来源优先级稳定去重可用于匹配的非空标题。"""
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _optional_text(value)
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return tuple(names)


def _build_projection_plan(
    target_source: MediaSource,
    media_source: MediaSource,
    media_id: str,
    media_type: Optional[MediaType],
    season: Optional[int],
) -> Optional[_ProjectionPlan]:
    """标准化身份并确定内置规则；不支持的组合保留给插件事件。"""
    target = normalize_media_source(target_source)
    source, normalized_id = resolve_media_identity(
        media_source=media_source,
        media_id=media_id,
    )
    if not target or not source or not normalized_id:
        return None
    rule = _PROJECTION_RULES.get((source, target), _ProjectionRule.EVENT)
    if rule in _NUMERIC_SOURCE_RULES and not normalized_id.isdigit():
        return None
    return _ProjectionPlan(
        rule=rule,
        source=source,
        target=target,
        media_id=normalized_id,
        media_type=media_type,
        season=season,
    )


def _build_douban_tmdb_match(
    plan: _ProjectionPlan,
    source_info: _ProjectionSource,
) -> Optional[_TmdbMatch]:
    """把豆瓣详情投影为 TMDB 名称、年份、类型和季请求。"""
    title = _optional_text(source_info.get("title"))
    if not title:
        return None
    meta = MetaInfo(title=title)
    original_title = _optional_text(source_info.get("original_title"))
    original_meta = MetaInfo(title=original_title) if original_title else meta
    source_type = source_info.get("media_type")
    inferred_type = (
        source_type
        if isinstance(source_type, MediaType)
        else MediaType.MOVIE
        if source_info.get("type") == "movie"
        else MediaType.TV
    )
    selected_season = plan.season if plan.season is not None else meta.begin_season
    return _TmdbMatch(
        names=_unique_names((original_meta.name, meta.cn_name, meta.en_name)),
        year=_optional_text(source_info.get("year")),
        media_type=plan.media_type or inferred_type,
        season=selected_season,
        include_season=True,
    )


def _build_bangumi_tmdb_match(
    plan: _ProjectionPlan,
    source_info: _ProjectionSource,
) -> Optional[_TmdbMatch]:
    """把 Bangumi 详情投影为 TMDB 名称匹配请求。"""
    title = _optional_text(source_info.get("name"))
    if not title:
        return None
    meta = MetaInfo(title=title)
    localized_title = _optional_text(source_info.get("name_cn"))
    localized_meta = MetaInfo(title=localized_title) if localized_title else meta
    selected_season = plan.season if plan.season is not None else meta.begin_season
    return _TmdbMatch(
        names=_unique_names((localized_meta.name, meta.name)),
        year=MediaProjectionOwner._extract_year_from_bangumi(source_info),
        media_type=plan.media_type or resolve_bangumi_media_type(source_info),
        season=selected_season,
    )


def _build_tmdb_douban_match(
    plan: _ProjectionPlan,
    source_info: _ProjectionSource,
) -> Optional[_DoubanMatch]:
    """把 TMDB 详情投影为豆瓣标题、年份和外部身份请求。"""
    name = _optional_text(source_info.get("title") or source_info.get("name"))
    if not name:
        return None
    external_ids = source_info.get("external_ids")
    imdb_id = (
        _optional_text(external_ids.get("imdb_id"))
        if isinstance(external_ids, Mapping)
        else None
    )
    return _DoubanMatch(
        name=name,
        year=MediaProjectionOwner._extract_year_from_tmdb(source_info, plan.season),
        media_type=plan.media_type,
        imdb_id=imdb_id,
    )


def _build_bangumi_douban_match(
    plan: _ProjectionPlan,
    source_info: _ProjectionSource,
) -> Optional[_DoubanMatch]:
    """把 Bangumi 详情投影为豆瓣名称、年份、类型和季请求。"""
    title = _optional_text(source_info.get("name_cn") or source_info.get("name"))
    if not title:
        return None
    meta = MetaInfo(title=title)
    selected_season = plan.season if plan.season is not None else meta.begin_season
    return _DoubanMatch(
        name=meta.name,
        year=MediaProjectionOwner._extract_year_from_bangumi(source_info),
        media_type=plan.media_type or resolve_bangumi_media_type(source_info),
        season=selected_season,
    )


def _build_projection_match(
    plan: _ProjectionPlan,
    source_info: _ProjectionSource,
) -> Optional[_ProjectionMatch]:
    """依据已确定规则纯计算目标来源所需的匹配参数。"""
    builders = {
        _ProjectionRule.DOUBAN_TO_TMDB: _build_douban_tmdb_match,
        _ProjectionRule.BANGUMI_TO_TMDB: _build_bangumi_tmdb_match,
        _ProjectionRule.TMDB_TO_DOUBAN: _build_tmdb_douban_match,
        _ProjectionRule.BANGUMI_TO_DOUBAN: _build_bangumi_douban_match,
    }
    builder = builders.get(plan.rule)
    return builder(plan, source_info) if builder else None


def _load_projection_source(
    owner: MediaProjectionOwner,
    plan: _ProjectionPlan,
) -> Optional[_ProjectionPayload]:
    """同步读取规则指定的来源详情。"""
    if plan.rule == _ProjectionRule.DOUBAN_TO_TMDB:
        return owner.douban_info(doubanid=plan.media_id, mtype=plan.media_type)
    if plan.rule in {
        _ProjectionRule.BANGUMI_TO_TMDB,
        _ProjectionRule.BANGUMI_TO_DOUBAN,
    }:
        return owner.bangumi_info(bangumiid=int(plan.media_id))
    # 旧模块入口运行时允许 mtype=None；cast 仅适配 ChainBase 过窄的静态签名。
    return owner.tmdb_info(
        tmdbid=int(plan.media_id),
        mtype=cast(MediaType, plan.media_type),
    )


async def _async_load_projection_source(
    owner: MediaProjectionOwner,
    plan: _ProjectionPlan,
) -> Optional[_ProjectionPayload]:
    """异步读取规则指定的来源详情。"""
    if plan.rule == _ProjectionRule.DOUBAN_TO_TMDB:
        source_info = await owner.async_douban_info(
            doubanid=plan.media_id,
            mtype=plan.media_type,
        )
    elif plan.rule in {
        _ProjectionRule.BANGUMI_TO_TMDB,
        _ProjectionRule.BANGUMI_TO_DOUBAN,
    }:
        source_info = await owner.async_bangumi_info(bangumiid=int(plan.media_id))
    else:
        source_info = await owner.async_tmdb_info(
            tmdbid=int(plan.media_id),
            mtype=cast(MediaType, plan.media_type),
        )
    return source_info


def _apply_projection_match(
    owner: MediaProjectionOwner,
    match: _ProjectionMatch,
) -> Optional[_ProjectionPayload]:
    """同步执行纯匹配参数，并隔离对来源模块返回对象的修改。"""
    if isinstance(match, _TmdbMatch):
        result = owner._match_tmdb_with_names(
            meta_names=match.names,
            year=match.year,
            mtype=match.media_type,
            season=match.season,
        )
        if result and match.include_season:
            return {**result, "season": match.season}
        return result
    return owner.match_doubaninfo(
        name=match.name,
        year=match.year,
        mtype=match.media_type,
        imdbid=match.imdb_id,
        season=match.season,
    )


async def _async_apply_projection_match(
    owner: MediaProjectionOwner,
    match: _ProjectionMatch,
) -> Optional[_ProjectionPayload]:
    """异步执行纯匹配参数，并隔离对来源模块返回对象的修改。"""
    if isinstance(match, _TmdbMatch):
        result = await owner._async_match_tmdb_with_names(
            meta_names=match.names,
            year=match.year,
            mtype=match.media_type,
            season=match.season,
        )
        if result and match.include_season:
            return {**result, "season": match.season}
        return result
    return await owner.async_match_doubaninfo(
        name=match.name,
        year=match.year,
        mtype=match.media_type,
        imdbid=match.imdb_id,
        season=match.season,
    )


def _convert_builtin_projection(
    owner: MediaProjectionOwner,
    plan: _ProjectionPlan,
) -> Optional[_ProjectionPayload]:
    """同步执行来源读取、纯投影和目标匹配三阶段流程。"""
    source_info = _load_projection_source(owner, plan)
    match = _build_projection_match(plan, source_info) if source_info else None
    return _apply_projection_match(owner, match) if match else None


async def _async_convert_builtin_projection(
    owner: MediaProjectionOwner,
    plan: _ProjectionPlan,
) -> Optional[_ProjectionPayload]:
    """异步执行来源读取、纯投影和目标匹配三阶段流程。"""
    source_info = await _async_load_projection_source(owner, plan)
    match = _build_projection_match(plan, source_info) if source_info else None
    return await _async_apply_projection_match(owner, match) if match else None


class MediaProjectionOwner(_MediaOwnerBase):
    """跨媒体来源身份投影与匹配 owner。"""

    def _dispatch_projection_event(
        self,
        plan: _ProjectionPlan,
    ) -> Optional[_ProjectionPayload]:
        """同步把非内置来源组合交给官方插件转换事件。"""
        event_data = _SchemaMediaRecognizeConvertEventData(
            media_source=plan.source,
            media_id=plan.media_id,
            target_media_source=plan.target,
        )
        event = self.eventmanager.send_event(
            ChainEventType.MediaRecognizeConvert,
            event_data,
        )
        return (
            cast(_ProjectionPayload, event_data.media_dict)
            if event and event_data.media_dict
            else None
        )

    async def _async_dispatch_projection_event(
        self,
        plan: _ProjectionPlan,
    ) -> Optional[_ProjectionPayload]:
        """异步把非内置来源组合交给官方插件转换事件。"""
        event_data = _SchemaMediaRecognizeConvertEventData(
            media_source=plan.source,
            media_id=plan.media_id,
            target_media_source=plan.target,
        )
        event = await self.eventmanager.async_send_event(
            ChainEventType.MediaRecognizeConvert,
            event_data,
        )
        return (
            cast(_ProjectionPayload, event_data.media_dict)
            if event and event_data.media_dict
            else None
        )

    def convert_media_identity(
        self,
        target_source: MediaSource,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        season: Optional[int] = None,
    ) -> Optional[_ProjectionPayload]:
        """使用统一媒体身份在影视来源之间同步转换原始详情。"""
        plan = _build_projection_plan(
            target_source, media_source, media_id, mtype, season
        )
        if not plan:
            return None
        if plan.rule == _ProjectionRule.EVENT:
            return MediaProjectionOwner._dispatch_projection_event(self, plan)
        return _convert_builtin_projection(self, plan)

    @staticmethod
    def _extract_year_from_bangumi(
        bangumiinfo: Mapping[str, Any],
    ) -> Optional[str]:
        """从 Bangumi 上映日期中提取四位年份。"""
        release_date = _optional_text(
            bangumiinfo.get("date") or bangumiinfo.get("air_date")
        )
        return release_date[:4] if release_date else None

    @staticmethod
    def _extract_year_from_tmdb(
        tmdbinfo: Mapping[str, Any],
        season: Optional[int] = None,
    ) -> Optional[str]:
        """优先从 TMDB 首映日期、再从指定季日期中提取年份。"""
        release_date = _optional_text(tmdbinfo.get("release_date"))
        if release_date:
            return release_date[:4]
        seasons = tmdbinfo.get("seasons")
        if not isinstance(seasons, Sequence) or season is None:
            return None
        for season_info in seasons:
            if not isinstance(season_info, Mapping):
                continue
            air_date = _optional_text(season_info.get("air_date"))
            if season_info.get("season_number") == season and air_date:
                return air_date[:4]
        return None

    def _match_tmdb_with_names(
        self,
        meta_names: Sequence[str],
        year: Optional[str],
        mtype: MediaType,
        season: Optional[int] = None,
    ) -> Optional[_ProjectionPayload]:
        """按稳定名称顺序同步匹配第一条 TMDB 结果。"""
        for name in meta_names:
            tmdbinfo = self.match_tmdbinfo(
                name=name,
                year=year,
                mtype=mtype,
                season=season,
            )
            if tmdbinfo:
                return tmdbinfo
        return None

    async def _async_match_tmdb_with_names(
        self,
        meta_names: Sequence[str],
        year: Optional[str],
        mtype: MediaType,
        season: Optional[int] = None,
    ) -> Optional[_ProjectionPayload]:
        """按稳定名称顺序异步匹配第一条 TMDB 结果。"""
        for name in meta_names:
            tmdbinfo = await self.async_match_tmdbinfo(
                name=name,
                year=year,
                mtype=mtype,
                season=season,
            )
            if tmdbinfo:
                return tmdbinfo
        return None

    async def async_convert_media_identity(
        self,
        target_source: MediaSource,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[MediaType] = None,
        season: Optional[int] = None,
    ) -> Optional[_ProjectionPayload]:
        """使用统一媒体身份在影视来源之间异步转换原始详情。"""
        plan = _build_projection_plan(
            target_source, media_source, media_id, mtype, season
        )
        if not plan:
            return None
        if plan.rule == _ProjectionRule.EVENT:
            return await MediaProjectionOwner._async_dispatch_projection_event(
                self, plan
            )
        return await _async_convert_builtin_projection(self, plan)
