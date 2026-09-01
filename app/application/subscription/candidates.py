"""订阅候选批次与无损路由合同。"""

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from app.application.subscription.contract import SubscriptionSnapshot
from app.domain.context import Context, MediaInfo
from app.foundation import text as text_tools
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaType


CandidateGroups = Dict[str, List[Context]]


@dataclass(slots=True)
class CandidateBatch:
    """一次资源获取产生的完整候选、增量候选与重试候选边界。"""

    batch_id: str
    source: str
    candidates: CandidateGroups
    fresh_candidates: CandidateGroups = field(default_factory=dict)
    retry_candidates: CandidateGroups = field(default_factory=dict)
    sites: tuple[str, ...] = ()
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None

    @classmethod
    def create(
        cls,
        *,
        source: str,
        candidates: CandidateGroups,
        fresh_candidates: Optional[CandidateGroups] = None,
        retry_candidates: Optional[CandidateGroups] = None,
        sites: Optional[List[str]] = None,
        started_at: Optional[datetime] = None,
    ) -> "CandidateBatch":
        """构造已完成获取的候选批次。"""
        return cls(
            batch_id=uuid4().hex,
            source=source,
            candidates=candidates,
            fresh_candidates=fresh_candidates or {},
            retry_candidates=retry_candidates or {},
            sites=tuple(sites or candidates.keys()),
            started_at=started_at or datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )

    @classmethod
    def from_legacy(cls, candidates: CandidateGroups, source: str = "legacy") -> "CandidateBatch":
        """把旧入口传入的完整候选包装为无增量声明的兼容批次。"""
        return cls.create(source=source, candidates=candidates)

    @staticmethod
    def count(groups: CandidateGroups) -> int:
        """统计分站点候选总数。"""
        return sum(len(contexts) for contexts in groups.values())

    def build_index(self) -> "CandidateIndex":
        """基于完整候选构建一次性无损索引。"""
        return CandidateIndex(self.candidates)


class CandidateIndex:
    """一次构建并保持原顺序的订阅候选身份索引。"""

    def __init__(self, candidates: CandidateGroups) -> None:
        """记录候选顺序、明确身份和必须保守处理的候选集合。"""
        self._ordered: list[tuple[str, Context]] = []
        self._by_identity: dict[tuple[str, str], set[int]] = {}
        self._unknown: set[int] = set()
        self._reconcilable: set[int] = set()
        self._explicit_identity: dict[int, tuple[str, str]] = {}
        for domain, contexts in candidates.items():
            for context in contexts:
                position = len(self._ordered)
                self._ordered.append((domain, context))
                media_identity = self.media_identity(getattr(context, "media_info", None))
                meta_identity = self.media_identity(getattr(context, "meta_info", None))
                identities = {identity for identity in (media_identity, meta_identity) if identity}
                for identity in identities:
                    self._by_identity.setdefault(identity, set()).add(position)
                if not media_identity:
                    # 主识别失败时 canonical Match 仍允许标题兜底，不能因标题标签 ID 提前排除。
                    self._unknown.add(position)
                elif meta_identity:
                    self._explicit_identity[position] = meta_identity
                else:
                    # 标题解析未携带显式 ID 的识别冲突仍可能通过同作品证据复核。
                    self._reconcilable.add(position)

    def select_cache_candidates(
        self,
        subscribe: SubscriptionSnapshot,
        *,
        allow_title_match: bool = False,
    ) -> List[Context]:
        """返回严格身份候选，并按需附加显式标记的标题兜底副本。"""
        results: List[Context] = []
        for _domain, context in self._ordered:
            copied = copy.deepcopy(context)
            if self.strict_matches(copied, subscribe):
                results.append(copied)
                continue
            if allow_title_match and self.title_matches(copied, subscribe):
                self.mark_title_candidate(copied, subscribe)
                results.append(copied)
        return results

    def route_for_match(
        self,
        subscribe: SubscriptionSnapshot,
        *,
        domains: Optional[set[str]] = None,
        site_ids: Optional[set[int]] = None,
    ) -> CandidateGroups:
        """保守路由可能命中的候选，并在外部事实查询前应用站点范围。"""
        if subscribe.custom_words:
            positions = set(range(len(self._ordered)))
        else:
            target_identity = self.media_identity(subscribe)
            positions = set(self._unknown)
            positions.update(self._reconcilable)
            if target_identity:
                positions.update(self._by_identity.get(target_identity, set()))
                positions.update(
                    position
                    for position, explicit_identity in self._explicit_identity.items()
                    if explicit_identity == target_identity
                )

        routed: CandidateGroups = {}
        for position, (domain, context) in enumerate(self._ordered):
            if position not in positions:
                continue
            if domains and domain not in domains:
                continue
            torrent_info = getattr(context, "torrent_info", None)
            if site_ids and getattr(torrent_info, "site", None) not in site_ids:
                continue
            if not subscribe.custom_words and (
                not self.media_type_matches(context, subscribe)
                or not self.season_matches(context, subscribe)
            ):
                continue
            routed.setdefault(domain, []).append(context)
        return routed

    @classmethod
    def strict_matches(cls, context: Context, subscribe: SubscriptionSnapshot) -> bool:
        """判断候选自身明确身份、类型和季是否严格命中订阅。"""
        if not cls.media_type_matches(context, subscribe):
            return False
        if not cls.season_matches(context, subscribe):
            return False
        subscribe_identity = cls.media_identity(subscribe)
        return bool(subscribe_identity and subscribe_identity in cls.context_identities(context))

    @classmethod
    def title_matches(cls, context: Context, subscribe: SubscriptionSnapshot) -> bool:
        """仅允许身份缺失候选按标题进入低置信诊断兜底。"""
        if cls.context_identities(context):
            return False
        if not cls.media_type_matches(context, subscribe):
            return False
        if not cls.season_matches(context, subscribe):
            return False
        subscribe_title = cls.normalize_title(subscribe.name)
        if not subscribe_title:
            return False
        meta_info = getattr(context, "meta_info", None)
        torrent_info = getattr(context, "torrent_info", None)
        candidate_titles = (
            getattr(torrent_info, "title", None),
            getattr(meta_info, "title", None),
            getattr(meta_info, "name", None),
        )
        return any(
            subscribe_title in candidate_title
            for candidate_title in (cls.normalize_title(title) for title in candidate_titles)
            if candidate_title
        )

    @staticmethod
    def mark_title_candidate(context: Context, subscribe: SubscriptionSnapshot) -> None:
        """把标题兜底副本标记为目标回填，避免伪装成候选自身识别结果。"""
        context.match_source = "title"
        context.candidate_recognized = False
        context.media_info_is_target = True
        context.media_info = MediaInfo(
            type=subscribe.type,
            title=subscribe.name,
            media_source=subscribe.media_source,
            media_id=subscribe.media_id,
            season=subscribe.season,
        )

    @classmethod
    def media_type_matches(cls, context: Context, subscribe: SubscriptionSnapshot) -> bool:
        """类型已知且冲突时拒绝，缺失类型保持保守候选。"""
        subscribe_type = cls.normalize_media_type(subscribe.type)
        media_info = getattr(context, "media_info", None)
        meta_info = getattr(context, "meta_info", None)
        context_types = {
            cls.normalize_media_type(value)
            for value in (
                getattr(media_info, "type", None),
                getattr(meta_info, "type", None),
            )
        }
        context_types.discard(None)
        return not subscribe_type or not context_types or all(
            context_type == subscribe_type for context_type in context_types
        )

    @classmethod
    def season_matches(cls, context: Context, subscribe: SubscriptionSnapshot) -> bool:
        """仅在资源季信息明确排除目标季时拒绝。"""
        target_season = cls.normalize_int(subscribe.season)
        if target_season is None:
            return True
        meta_info = getattr(context, "meta_info", None)
        explicit_meta_seasons = cls.meta_seasons(meta_info)
        if explicit_meta_seasons:
            return target_season in explicit_meta_seasons
        media_info = getattr(context, "media_info", None)
        media_season = cls.normalize_int(getattr(media_info, "season", None))
        return media_season is None or target_season == media_season

    @classmethod
    def meta_seasons(cls, meta_info) -> set[int]:
        """提取标题解析出的显式季范围。"""
        meta_fields = vars(meta_info) if meta_info else {}
        if "season_list" in meta_fields:
            season_list = {
                season
                for season in (
                    cls.normalize_int(item)
                    for item in (meta_fields.get("season_list") or [])
                )
                if season is not None
            }
            if season_list:
                return season_list
        begin_season = cls.normalize_int(getattr(meta_info, "begin_season", None))
        end_season = cls.normalize_int(getattr(meta_info, "end_season", None))
        if begin_season is not None and end_season is not None:
            start, end = sorted((begin_season, end_season))
            return set(range(start, end + 1))
        if begin_season is not None:
            return {begin_season}
        if end_season is not None:
            return {end_season}
        return set()

    @classmethod
    def context_identities(cls, context: Context) -> set[tuple[str, str]]:
        """提取候选媒体信息与标题标签中的通用媒体身份。"""
        identities = {
            cls.media_identity(getattr(context, "media_info", None)),
            cls.media_identity(getattr(context, "meta_info", None)),
        }
        return {identity for identity in identities if identity}

    @staticmethod
    def media_identity(media) -> Optional[tuple[str, str]]:
        """把动态媒体对象的身份归一为可索引键。"""
        source, media_id = resolve_media_identity(media=media)
        if not source or not media_id:
            return None
        return str(source), media_id

    @staticmethod
    def normalize_int(value) -> Optional[int]:
        """将季号等动态字段转为整数。"""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def normalize_media_type(value) -> Optional[str]:
        """统一媒体类型枚举与字符串形态。"""
        if isinstance(value, MediaType):
            value = value.value
        if value == MediaType.UNKNOWN.value:
            return None
        return value

    @staticmethod
    def normalize_title(value) -> str:
        """归一标题用于低置信标题匹配。"""
        return (text_tools.normalize_upper(value or "") or "").strip()
