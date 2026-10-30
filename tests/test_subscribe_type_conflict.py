"""
验证订阅候选的媒体身份与类型冲突不会进入下载或污染缓存。
"""

from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Optional
from unittest.mock import Mock

import pytest

from app.application.subscription.candidates import CandidateIndex
from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.execution import (
    SubscriptionExecutionAdmission,
    SubscriptionExecutionContext,
)
from app.application.subscription.facts import FreshFactLease
from app.application.torrent.download import TorrentHelper
from app.chain.subscribe import match as subscribe_match
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def conflict_case(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """
    构造《空枪》资源被识别为《V星入侵》的离线订阅匹配场景。

    保留真实候选索引、标题解析和匹配逻辑，仅替换媒体查询、持久化和下载边界。
    """
    target = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="1567937",
        tmdb_id=1567937,
        type=MediaType.MOVIE,
        title="空枪",
        original_title="空枪",
        year="2026",
    )
    candidate = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="21494",
        tmdb_id=21494,
        type=MediaType.TV,
        title="V星入侵",
        original_title="V",
        year="2009",
    )
    torrent = TorrentInfo(
        title="V 2026 1080p WEB-DL DDP5.1 H265 2Audio-HDSWEB",
        description="空枪 | 主演: 朱一龙 檀健次 梁家辉 【国粤双语】",
        category=MediaType.MOVIE.value,
        site=1,
        site_name="测试站点",
    )
    meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
    context = Context(
        meta_info=meta,
        media_info=candidate,
        torrent_info=torrent,
        resource_source="rss",
        match_source=MediaSource.TMDB.value,
        candidate_recognized=True,
        media_info_is_target=False,
    )
    subscribe = SubscriptionSnapshot(
        id=1,
        name=target.title,
        year=target.year,
        type=target.type.value,
        media_source=target.media_source,
        media_id=target.media_id,
        state="R",
    )
    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(get=lambda _subscription_id: None)
    chain.get_sub_sites = Mock(return_value=[])
    chain.get_params = Mock(return_value={})
    chain.check_and_handle_existing_media = Mock(return_value=(False, {}))
    chain.filter_torrents = Mock(side_effect=lambda **kwargs: kwargs["torrent_list"])
    chain.finish_subscribe_or_not = Mock()
    download = Mock(return_value=([], {}))
    chain._SubscribeChain__download_best_version_with_full_pack_first = download
    media_chain = SimpleNamespace(
        recognize_media=Mock(return_value=target),
        supplement_media_info=lambda media: media,
    )
    monkeypatch.setattr(subscribe_match, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(
        subscribe_match,
        "get_configured_system_config",
        lambda: SimpleNamespace(get=lambda _key: []),
    )
    monkeypatch.setattr(
        subscribe_match, "runtime_stop_state", SimpleNamespace(is_system_stopped=False)
    )
    admission = SubscriptionExecutionAdmission()
    lease = admission.try_acquire(subscription_id=subscribe.id, operation="match", ttl_seconds=60)
    assert lease is not None
    yield SimpleNamespace(
        target=target,
        candidate=candidate,
        torrent=torrent,
        meta=meta,
        context=context,
        subscribe=subscribe,
        chain=chain,
        download=download,
        execution=SubscriptionExecutionContext(lease=lease, admission=admission),
    )
    assert admission.release(lease)


def _execute_match(case: SimpleNamespace) -> str:
    """
    经真实候选路由和单订阅匹配流程执行一次资源检查。
    """
    candidates = {"example.com": [case.context]}
    return case.chain._execute_match(
        subscribe=case.subscribe,
        processed_torrents=candidates,
        candidate_index=CandidateIndex(candidates),
        fresh_fact_lease=FreshFactLease(),
        execution_context=case.execution,
    )


def _reconcile(case: SimpleNamespace) -> Optional[MediaInfo]:
    """
    直接验证冲突复核边界，避免索引提前拒绝掩盖复核回归。
    """
    return SubscribeChain._SubscribeChain__reconcile_candidate_media(
        target_mediainfo=case.target,
        candidate_mediainfo=case.candidate,
        torrent_meta=case.meta,
        torrent_info=case.torrent,
        context=case.context,
    )


@pytest.mark.parametrize(
    ("torrent_year", "candidate_year"),
    [("2026", "2024"), ("2026", "2026"), (None, "2026"), (None, None), (None, "2024")],
)
def test_live_action_subscription_rejects_same_title_animation(
    conflict_case: SimpleNamespace,
    torrent_year: Optional[str],
    candidate_year: Optional[str],
) -> None:
    """
    《剑来》同名真人与动漫使用不同身份时，标题和年份均不能使动漫进入下载。

    ID 和年份为离线样例，覆盖资源年份相同、共同首播年份及缺失年份的放行路径。
    """
    case = conflict_case
    case.target.type = MediaType.TV
    case.target.title = case.target.original_title = "剑来"
    case.target.season_years = {1: "2026"}
    case.target.genre_ids = [18]
    case.candidate.type = MediaType.TV
    case.candidate.title = case.candidate.original_title = "剑来"
    case.candidate.year = candidate_year
    case.candidate.genre_ids = [16]
    case.torrent.title = f"剑来 {torrent_year or ''} S01E01 1080p WEB-DL"
    case.torrent.description = "剑来 | 动画版 | 国语中字"
    case.torrent.category = MediaType.TV.value
    case.meta = MetaInfo(title=case.torrent.title, subtitle=case.torrent.description)
    case.context.meta_info = case.meta
    case.subscribe = replace(case.subscribe, name="剑来", type=MediaType.TV.value, season=1)

    assert _execute_match(case) == "completed"

    case.download.assert_not_called()
    assert case.context.media_info is case.candidate
    assert case.context.media_info.media_id != case.target.media_id
    assert case.context.candidate_recognized is True
    assert case.context.media_info_is_target is False


@pytest.mark.parametrize("recognized", [True, False])
def test_matching_identity_and_unrecognized_title_still_reach_download(
    conflict_case: SimpleNamespace, recognized: bool
) -> None:
    """
    身份一致的正常资源及尚未识别出身份的标题兜底继续下载，并保留各自匹配依据。
    """
    case = conflict_case
    candidate = replace(case.target) if recognized else None
    case.context.media_info = candidate
    case.context.match_source = MediaSource.TMDB.value if recognized else "unknown"
    case.context.candidate_recognized = recognized

    assert _execute_match(case) == "completed"

    case.download.assert_called_once()
    matched = case.download.call_args.kwargs["contexts"][0]
    assert matched.media_info.media_source == case.target.media_source
    assert matched.media_info.media_id == case.target.media_id
    assert matched.media_info.type == case.target.type
    if recognized:
        assert matched.media_info is candidate
    assert matched.match_source == (MediaSource.TMDB.value if recognized else "title")
    assert matched.candidate_recognized is recognized
    assert matched.media_info_is_target is (not recognized)
    assert case.context.media_info is candidate


def test_inferred_type_conflict_reaches_subscription_review(conflict_case: SimpleNamespace) -> None:
    """
    只由识别结果产生的类型冲突不能在候选路由阶段丢失。
    """
    case = conflict_case
    index = CandidateIndex({"example.com": [case.context]})

    assert index.route_for_match(case.subscribe) == {"example.com": [case.context]}
    assert index.select_cache_candidates(case.subscribe, allow_title_match=True) == []


@pytest.mark.parametrize("candidate_id", ["21494", "1567937"])
def test_movie_subtitle_and_year_cannot_override_inferred_tv(
    conflict_case: SimpleNamespace, candidate_id: str
) -> None:
    """
    副标题和精确年份不能覆盖类型冲突，相同数字 ID 也不允许进入下载。
    """
    case = conflict_case
    case.candidate.media_id = candidate_id
    case.candidate.tmdb_id = int(candidate_id)

    assert _execute_match(case) == "completed"

    case.download.assert_not_called()
    assert case.context.media_info is case.candidate
    assert case.candidate.type == MediaType.TV
    assert case.candidate.media_id == candidate_id
    assert case.context.match_source == MediaSource.TMDB.value
    assert case.context.candidate_recognized is True
    assert case.context.media_info_is_target is False


@pytest.mark.parametrize("candidate_id", ["21494", "1567937"])
def test_type_conflict_with_same_numeric_id_is_rejected(
    conflict_case: SimpleNamespace, candidate_id: str
) -> None:
    """
    TMDB 的电影与电视剧数字 ID 相同也不能绕过类型校验。
    """
    case = conflict_case
    case.candidate.media_id = candidate_id
    case.candidate.tmdb_id = int(candidate_id)
    case.torrent.description = "其他电影"

    assert _reconcile(case) is None
    assert case.context.media_info is case.candidate


@pytest.mark.parametrize("constraint", ["year", "missing_year", "title_type", "site_type", "explicit_id"])
@pytest.mark.parametrize("candidate_id", ["21494", "1567937"])
def test_cross_type_review_preserves_resource_constraints(
    conflict_case: SimpleNamespace, constraint: str, candidate_id: str
) -> None:
    """
    跨类型复核仍拒绝年份、季集类型、站点分类或明确媒体 ID 的冲突。
    """
    case = conflict_case
    case.candidate.media_id = candidate_id
    case.candidate.tmdb_id = int(candidate_id)
    if constraint == "year":
        case.meta.year = "2025"
    elif constraint == "missing_year":
        case.meta.year = None
    elif constraint == "title_type":
        case.meta.type = MediaType.TV
        case.meta.begin_season = 1
    elif constraint == "site_type":
        case.torrent.category = MediaType.TV.value
    else:
        case.meta.media_source = MediaSource.TMDB
        case.meta.media_id = case.candidate.media_id

    assert _reconcile(case) is None
    assert case.context.media_info is case.candidate
    assert _execute_match(case) == "completed"
    case.download.assert_not_called()


@pytest.mark.parametrize("candidate_id", ["21494", "1567937"])
def test_no_year_tv_title_cannot_override_other_type_without_work_evidence(
    conflict_case: SimpleNamespace, candidate_id: str
) -> None:
    """
    无年份剧集标题命中也不能覆盖候选自身的媒体类型。
    """
    case = conflict_case
    case.target.type = MediaType.TV
    case.target.season_years = {1: "2026"}
    case.candidate.type = MediaType.MOVIE
    case.candidate.media_id = candidate_id
    case.candidate.tmdb_id = case.target.tmdb_id
    case.torrent.title = "空枪 S01E01 1080p WEB-DL"
    case.torrent.category = MediaType.TV.value
    case.meta = MetaInfo(title=case.torrent.title)
    case.context.meta_info = case.meta
    case.subscribe = replace(case.subscribe, type=MediaType.TV.value, season=1)

    assert _reconcile(case) is None
    assert _execute_match(case) == "completed"
    case.download.assert_not_called()


def test_cross_type_rejection_logs_conflicting_types(
    conflict_case: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    冲突诊断须同时记录候选和目标的媒体类型。
    """
    case = conflict_case
    case.torrent.description = "其他电影"
    messages = []
    monkeypatch.setattr(subscribe_match.logger, "debug", messages.append)

    assert _reconcile(case) is None
    assert any("电视剧" in message and "电影" in message and "已跳过" in message for message in messages)


def test_tv_title_and_year_cannot_override_inferred_movie(conflict_case: SimpleNamespace) -> None:
    """
    电视剧标题及目标季年份命中，仍不能覆盖候选自身的电影类型。
    """
    case = conflict_case
    case.target.type = MediaType.TV
    case.target.season_years = {1: "2026"}
    case.candidate.type = MediaType.MOVIE
    case.torrent.title = "空枪 2026 S01E01 1080p WEB-DL"
    case.torrent.category = MediaType.TV.value
    case.meta = MetaInfo(title=case.torrent.title)
    case.context.meta_info = case.meta
    case.subscribe = replace(case.subscribe, type=MediaType.TV.value, season=1)

    assert _execute_match(case) == "completed"

    case.download.assert_not_called()
    assert case.context.media_info is case.candidate
    assert case.context.media_info.type == MediaType.MOVIE


def test_global_identity_remains_valid_cross_type_evidence(conflict_case: SimpleNamespace) -> None:
    """
    IMDb 这类全局媒体身份仍可证明同作品，不受 TMDB 的类型命名空间限制影响。
    """
    case = conflict_case
    case.meta.year = None
    for media in (case.target, case.candidate):
        media.media_source = MediaSource.IMDb
        media.media_id = "tt1234567"

    matched, evidence = TorrentHelper.match_same_work_evidence(
        target_mediainfo=case.target,
        candidate_mediainfo=case.candidate,
        torrent_meta=case.meta,
    )

    assert matched is True
    assert "tt1234567" in evidence
