"""订阅候选批次与无损索引合同测试。"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.application.subscription.candidates import CandidateBatch, CandidateIndex
from app.application.subscription.contract import SubscriptionSnapshot
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


def test_refresh_batch_distinguishes_complete_cache_from_fresh_delta():
    """刷新批次必须保留完整缓存，同时只把本轮新增资源放入 fresh 集合。"""
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
        batch = chain.refresh_batch(stype="rss", sites=[1])

    assert isinstance(batch, CandidateBatch)
    assert batch.source == "rss"
    assert batch.finished_at is not None
    assert [item.torrent_info.title for item in batch.candidates["example.com"]] == [
        existing.torrent_info.title,
        fresh.title,
    ]
    assert [item.torrent_info.title for item in batch.fresh_candidates["example.com"]] == [fresh.title]
    assert CandidateBatch.count(batch.candidates) == 2
    assert CandidateBatch.count(batch.fresh_candidates) == 1


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
