"""资源过滤、匹配、投影与去重 owner。"""

import copy
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from app.application.configuration import get_configured_system_config
from app.application.torrent.download import TorrentHelper
from app.chain.media import MediaChain
from app.chain.search.contract import _SearchOwnerBase
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.domain.music import MusicMatch, match_music_resource
from app.domain.search import SearchResourceEvidence, search_resource_id
from app.runtime.log import logger
from app.runtime.progress import ProgressHelper
from app.runtime.stop import runtime_stop_state
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaSource, MediaType, ProgressKey, SystemConfigKey

SiteKey = Tuple[Optional[int], Optional[str]]
DisambiguationCache = Dict[Tuple[str, str, str], Optional[MediaInfo]]


@dataclass(frozen=True, slots=True)
class MatchedTorrent:
    """保存资源解析证据和身份匹配结果，不从目标媒体反向生成元数据。"""

    torrent: TorrentInfo
    meta: MetaBase
    source: str
    music_match: Optional[MusicMatch] = None


def _site_torrents(torrents: List[TorrentInfo]) -> Dict[SiteKey, List[TorrentInfo]]:
    """按站点归集资源，并保留站点及资源的首次出现顺序。"""
    grouped: Dict[SiteKey, List[TorrentInfo]] = {}
    for torrent in torrents:
        grouped.setdefault((torrent.site, torrent.site_name), []).append(torrent)
    return grouped


def _filter_site_torrents(
    owner: _SearchOwnerBase,
    torrents: List[TorrentInfo],
    mediainfo: MediaInfo | MusicInfo,
    rule_groups: List[str],
    filter_params: Dict[str, str],
    diagnostics: Counter[str],
) -> List[TorrentInfo]:
    """按一个站点执行附加参数和优先级规则过滤。"""
    filtered = torrents
    if filter_params:
        helper = cast(Callable[[], TorrentHelper], TorrentHelper)()
        filtered = [torrent for torrent in filtered if helper.filter_torrent(torrent, filter_params)]
        diagnostics["filter_params"] += len(torrents) - len(filtered)
    if rule_groups and filtered:
        count = len(filtered)
        filtered = (
            owner.filter_torrents(
                rule_groups=rule_groups,
                torrent_list=filtered,
                mediainfo=mediainfo,
            )
            or []
        )
        diagnostics["filter_rules"] += count - len(filtered)
    return filtered


def _filter_torrents(
    owner: _SearchOwnerBase,
    torrents: List[TorrentInfo],
    mediainfo: MediaInfo | MusicInfo,
    rule_groups: List[str],
    filter_params: Dict[str, str],
    progress: ProgressHelper,
    diagnostics: Counter[str],
) -> List[TorrentInfo]:
    """逐站点过滤资源，避免在调用方工作线程内再创建线程池。"""
    if not filter_params and not rule_groups:
        return torrents

    grouped = _site_torrents(torrents)
    retained_ids: set[int] = set()
    total = len(grouped)
    for count, site_items in enumerate(grouped.values(), start=1):
        retained_ids.update(
            id(torrent)
            for torrent in _filter_site_torrents(
                owner=owner,
                torrents=site_items,
                mediainfo=mediainfo,
                rule_groups=rule_groups,
                filter_params=filter_params,
                diagnostics=diagnostics,
            )
        )
        progress.update(
            value=count / total * 50,
            text=f"正在过滤，已完成 {count} / {total} 个站点 ...",
        )
    return [torrent for torrent in torrents if id(torrent) in retained_ids]


def _torrent_meta(torrent: TorrentInfo, custom_words: List[str], mediainfo: MediaInfo | MusicInfo) -> MetaBase:
    """解析一条资源的元数据，并记录识别词改写结果。"""
    meta = MetaInfo(
        title=torrent.title,
        subtitle=torrent.description,
        custom_words=custom_words,
        mtype=mediainfo.type,
    )
    if torrent.title != meta.org_string:
        logger.info(f"种子名称应用识别词后发生改变：{torrent.title} => {meta.org_string}")
    return meta


def _disambiguation_key(meta: MetaBase) -> Tuple[str, str, str]:
    """构造同名候选识别缓存键。"""
    return meta.cn_name or "", meta.en_name or "", meta.year or ""


def _same_work_matched(
    torrent: TorrentInfo,
    torrent_meta: MetaBase,
    mediainfo: MediaInfo,
    cache: DisambiguationCache,
) -> bool:
    """确认无年份别名候选与目标媒体属于同一作品。"""
    key = _disambiguation_key(torrent_meta)
    if key not in cache:
        cache[key] = MediaChain().recognize_by_meta(torrent_meta, obtain_images=False)
    candidate = cache[key]
    if not candidate:
        logger.info(f"{torrent.site_name} - {torrent.title} 仅通过无年份别名命中且候选媒体身份无法确认，已跳过")
        return False
    matched, evidence = TorrentHelper.match_same_work_evidence(
        target_mediainfo=mediainfo,
        candidate_mediainfo=candidate,
        torrent_meta=torrent_meta,
    )
    if not matched:
        logger.info(f"{torrent.site_name} - {torrent.title} 无年份同名候选未通过消歧：{evidence}")
    return matched


def _match_source(
    torrent: TorrentInfo,
    torrent_meta: MetaBase,
    mediainfo: MediaInfo,
    cache: DisambiguationCache,
) -> Optional[str]:
    """返回资源命中的身份依据，未匹配时返回 None。"""
    torrent_source, torrent_media_id = resolve_media_identity(media=torrent)
    if torrent_source == MediaSource.IMDb and mediainfo.imdb_id and torrent_media_id == str(mediainfo.imdb_id):
        logger.info(f"{mediainfo.title} 通过IMDBID匹配到资源：{torrent.site_name} - {torrent.title}")
        return str(MediaSource.IMDb)

    if not TorrentHelper.match_torrent(
        mediainfo=mediainfo,
        torrent_meta=torrent_meta,
        torrent=torrent,
    ):
        return None
    if TorrentHelper.requires_identity_disambiguation(
        mediainfo=mediainfo,
        torrent_meta=torrent_meta,
    ) and not _same_work_matched(
        torrent=torrent,
        torrent_meta=torrent_meta,
        mediainfo=mediainfo,
        cache=cache,
    ):
        return None
    return "title"


def _match_torrents(
    torrents: List[TorrentInfo],
    mediainfo: MediaInfo | MusicInfo,
    season_episodes: Dict[int, List[int]],
    custom_words: List[str],
    progress: ProgressHelper,
    include_candidates: bool,
    diagnostics: Counter[str],
) -> List[MatchedTorrent]:
    """按输入顺序匹配资源，并复用同名候选的识别结果。"""
    logger.info(f"开始匹配结果 类型：{mediainfo.type.value}，标题：{mediainfo.title}，别名：{mediainfo.names}")
    progress.update(value=51, text=f"开始匹配，总 {len(torrents)} 个资源 ...")
    matches: List[MatchedTorrent] = []
    cache: DisambiguationCache = {}
    total = len(torrents)
    for count, torrent in enumerate(torrents, start=1):
        if runtime_stop_state.is_system_stopped:
            break
        progress.update(
            value=count / total * 96,
            text=f"正在匹配 {torrent.site_name}，已完成 {count} / {total} ...",
        )
        if not torrent.title:
            diagnostics["title_missing"] += 1
            continue
        meta = _torrent_meta(torrent=torrent, custom_words=custom_words, mediainfo=mediainfo)
        if season_episodes and not TorrentHelper.match_season_episodes(
            torrent=torrent,
            meta=meta,
            season_episodes=season_episodes,
        ):
            diagnostics["scope_mismatch"] += 1
            continue
        if isinstance(mediainfo, MusicInfo):
            match = match_music_resource(
                mediainfo, torrent.title, torrent.description, torrent.category,
                meta=cast(MetaMusic, meta),
            )
            diagnostics[match.reason] += 1
            if match.status == "exact" or (include_candidates and match.status != "rejected"):
                matches.append(MatchedTorrent(torrent, meta, "title", match))
            else:
                logger.debug(f"音乐资源 {torrent.site_name} - {torrent.title} 未通过匹配：{match.reason}")
            continue
        source = _match_source(
            torrent=torrent,
            torrent_meta=meta,
            mediainfo=mediainfo,
            cache=cache,
        )
        if source:
            diagnostics["matched"] += 1
            matches.append(MatchedTorrent(torrent, meta, source))
        else:
            diagnostics["identity_mismatch"] += 1
    logger.info(f"匹配完成，共匹配到 {len(matches)} 个资源")
    progress.update(value=97, text=f"匹配完成，共匹配到 {len(matches)} 个资源")
    return matches


def _context_media(mediainfo: MediaInfo | MusicInfo) -> MediaInfo | MusicInfo:
    """复制并裁剪上下文媒体信息，避免修改调用方持有的目标对象。"""
    context_media = copy.copy(mediainfo)
    context_media.clear()
    return context_media


def _build_contexts(matches: List[MatchedTorrent], mediainfo: MediaInfo | MusicInfo) -> List[Context]:
    """将匹配结果投影为搜索上下文。"""
    context_media = _context_media(mediainfo)
    return [
        Context(
            torrent_info=match.torrent,
            media_info=context_media if match.music_match is None or match.music_match.status == "exact" else None,
            meta_info=match.meta,
            resource_source="search",
            match_source=match.source,
            candidate_recognized=False,
            media_info_is_target=match.music_match is None or match.music_match.status == "exact",
            match_status="exact" if match.music_match is None or match.music_match.status == "exact" else "candidate",
            match_reason=match.music_match.reason if match.music_match else "matched",
        )
        for match in matches
    ]


def _resource_targets(meta: MetaBase, targets: set[str], media_type: object) -> set[str]:
    """资源集范围只与本轮缺集相交，整季资源覆盖该季的已知必要目标。"""
    if media_type == MediaType.MOVIE:
        return targets & {"movie"}
    matched = set()
    seasons = set(meta.season_list)
    episodes = set(meta.episode_list)
    for target in targets:
        season, episode = target.split(":", 1)
        if int(season) in seasons and (episode == "season" or not episodes or int(episode) in episodes):
            matched.add(target)
    return matched


def _resource_latest(meta: MetaBase, media_type: object) -> list[list[Any]]:
    """资源覆盖的各季最大集号；整季包没有集号，不参与站点已收录范围判断。"""
    if media_type == MediaType.MOVIE or not meta.episode_list:
        return []
    return [[str(season), max(meta.episode_list)] for season in meta.season_list]


def stored_meta(record: dict[str, Any]) -> MetaBase:
    """从识别缓存恢复元数据而不重跑识别词解析；JSON 中的类型须还原为枚举。"""
    meta = object.__new__(MetaBase)
    meta.__dict__.update(record["meta"])
    if isinstance(meta.type, str):
        meta.type = MediaType(meta.type)
    return meta


class SearchResultOwner(_SearchOwnerBase):
    """负责搜索资源的过滤、匹配、上下文投影与去重。"""

    @staticmethod
    def _resource_identity(torrent: TorrentInfo) -> str:
        """与原搜索结果去重一致的资源键。"""
        return search_resource_id(torrent)

    @staticmethod
    def _identify_page(
        *, torrents: List[TorrentInfo], mediainfo: MediaInfo, targets: set[str],
        custom_words: List[str], cache: dict[str, Any], disambiguation: DisambiguationCache,
    ) -> tuple[list[SearchResourceEvidence], dict[str, Context]]:
        """过滤前识别新增资源；识别未知不会成为缺席证据或永久负缓存。"""
        evidence = []
        contexts = {}
        for torrent in torrents:
            # 去重键含标题和描述，内容变化即为新资源；识别规则变化由检查点合同触发重搜。
            key = SearchResultOwner._resource_identity(torrent)
            record = cache.get(key)
            if record is None or record["targets"] is None:
                meta = _torrent_meta(torrent=torrent, custom_words=custom_words, mediainfo=mediainfo)
                source = _match_source(torrent, meta, mediainfo, disambiguation) if torrent.title else None
                unknown = not torrent.title or (_disambiguation_key(meta) in disambiguation
                                               and disambiguation[_disambiguation_key(meta)] is None)
                matched_targets = _resource_targets(meta, targets, mediainfo.type) if source else set()
                record = {"targets": None if unknown else sorted(matched_targets),
                          "source": source, "meta": vars(meta).copy(),
                          "latest": _resource_latest(meta, mediainfo.type) if source and not unknown else []}
                cache[key] = record
            matches = None if record["targets"] is None else frozenset(record["targets"])
            latest = tuple((str(season), int(episode)) for season, episode in record.get("latest") or [])
            evidence.append(SearchResourceEvidence(key, matches, latest))
            if record["source"]:
                meta = stored_meta(record)
                meta.type = mediainfo.type
                contexts[key] = _build_contexts([MatchedTorrent(torrent, meta, record["source"])], mediainfo)[0]
        # 临时识别失败下一页允许重试；成功消歧在本轮复用。
        for ambiguity_key in [entry for entry, value in disambiguation.items() if value is None]:
            del disambiguation[ambiguity_key]
        return evidence, contexts

    def _filter_identified_contexts(
        self, contexts: List[Context], mediainfo: MediaInfo, rule_groups: List[str],
        filter_params: Dict[str, str], candidate_filter: Optional[Callable[[List[Context]], List[Context]]],
        season_episodes: Optional[Dict[int, List[int]]] = None,
    ) -> List[Context]:
        """就绪目标才应用下载规则，复用已识别上下文而不再次识别整个候选池。"""
        if season_episodes:
            contexts = [context for context in contexts if context.meta_info is not None and TorrentHelper.match_season_episodes(
                torrent=context.torrent_info, meta=context.meta_info, season_episodes=season_episodes)]
        filtered = _filter_torrents(owner=self, torrents=[context.torrent_info for context in contexts],
                                    mediainfo=mediainfo, rule_groups=rule_groups, filter_params=filter_params,
                                    progress=ProgressHelper(ProgressKey.Search), diagnostics=Counter())
        accepted = {id(torrent) for torrent in filtered}
        result = [context for context in contexts if id(context.torrent_info) in accepted]
        if candidate_filter:
            result = candidate_filter(result)
        return TorrentHelper.sort_torrents(result)

    def _parse_result(
        self,
        torrents: List[TorrentInfo],
        mediainfo: MediaInfo | MusicInfo,
        keyword: Optional[str] = None,
        rule_groups: Optional[List[str]] = None,
        season_episodes: Optional[Dict[int, List[int]]] = None,
        custom_words: Optional[List[str]] = None,
        filter_params: Optional[Dict[str, str]] = None,
        include_candidates: bool = False,
        diagnostics: Optional[Counter[str]] = None,
        candidate_filter: Optional[Callable[[List[Context]], List[Context]]] = None,
    ) -> List[Context]:
        """过滤并匹配搜索结果，不修改调用方持有的媒体信息和资源容器。"""
        if not torrents:
            logger.warning(f"{keyword or mediainfo.title} 未搜索到资源")
            return []

        source_torrents = [copy.copy(torrent) for torrent in torrents]
        counts = diagnostics if diagnostics is not None else Counter()
        effective_rules = rule_groups
        if effective_rules is None:
            effective_rules = get_configured_system_config().get(SystemConfigKey.SearchFilterRuleGroups)
        if filter_params:
            logger.info(f"开始附加参数过滤，附加参数：{filter_params} ...")
        if effective_rules:
            logger.info(f"开始过滤规则/剧集过滤，使用规则组：{effective_rules} ...")

        progress = ProgressHelper(ProgressKey.Search)
        progress.start()
        try:
            progress.update(value=0, text=f"开始过滤，总 {len(source_torrents)} 个资源，请稍候...")
            filtered = _filter_torrents(
                owner=self,
                torrents=source_torrents,
                mediainfo=mediainfo,
                rule_groups=effective_rules or [],
                filter_params=filter_params or {},
                progress=progress,
                diagnostics=counts,
            )
            if effective_rules and not filtered:
                logger.warning(f"{keyword or mediainfo.title} 没有符合过滤规则的资源")
                return []
            if effective_rules:
                logger.info(f"过滤规则/剧集过滤完成，剩余 {len(filtered)} 个资源")
            progress.update(value=50, text=f"过滤完成，剩余 {len(filtered)} 个资源")

            matches = _match_torrents(
                torrents=filtered,
                mediainfo=mediainfo,
                season_episodes=season_episodes or {},
                custom_words=custom_words or [],
                progress=progress,
                include_candidates=include_candidates,
                diagnostics=counts,
            )
            contexts = _build_contexts(matches=matches, mediainfo=mediainfo)
            if candidate_filter is not None:
                before_filter = len(contexts)
                contexts = candidate_filter(contexts)
                counts["caller_filter"] += before_filter - len(contexts)
            progress.update(value=99, text=f"正在对 {len(contexts)} 个资源进行排序，请稍候...")
            contexts = TorrentHelper.sort_torrents(contexts)
            contexts.sort(key=lambda context: not context.media_info_is_target)
            contexts = self._remove_duplicate(contexts)
            logger.info(f"搜索完成，共 {len(contexts)} 个资源")
            progress.update(value=100, text=f"搜索完成，共 {len(contexts)} 个资源")
            return contexts
        finally:
            progress.end()

    @staticmethod
    def _remove_duplicate(_torrents: List[Context]) -> List[Context]:
        """按站点、标题和描述去重，并保留既有顺序语义。"""
        return list(
            {
                f"{item.torrent_info.site_name}_{item.torrent_info.title}_{item.torrent_info.description}": item
                for item in _torrents
            }.values()
        )
