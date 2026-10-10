"""保护批量下载各阶段之间的状态传递与副作用顺序。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.chain.download.batch as download_batch
import app.chain.download.selection as download_selection
from app.chain.download import DownloadChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType, NotificationChannel


def _context(
        name: str,
        media_id: str = "1",
        episodes: tuple[int, ...] = (),
        season: int = 1,
        media_type: MediaType = MediaType.TV,
) -> Context:
    meta = MetaInfo(f"{name} S{season:02d}")
    if episodes:
        meta.set_episodes(begin=min(episodes), end=max(episodes))
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            title=name, type=media_type, media_source=MediaSource.TMDB, media_id=media_id,
        ),
        torrent_info=TorrentInfo(title=name, site_name="test", site=1),
    )


def _missing(
        episodes: tuple[int, ...] = (),
        total: int = 4,
        season: int = 1,
        complete: bool = False,
) -> NotExistMediaInfo:
    return NotExistMediaInfo(
        season=season, episodes=list(episodes), total_episode=total,
        start_episode=1, require_complete_coverage=complete,
    )


@pytest.fixture
def batch(monkeypatch):
    """只替换事件、排序、持久化及下载边界，保留真实阶段选择和失败指纹。"""
    chain = object.__new__(DownloadChain)
    chain.eventmanager = SimpleNamespace(send_event=MagicMock(return_value=None))
    chain._active_download_failure_fingerprints = MagicMock(return_value={})
    chain._record_download_failure = MagicMock()
    chain._log_download_failure_cooldown = MagicMock()
    chain.download_single = MagicMock(return_value="download-id")
    chain.download_torrent = MagicMock(return_value=(b"torrent", None, [1, 2, 3, 4]))
    helper = SimpleNamespace(
        sort_torrents=MagicMock(side_effect=lambda contexts: contexts),
        get_torrent_episodes=MagicMock(side_effect=lambda files, **_kwargs: files),
    )
    stop = SimpleNamespace(is_system_stopped=False)
    monkeypatch.setattr(download_batch, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_selection, "TorrentHelper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", stop)
    monkeypatch.setattr(download_selection, "runtime_stop_state", stop)
    return SimpleNamespace(chain=chain, helper=helper, stop=stop)


def _submitted(chain) -> list[Context]:
    return [
        call.args[0] if call.args else call.kwargs["context"]
        for call in chain.download_single.call_args_list
    ]


def test_batch_preserves_phase_order_and_submission_options(batch):
    partial = _context("partial", media_id="3")
    labelled = _context("labelled", media_id="2", episodes=(2,))
    whole = _context("whole")
    movie = _context("movie", media_type=MediaType.MOVIE)
    music = _context("music", media_type=MediaType.MUSIC)
    missing = {
        "tmdb:3": {1: _missing((3,))},
        "tmdb:2": {1: _missing((2,))},
        "tmdb:1": {1: _missing()},
    }
    options = {
        "save_path": "smb:/server/media",
        "channel": NotificationChannel.Telegram,
        "source": "Subscribe|42",
        "userid": "user-id",
        "username": "plugin-name",
        "downloader": "test-downloader",
        "custom_words": "first\nsecond",
        "governance": object(),
    }
    result, remaining = batch.chain.batch_download(
        [partial, labelled, whole, movie, music], missing, **options,
    )

    assert result == [movie, music, whole, labelled, partial]
    assert _submitted(batch.chain) == result
    assert remaining is missing
    assert remaining == {}
    assert batch.chain.eventmanager.send_event.call_count == 1
    assert batch.helper.sort_torrents.call_count == 1
    assert batch.chain._active_download_failure_fingerprints.call_count == 1
    for call in batch.chain.download_single.call_args_list:
        assert {key: call.kwargs[key] for key in options} == options
    calls = batch.chain.download_single.call_args_list
    assert "torrent_content" not in calls[0].kwargs
    assert calls[2].kwargs["torrent_content"] == b"torrent"
    assert "torrent_content" not in calls[3].kwargs
    assert calls[4].kwargs["episodes"] == {3}
    assert all(
        call.kwargs["custom_words"] == ["first", "second"]
        for call in batch.helper.get_torrent_episodes.call_args_list
    )


def test_incomplete_season_becomes_labelled_candidate_after_file_inspection(batch):
    candidate = _context("incomplete")
    missing = {"tmdb:1": {1: _missing()}}
    batch.chain.download_torrent.return_value = (b"torrent", None, [1, 2])

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert remaining is missing
    assert remaining["tmdb:1"][1].episodes == [3, 4]
    assert candidate.meta_info.episode_list == [1, 2]
    batch.chain.download_torrent.assert_called_once()
    batch.chain.download_single.assert_called_once()
    assert "torrent_content" not in batch.chain.download_single.call_args.kwargs
    batch.chain._record_download_failure.assert_not_called()


def test_failed_season_submission_stays_in_cooldown_after_metadata_changes(batch):
    whole = _context("whole")
    fallback = _context("fallback", episodes=(1,))
    missing = {"tmdb:1": {1: _missing(total=3)}}
    batch.chain.download_torrent.return_value = (b"torrent", None, [1, 2, 3])
    batch.chain.download_single.side_effect = [None, "fallback-id"]

    result, remaining = batch.chain.batch_download([whole, fallback], missing)

    assert result == [fallback]
    assert _submitted(batch.chain) == [whole, fallback]
    assert remaining["tmdb:1"][1].episodes == [2, 3]
    assert whole.meta_info.episode_list == [1, 2, 3]
    batch.chain.download_torrent.assert_called_once()
    assert batch.chain._log_download_failure_cooldown.call_count == 2


def test_empty_torrent_is_recorded_once_and_not_retried_in_later_phases(batch):
    whole = _context("empty")
    fallback = _context("fallback", episodes=(1,))
    missing = {"tmdb:1": {1: _missing(total=3)}}
    batch.chain.download_torrent.return_value = (None, "failed", [])

    result, remaining = batch.chain.batch_download(
        [whole, fallback], missing, source="Subscribe|42", downloader="test",
    )

    assert result == [fallback]
    assert remaining["tmdb:1"][1].episodes == [2, 3]
    batch.chain.download_torrent.assert_called_once()
    batch.chain._record_download_failure.assert_called_once_with(
        context=whole, error_msg="下载种子内容为空", downloader="test", source="Subscribe|42",
    )


def test_uninspectable_magnet_is_not_cooled_down_as_a_download_failure(batch):
    whole = _context("magnet")
    fallback = _context("fallback", episodes=(1,))
    missing = {"tmdb:1": {1: _missing(total=3)}}
    batch.chain.download_torrent.return_value = ("magnet:?xt=test", None, [])

    result, remaining = batch.chain.batch_download([whole, fallback], missing)

    assert result == [fallback]
    assert remaining["tmdb:1"][1].episodes == [2, 3]
    assert batch.chain.download_torrent.call_count == 2
    batch.chain._record_download_failure.assert_not_called()
    batch.chain._log_download_failure_cooldown.assert_not_called()
    batch.helper.get_torrent_episodes.assert_not_called()


def test_partial_packs_only_settle_successful_allowed_episode_selections(batch):
    failed = _context("failed", episodes=(1, 4))
    first = _context("first")
    first.allowed_episodes = {1, 3}
    second = _context("second")
    missing = {"tmdb:1": {1: _missing((1, 2, 3))}}
    batch.chain.download_torrent.side_effect = [
        (b"failed", None, [1, 2, 4]),
        (b"first", None, [1, 3, 4]),
        (b"second", None, [2, 4]),
    ]
    needs_at_submission = []

    def submit(**kwargs):
        needs_at_submission.append(list(missing["tmdb:1"][1].episodes))
        return None if kwargs["context"] is failed else "id"

    batch.chain.download_single.side_effect = submit
    result, remaining = batch.chain.batch_download([failed, first, second], missing)

    assert result == [first, second]
    assert remaining is missing
    assert remaining == {}
    assert needs_at_submission == [[1, 2, 3], [1, 2, 3], [2]]
    assert [
        call.kwargs["episodes"] for call in batch.chain.download_single.call_args_list
    ] == [{1, 2}, {1, 3}, {2}]
    assert first.meta_info.episode_list == [1, 2, 3, 4]
    assert second.meta_info.episode_list == [2, 3, 4]
    assert failed.meta_info.episode_list == [1, 2, 3, 4]


@pytest.mark.parametrize("missing", [None, {}])
def test_batch_preserves_optional_missing_result_and_resets_per_call_state(batch, missing):
    candidate = _context("movie", media_type=MediaType.MOVIE)
    batch.chain.download_single.side_effect = [None, "retry-id"]

    first, first_remaining = batch.chain.batch_download([candidate], missing)
    second, second_remaining = batch.chain.batch_download([candidate], missing)

    assert first == []
    assert second == [candidate]
    assert first_remaining is missing
    assert second_remaining is missing
    assert batch.chain.download_single.call_count == 2


def test_reentrant_batch_call_keeps_outer_candidates_and_accounting(batch):
    """下载回调触发另一批次时，两次调用不能共用候选、结果或缺集状态。"""
    movie = _context("outer-movie", media_type=MediaType.MOVIE)
    nested = _context("nested-movie", media_type=MediaType.MOVIE)
    whole = _context("whole")
    missing = {"tmdb:1": {1: _missing()}}
    nested_results = []

    def submit(*args, **kwargs):
        context = args[0] if args else kwargs["context"]
        if context is movie:
            nested_results.append(batch.chain.batch_download([nested]))
        return "id"

    batch.chain.download_single.side_effect = submit
    result, remaining = batch.chain.batch_download([whole, movie], missing)

    assert result == [movie, whole]
    assert nested_results == [([nested], None)]
    assert remaining is missing
    assert remaining == {}
    assert _submitted(batch.chain) == [movie, nested, whole]


def test_stop_during_movie_submission_prevents_later_phase_side_effects(batch):
    movie = _context("movie", media_type=MediaType.MOVIE)
    whole = _context("whole")
    missing = {"tmdb:1": {1: _missing()}}

    def submit(*_args, **_kwargs):
        batch.stop.is_system_stopped = True
        return "movie-id"

    batch.chain.download_single.side_effect = submit
    result, remaining = batch.chain.batch_download([whole, movie], missing)

    assert result == [movie]
    assert remaining == {"tmdb:1": {1: _missing()}}
    batch.chain.download_single.assert_called_once()
    batch.chain.download_torrent.assert_not_called()


def test_multiseason_pack_submits_without_reading_torrent_files(batch):
    candidate = _context("multiseason", season=0)
    candidate.meta_info.end_season = 1
    missing = {"tmdb:1": {0: _missing(season=0), 1: _missing()}}

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == [candidate]
    assert remaining is missing
    assert remaining == {}
    batch.chain.download_torrent.assert_not_called()
    assert batch.chain.download_single.call_args.args == (candidate,)
    assert candidate.confirmed_full_coverage is False


def test_partial_failure_does_not_rewrite_candidate_episode_range(batch):
    candidate = _context("partial")
    missing = {"tmdb:1": {1: _missing((2,))}}
    batch.chain.download_single.return_value = None
    batch.chain.download_torrent.return_value = (b"torrent", None, [1, 2, 3])

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == []
    assert remaining["tmdb:1"][1].episodes == [2]
    assert candidate.meta_info.episode_list == []
    batch.chain.download_single.assert_called_once()


def test_failed_complete_coverage_submission_does_not_confirm_or_settle(batch):
    candidate = _context("complete")
    missing = {"tmdb:1": {1: _missing(complete=True)}}
    batch.chain.download_single.return_value = None

    result, remaining = batch.chain.batch_download([candidate], missing)

    assert result == []
    assert remaining["tmdb:1"][1].require_complete_coverage is True
    assert remaining["tmdb:1"][1].episodes == []
    assert candidate.confirmed_full_coverage is False
    batch.chain.download_single.assert_called_once()


def test_higher_priority_labelled_full_pack_wins_whole_season_phase(batch):
    """标注完整集数的高优先级包与无集数整季包同轮竞争，不被后者抢先清空缺季。"""
    labelled = _context("labelled", episodes=(1, 2, 3, 4))
    whole = _context("whole")
    missing = {"tmdb:1": {1: _missing(complete=True)}}

    result, remaining = batch.chain.batch_download([labelled, whole], missing)

    assert result == [labelled]
    assert remaining == {}
    batch.chain.download_torrent.assert_called_once()
    assert batch.chain.download_single.call_args.kwargs["torrent_content"] == b"torrent"
    assert labelled.confirmed_full_coverage is True


@pytest.mark.parametrize(
    ("episodes", "total"),
    [((1, 2), 4), ((1, 2, 3, 4), 0)],
    ids=["partial-range", "unknown-total"],
)
def test_labelled_pack_without_full_coverage_stays_out_of_whole_season_phase(batch, episodes, total):
    labelled = _context("labelled", episodes=episodes)
    whole = _context("whole")
    missing = {"tmdb:1": {1: _missing(total=total)}}

    result, _remaining = batch.chain.batch_download([labelled, whole], missing)

    assert result == [whole]
    batch.chain.download_torrent.assert_called_once()
    assert batch.chain.download_single.call_args.kwargs["context"] is whole


def test_labelled_full_pack_respects_candidate_allowed_episodes(batch):
    labelled = _context("labelled", episodes=(1, 2, 3, 4))
    labelled.allowed_episodes = {1, 2}
    missing = {"tmdb:1": {1: _missing()}}

    result, remaining = batch.chain.batch_download([labelled], missing)

    assert result == []
    assert remaining["tmdb:1"][1].episodes == []
    batch.chain.download_torrent.assert_not_called()
    batch.chain.download_single.assert_not_called()


def test_labelled_full_pack_magnet_falls_back_to_episode_pack_phase(batch):
    labelled = _context("labelled", episodes=(1, 2, 3, 4))
    missing = {"tmdb:1": {1: _missing(complete=True)}}
    batch.chain.download_torrent.return_value = ("magnet:?xt=test", None, [])

    result, remaining = batch.chain.batch_download([labelled], missing)

    assert result == [labelled]
    assert remaining == {}
    batch.chain.download_torrent.assert_called_once()
    assert "torrent_content" not in batch.chain.download_single.call_args.kwargs
    batch.chain._record_download_failure.assert_not_called()
