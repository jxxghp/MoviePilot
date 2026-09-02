"""订阅候选批次与无损索引合同测试。"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.application.subscription.candidates import CandidateIndex
from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.facts import FreshFactLease
from app.chain.torrents import TorrentsChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType


def _context(
    title: str,
    media_id: str = None,
    *,
    meta_media_id: str = None,
    media_type: MediaType = MediaType.TV,
    season: int = 1,
    enclosure: str = None,
) -> Context:
    """构造可配置身份、类型和季的候选上下文。"""
    meta = MetaInfo(title=title)
    meta.type = media_type
    meta.begin_season = season
    if meta_media_id:
        meta.media_source = MediaSource.TMDB
        meta.media_id = meta_media_id
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            media_source=MediaSource.TMDB if media_id else None,
            media_id=media_id,
            type=media_type,
            title=title,
            season=season,
        ),
        torrent_info=TorrentInfo(
            title=title,
            description="",
            enclosure=enclosure or f"https://example.com/{title}",
            site=1,
            site_name="Test",
            category=media_type.value,
        ),
        resource_source="rss",
        match_source=MediaSource.TMDB.value if media_id else "unknown",
        candidate_recognized=bool(media_id),
    )


def _subscribe(**overrides) -> SubscriptionSnapshot:
    """构造候选路由使用的电视剧订阅。"""
    values = {
        "id": 1,
        "name": "目标剧集",
        "type": MediaType.TV.value,
        "media_source": MediaSource.TMDB,
        "media_id": "100",
        "season": 1,
        "state": "R",
        "sites": [],
        "best_version": 0,
    }
    values.update(overrides)
    return SubscriptionSnapshot(**values)


def _candidate_count(groups: dict[str, list[Context]]) -> int:
    """统计分站点候选总数。"""
    return sum(len(contexts) for contexts in groups.values())


def test_refresh_returns_complete_cache_with_new_candidates():
    """刷新结果必须包含既有缓存和本轮新增资源的完整候选。"""
    chain = TorrentsChain()
    existing = _context("既有剧集 S01E01", media_id="100")
    duplicate = TorrentInfo(
        title=existing.torrent_info.title,
        description=existing.torrent_info.description,
        enclosure="https://example.com/duplicate",
        site=1,
        site_name="Test",
        category=MediaType.TV.value,
    )
    fresh = TorrentInfo(
        title="新增剧集 S01E02",
        description="",
        enclosure="https://example.com/fresh",
        site=1,
        site_name="Test",
        category=MediaType.TV.value,
    )
    sites_helper = Mock()
    sites_helper.get_indexers.return_value = [
        {"id": 1, "name": "Test", "domain": "https://example.com"}
    ]

    def _load_cache(filename):
        """仅影视 RSS 缓存预置一条历史候选。"""
        if filename == TorrentsChain._rss_file:
            return {"example.com": [existing]}
        return {}

    with (
        patch.object(chain, "load_cache", side_effect=_load_cache),
        patch.object(chain, "rss", return_value=[duplicate, fresh]),
        patch.object(chain, "save_cache"),
        patch("app.chain.torrents.SitesHelper", return_value=sites_helper),
        patch(
            "app.chain.torrents.MediaChain",
            return_value=SimpleNamespace(
                recognize_by_meta=lambda *_args, **_kwargs: MediaInfo(
                    media_source=MediaSource.TMDB,
                    media_id="100",
                    type=MediaType.TV,
                )
            ),
        ),
    ):
        candidates = chain.refresh(stype="rss", sites=[1])

    assert [item.torrent_info.title for item in candidates["example.com"]] == [
        existing.torrent_info.title,
        fresh.title,
    ]
    assert _candidate_count(candidates) == 2


def test_candidate_index_routes_all_canonical_fallback_classes_without_loss():
    """索引必须保留未知身份、识别失败和可复核冲突，只排除确定冲突。"""
    exact = _context("目标剧集 S01E01", media_id="100")
    inferred_conflict = _context("目标剧集 S01E02", media_id="200")
    failed_with_meta_id = _context(
        "目标剧集 S01E03",
        media_id=None,
        meta_media_id="300",
    )
    explicit_conflict = _context(
        "其他剧集 S01E01",
        media_id="400",
        meta_media_id="400",
    )
    season_conflict = _context("目标剧集 S02E01", media_id="100", season=2)
    type_conflict = _context(
        "目标电影 2026",
        media_id="100",
        media_type=MediaType.MOVIE,
        season=None,
    )
    candidates = {
        "example.com": [
            exact,
            inferred_conflict,
            failed_with_meta_id,
            explicit_conflict,
            season_conflict,
            type_conflict,
        ]
    }

    routed = CandidateIndex(candidates).route_for_match(_subscribe())

    assert routed["example.com"] == [exact, inferred_conflict, failed_with_meta_id]


def test_candidate_index_custom_words_preserve_complete_candidate_set():
    """自定义识别词可能改变身份、类型和季，索引不得提前排除任何候选。"""
    candidates = {
        "example.com": [
            _context("其他剧集 S02E01", media_id="400", meta_media_id="400", season=2),
            _context("其他电影 2026", media_id="500", media_type=MediaType.MOVIE, season=None),
        ]
    }

    routed = CandidateIndex(candidates).route_for_match(
        _subscribe(custom_words="被替换词 => 目标剧集")
    )

    assert routed == candidates


def test_candidate_index_target_scale_avoids_subscription_candidate_product():
    """200 条订阅与 1000 候选只检查命中身份位置，并按唯一媒体合并事实读取。"""
    media_count = 100
    subscription_count = 200
    site_count = 20
    candidates = {f"site-{site_index}.example": [] for site_index in range(site_count)}
    for candidate_index in range(1000):
        media_id = str(1000 + candidate_index % media_count)
        domain = f"site-{candidate_index % site_count}.example"
        candidates[domain].append(
            _context(
                f"目标剧集 {media_id} S01E{candidate_index + 1:04d}",
                media_id=media_id,
                meta_media_id=media_id,
                enclosure=f"https://{domain}/{candidate_index}",
            )
        )
    subscribes = [
        _subscribe(
            id=index + 1,
            name=f"目标剧集 {index % media_count}",
            media_id=str(1000 + index % media_count),
        )
        for index in range(subscription_count)
    ]

    candidate_index = CandidateIndex(candidates)
    examined = 0
    routed = 0
    for subscribe in subscribes:
        groups = candidate_index.route_for_match(subscribe)
        examined += candidate_index.last_examined_count
        routed += _candidate_count(groups)

    lease = FreshFactLease()
    fact_loads = []
    for subscribe in subscribes:
        lease.get_or_load(
            subscribe,
            lambda subscribe=subscribe: fact_loads.append(subscribe.media_id)
            or MediaInfo(
                media_source=MediaSource.TMDB,
                media_id=subscribe.media_id,
                type=MediaType.TV,
                season=1,
            ),
        )

    assert len(candidates) == site_count
    assert _candidate_count(candidates) == 1000
    assert examined == routed == 2000
    assert examined < subscription_count * _candidate_count(candidates) // 50
    assert len(fact_loads) == media_count
    assert lease.loads == media_count
    assert lease.hits == subscription_count - media_count
