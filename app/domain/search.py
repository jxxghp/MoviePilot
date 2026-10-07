"""分页搜索的收集与连续进度语义，不依赖站点请求或持久化。"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional

# 某来源距上次请求超过该时长（中断后恢复）时，先接着拉原来的下一页，再重拉一次第一页补入新资源。
HEAD_REFRESH_SECONDS = 900
# 每个来源最多请求的页数，防止异常站点或缺陷导致无限翻页。
MAX_SEARCH_PAGES = 100


def search_resource_id(torrent: Any) -> str:
    """资源去重键，与原搜索结果去重一致：站点名 + 标题 + 描述。"""
    return f"{torrent.site_name}_{torrent.title}_{torrent.description}"


def search_page_signature(resource_ids: Iterable[str]) -> Optional[str]:
    """按原搜索去重键生成整页摘要，保留顺序与重复项；空页返回 None。"""
    page_ids = tuple(resource_ids)
    if not page_ids:
        return None
    return hashlib.sha256(json.dumps(page_ids, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SearchResourceEvidence:
    """一条资源的去重键及匹配目标；None 表示识别尚不能给出结论。"""

    resource_id: str
    targets: Optional[frozenset[str]]
    # 资源覆盖的各季最大集号（季号字符串, 集号），不限于本轮缺集，用于判断站点已收录范围。
    latest: tuple[tuple[str, int], ...] = ()


@dataclass(slots=True)
class SearchSourceCursor:
    """一个站点实际查询的连续页进度；重拉第一页只补入新资源，不拼接缺席证据。"""

    next_page: int = 0
    exhausted: bool = False
    failed: bool = False
    seen: set[str] = field(default_factory=set)
    closed: set[str] = field(default_factory=set)
    # 是否返回过任何资源；只需要有无，不保存逐条去重键以控制检查点体积。
    found: bool = False
    last_request_at: float = 0
    # 中断恢复后待重拉第一页；先拉下一页再重拉，保证每次重拉前至少推进一页，不会卡在第一页。
    refresh_pending: bool = False
    head_refresh: bool = False
    # 上一页的整页摘要，只用于识别站点越过末页后重复返回同一页。
    last_page_signature: Optional[str] = None
    error: Optional[str] = None
    failure_outcome: Optional[str] = None
    page_limit: Optional[int] = None

    @property
    def limit_reached(self) -> bool:
        """配置范围或最大页数已检查不代表站点搜尽，下载失败也不能突破该范围。"""
        return self.next_page >= min(self.page_limit or MAX_SEARCH_PAGES, MAX_SEARCH_PAGES)

    def page_to_request(self, now: float) -> int:
        """中断超过门槛后，先拉原来的下一页，下一次请求再重拉第一页；连续翻页不会触发。"""
        if (not self.refresh_pending and not self.head_refresh and self.next_page
                and now - self.last_request_at >= HEAD_REFRESH_SECONDS):
            self.refresh_pending = True
        return 0 if self.head_refresh else self.next_page

    def cancel_head_refresh(self) -> None:
        """请求失败时放弃本次重拉第一页。"""
        self.refresh_pending = False
        self.head_refresh = False

    def accept_page(
        self, *, page: int, evidence: list[SearchResourceEvidence], targets: set[str],
        exhausted: bool, now: float,
    ) -> bool:
        """收集一页；含识别未知资源的页面照常推进，但不作为任何目标的缺席证据。"""
        self.last_request_at = now
        self.error = None
        if self.head_refresh:
            # 重拉的第一页只补入新资源：已见过的资源（包括第一页置顶）按去重键只算一份，
            # 也不和历史进度拼成“下一页没有该集”的缺席证据。
            self.found = self.found or bool(evidence)
            self.head_refresh = False
            return True
        if page != self.next_page:
            raise ValueError("搜索结果不是当前连续页")
        signature = search_page_signature(item.resource_id for item in evidence)
        if signature and signature == self.last_page_signature:
            # 与上一页完全相同：站点越过末页或实际不支持分页，按空页即末页处理。
            evidence, signature, exhausted = [], None, True
        effective: set[str] = set()
        uncertain = False
        self.found = self.found or bool(evidence)
        for item in evidence:
            if item.targets is None:
                uncertain = True
            else:
                effective.update(item.targets)
        if not uncertain:
            self.closed.update((self.seen & targets) - effective)
        self.seen.update(effective)
        self.next_page = page + 1
        self.last_page_signature = signature
        self.exhausted = exhausted
        if self.refresh_pending:
            self.refresh_pending = False
            self.head_refresh = not exhausted
        return True

    def drives_search(self, targets: set[str], fallback: set[str]) -> bool:
        """完整兜底可重开 closed 来源，但不能越过用户配置的页数范围。"""
        return not (self.exhausted or self.failed or self.limit_reached) and bool((targets - self.closed) | (targets & fallback))


@dataclass(slots=True)
class SearchCollection:
    """各来源共用目标收集屏障，成功目标不再驱动分页。"""

    targets: set[str]
    sources: dict[str, SearchSourceCursor] = field(default_factory=dict)
    settled: set[str] = field(default_factory=set)
    fallback: set[str] = field(default_factory=set)
    # 本轮任一来源出现过的本作品各季最大集号，键为季号字符串。
    released: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> set[str]:
        """仍需择优或寻找候选的目标。"""
        return self.targets - self.settled

    def observe(self, evidence: Iterable[SearchResourceEvidence]) -> None:
        """记录站点已收录范围；只增不减，较深页面出现更新集数时相应目标恢复正常收集。"""
        for item in evidence:
            for season, episode in item.latest:
                if episode > self.released.get(season, 0):
                    self.released[season] = episode

    def unreleased(self) -> set[str]:
        """超过本轮各来源已见最大集号的缺集视为站点尚未收录：各来源只查第一页，之后交给订阅模式追更。

        判断依据是站点资源而不是播出时间：已播出但站点还没有的集同样等订阅模式抓取。
        站点结果一般按上传时间倒序，最新的集出现在前面；若较新的集只出现在更深页（如第一页被重新上传的
        旧集占满），本轮可能把它当作未收录，留待下次搜索或订阅模式，这是为避免连载剧翻遍全站接受的取舍。
        同季没有任何资源出现时无法判断，仍按正常规则收集。
        """
        result = set()
        for target in self.remaining:
            season, _, episode = target.partition(":")
            latest = self.released.get(season)
            if latest is not None and episode.isdigit() and int(episode) > latest:
                result.add(target)
        return result

    def ready(self, *, full: bool = False) -> set[str]:
        """正常来源均结束收集才就绪，完整模式及兜底必须等真实搜尽；站点未收录目标查过第一页即就绪。"""
        if not self.sources:
            return set()
        unreleased = self.unreleased()
        return {
            target for target in self.remaining
            if all(
                source.exhausted or source.failed or source.limit_reached
                or (target in unreleased and source.next_page > 0)
                or (not full and target not in self.fallback and target in source.closed)
                for source in self.sources.values()
            )
        }

    def active_sources(self, *, full: bool = False) -> list[str]:
        """只让未满足目标驱动共用游标，完整模式不使用智能关闭；站点未收录目标只驱动第一页。"""
        driving = self.remaining - self.unreleased()
        fallback = driving if full else self.fallback
        return [key for key, source in self.sources.items()
                if source.drives_search(driving if source.next_page else self.remaining, fallback)]

    @property
    def pending(self) -> set[str]:
        """仍值得在本轮继续寻找的目标；只剩站点未收录目标时本轮结束并释放检查点。"""
        return self.remaining - self.unreleased()

    def settle(self, targets: set[str]) -> None:
        """仅对已确认提交或已经存在的目标结算。"""
        self.settled.update(targets & self.targets)
        self.fallback.difference_update(targets)

    def deepen(self, targets: set[str]) -> None:
        """无合格候选或明确失败后，从来源当前进度继续完整兜底；站点未收录目标不兜底翻页。"""
        self.fallback.update(targets & self.pending)
