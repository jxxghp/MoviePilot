"""
手动下载后，订阅检查应把部分入库事实同步到卡片进度。
"""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.chain.subscribe.refresh as subscribe_refresh
from app.application.subscription.contract import SubscriptionSnapshot, build_subscribe_meta
from app.application.subscription.mutation import SyncSubscriptionMutationService
from app.chain.download import DownloadChain
from app.chain.subscribe.facade import SubscribeChain
from app.db.adapters.outbox import SqlAlchemyOutboxDispatchStore, SqlAlchemyOutboxStager
from app.db.adapters.subscription import SessionSubscriptionRepository
from app.db.models.outbox import OutboxMessage
from app.db.models.subscribe import Subscribe
from app.db.session import SessionFactory
from app.db.uow import SqlAlchemyUnitOfWork
from app.domain.context import MediaInfo
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def progress_scope(monkeypatch):
    """
    保留缺集计算和进度派生，仅隔离媒体库与持久化边界。
    """
    subscribe = SubscriptionSnapshot(
        id=6966, name="Doctor X", type=MediaType.TV.value,
        media_source=MediaSource.TMDB, media_id="6966", season=1,
        total_episode=12, lack_episode=11, note=[1], state="R",
        last_update="2026-10-01 12:00:00",
    )
    media = MediaInfo(
        title="Doctor X", type=MediaType.TV,
        media_source=MediaSource.TMDB, media_id="6966",
        seasons={1: list(range(1, 13))},
    )
    download = object.__new__(DownloadChain)
    download._media_exists_with_server_cache = Mock(
        return_value=ExistMediaInfo(seasons={1: [1, 2]})
    )
    monkeypatch.setattr(subscribe_refresh, "DownloadChain", lambda: download)
    chain = object.__new__(SubscribeChain)
    chain.finish_subscribe_or_not = Mock()
    chain._SubscribeChain__apply_episodes_refresh = Mock(return_value=12)
    state = SimpleNamespace(subscribe=subscribe, media=media, chain=chain, download=download)

    def apply_update(current, fields, **_kwargs):
        """
        保存派生快照，供重复检查读取同一持久化状态。
        """
        state.subscribe = replace(current, **fields)
        return state.subscribe

    chain._SubscribeChain__apply_subscribe_update = Mock(side_effect=apply_update)
    return state


def _check(scope):
    """
    使用当前订阅快照执行真实的现存媒体检查。
    """
    return scope.chain.check_and_handle_existing_media(
        subscribe=scope.subscribe, meta=build_subscribe_meta(scope.subscribe),
        mediainfo=scope.media, mediakey="tmdb:6966",
    )


def test_partial_library_updates_stale_card_progress(progress_scope):
    """
    前两集已入库且 note 只含第一集时，卡片应从 1/12 校正为 2/12。
    """
    for _ in range(2):
        completed, missing = _check(progress_scope)
        assert not completed
        assert missing["tmdb:6966"][1].episodes == list(range(3, 13))
        assert progress_scope.subscribe.lack_episode == 10
        assert progress_scope.subscribe.note == [1]
        assert progress_scope.subscribe.last_update == "2026-10-01 12:00:00"
    progress_scope.chain.finish_subscribe_or_not.assert_not_called()


def test_partial_library_respects_start_and_downloaded_facts(progress_scope):
    """
    进度复用订阅范围和已下载事实，不重复计数或包含其他季。
    """
    progress_scope.subscribe = replace(progress_scope.subscribe, start_episode=3, note=[3, 5])
    progress_scope.download._media_exists_with_server_cache.return_value = ExistMediaInfo(
        seasons={1: [1, 2, 3, 4, 4], 2: list(range(1, 13))}
    )

    completed, missing = _check(progress_scope)

    assert not completed
    assert missing["tmdb:6966"][1].episodes == list(range(6, 13))
    assert progress_scope.subscribe.lack_episode == 7
    progress_scope.chain.finish_subscribe_or_not.assert_not_called()


def test_failed_missing_query_preserves_progress(progress_scope):
    """
    无法取得缺集事实时，不能把未知状态当成全部完成。
    """
    progress_scope.chain.resolve_subscribe_missing = Mock(return_value=(False, {}))

    completed, missing = _check(progress_scope)

    assert not completed
    assert missing == {}
    assert progress_scope.subscribe.lack_episode == 11
    progress_scope.chain._SubscribeChain__apply_subscribe_update.assert_not_called()
    progress_scope.chain.finish_subscribe_or_not.assert_not_called()


def test_best_version_check_preserves_quality_progress(progress_scope):
    """
    洗版订阅继续按下载质量事实处理，不用部分入库覆盖其进度。
    """
    progress_scope.subscribe = replace(
        progress_scope.subscribe, best_version=1, episode_priority={"1": 80},
    )

    completed, missing = _check(progress_scope)

    assert not completed
    assert missing
    assert progress_scope.subscribe.lack_episode == 11
    assert progress_scope.subscribe.episode_priority == {"1": 80}
    progress_scope.chain._SubscribeChain__apply_subscribe_update.assert_not_called()
    progress_scope.chain.finish_subscribe_or_not.assert_not_called()


def test_complete_library_keeps_completion_path(progress_scope):
    """
    整季入库仍交给原有完成流程，避免在检查入口重复结算。
    """
    progress_scope.download._media_exists_with_server_cache.return_value = ExistMediaInfo(
        seasons={1: list(range(1, 13))}
    )

    completed, missing = _check(progress_scope)

    assert completed
    assert missing == {}
    progress_scope.chain.finish_subscribe_or_not.assert_called_once()
    assert progress_scope.chain.finish_subscribe_or_not.call_args.kwargs["force"]
    progress_scope.chain._SubscribeChain__apply_subscribe_update.assert_not_called()


def test_partial_progress_persists_without_overwriting_extra_fields(progress_scope, db):
    """
    真实订阅写入只更新缺失集数，保留数据库中较新的附加字段。
    """
    db.watermark(Subscribe, OutboxMessage)
    row = db.add(Subscribe(
        name="Doctor X", type=MediaType.TV.value,
        media_source=MediaSource.TMDB.value, media_id="6966", season=1,
        total_episode=12, lack_episode=11, note=[1, 99],
        episode_priority={"1": 80, "99": 90}, current_priority=80,
        sites=[1, 2], filter_groups=["WEB-DL"], state="R", best_version=0,
        last_update="2026-10-01 12:00:00", manual_total_episode=1,
    ))
    progress_scope.subscribe = replace(progress_scope.subscribe, id=row.id, manual_total_episode=1)
    published = []

    @contextmanager
    def mutation_scope():
        """
        用真实短事务和 outbox 保存进度并捕获修改事件。
        """
        with SessionFactory() as session:
            yield SyncSubscriptionMutationService(
                repository=SessionSubscriptionRepository(session),
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=SqlAlchemyOutboxStager(session),
                dispatch_store=SqlAlchemyOutboxDispatchStore(SessionFactory),
                publish_modified=published.append,
            )

    progress_scope.chain.sync_subscription_mutation_scope = mutation_scope
    progress_scope.chain._SubscribeChain__apply_subscribe_update = (
        SubscribeChain._SubscribeChain__apply_subscribe_update.__get__(progress_scope.chain)
    )

    completed, _missing = _check(progress_scope)

    assert not completed
    db.session.expire_all()
    stored = db.session.get(Subscribe, row.id)
    assert stored.lack_episode == 10
    assert stored.note == [1, 99]
    assert stored.episode_priority == {"1": 80, "99": 90}
    assert stored.current_priority == 80
    assert stored.sites == [1, 2]
    assert stored.filter_groups == ["WEB-DL"]
    assert stored.last_update == "2026-10-01 12:00:00"
    assert len(published) == 1
    assert published[0]["subscribe_info"]["lack_episode"] == 10
    assert published[0]["subscribe_info"]["note"] == [1, 99]
