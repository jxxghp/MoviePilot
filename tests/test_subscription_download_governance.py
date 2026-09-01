"""订阅下载跨入口幂等、不确定终态与取消补偿测试。"""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.chain.download.submission as download_submission
from app.application.download.admission import (
    DownloadReconciliationRequired,
    SubscriptionDownloadGovernance,
    SubscriptionDownloadRequest,
)
from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.download import DownloadChain
from app.chain.subscribe import policy as subscribe_policy
from app.chain.subscribe.facade import SubscribeChain
from app.db.adapters.subscriptiondownload import TransactionalSubscriptionDownloadRepository
from app.db.base import Base
from app.db.models.subscriptiondownload import SubscriptionDownloadSubmission
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType


def _request(key: str = "key-1", task_id: str | None = "task-1") -> SubscriptionDownloadRequest:
    """构造固定身份的提交认领请求。"""
    return SubscriptionDownloadRequest(
        idempotency_key=key,
        subscription_id=7,
        task_id=task_id,
        logical_identity='{"subscription_id":7}',
        resource_key="example.com:id=42",
        coverage="episodes:E01-E03",
        mode="normal",
    )


def _repository(tmp_path) -> tuple[TransactionalSubscriptionDownloadRepository, object]:
    """创建独立 SQLite 幂等账本仓储与 Session 工厂。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'subscription-download.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return TransactionalSubscriptionDownloadRepository(factory), factory


def test_repository_claims_same_submission_once_across_workers(tmp_path) -> None:
    """并发入口对同一幂等键只能有一个取得下载器提交权。"""
    repository, _factory = _repository(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _index: repository.claim(_request()), range(2)))

    assert sum(claim.acquired for claim in claims) == 1
    assert {claim.snapshot.state for claim in claims} == {"submitting"}
    assert {claim.snapshot.attempt_count for claim in claims} == {1}


def test_repository_fences_retry_and_preserves_uncertain_terminal(tmp_path) -> None:
    """明确拒绝可延迟重试，过期令牌和待对账状态均不得重新提交。"""
    repository, _factory = _repository(tmp_path)
    first = repository.claim(_request())
    token = first.snapshot.attempt_token
    assert token
    assert repository.mark_retryable(
        idempotency_key="key-1",
        attempt_token=token,
        available_at="1970-01-01T00:00:00+00:00",
        error="downloader rejected",
    )

    second = repository.claim(_request(task_id="task-2"))
    second_token = second.snapshot.attempt_token
    assert second.acquired
    assert second_token and second_token != token
    assert not repository.mark_succeeded(
        idempotency_key="key-1",
        attempt_token=token,
    )
    assert repository.mark_reconcile_required(
        idempotency_key="key-1",
        attempt_token=second_token,
        error="transport timeout",
    )

    blocked = repository.claim(_request(task_id="task-3"))
    assert not blocked.acquired
    assert blocked.snapshot.state == "reconcile_required"
    assert blocked.snapshot.attempt_count == 2
    assert repository.has_started_for_task("task-2")


class _FakeTorrentHelper:
    """返回固定种子目录和文件清单，隔离真实 bencode 解析。"""

    @staticmethod
    def get_fileinfo_from_torrent_content(_content):
        """返回单文件种子结构。"""
        return "Demo.Show.S01", ["Demo.Show.S01E01.mkv"]


def _download_chain(repository) -> DownloadChain:
    """构造只执行下载提交边界的 Chain 测试实例。"""
    chain = DownloadChain.__new__(DownloadChain)
    chain.subscription_download_repository = repository
    chain.download_history_repository = MagicMock()
    chain.download_history_repository.get_by_media_identity.return_value = []
    chain.download_failure_repository = MagicMock()
    chain.eventmanager = MagicMock()
    chain.eventmanager.send_event.return_value = None
    chain.post_message = MagicMock()
    chain._settle_download_success = MagicMock()
    chain._resolve_media_download_dir = MagicMock(
        return_value=("local", Path("/downloads"), None)
    )
    chain.runtime_config = SimpleNamespace(media_extensions=(".mkv",))
    return chain


def _context() -> Context:
    """构造具有稳定媒体和 torrent 身份的电视剧候选。"""
    return Context(
        meta_info=MetaInfo("Demo Show S01E01"),
        media_info=MediaInfo(
            media_source=MediaSource.TMDB,
            media_id="77",
            type=MediaType.TV,
            title="Demo Show",
            year="2026",
            tmdb_id=77,
            genre_ids=[18],
        ),
        torrent_info=TorrentInfo(
            site=12,
            site_name="TestSite",
            title="Demo Show S01E01 1080p",
            enclosure="https://example.com/download.php?id=42",
        ),
    )


@pytest.fixture(autouse=True)
def _submission_dependencies(monkeypatch):
    """固定目录、媒体补全和种子解析，避免触发外部模块。"""
    monkeypatch.setattr(
        "app.application.directory.DirectoryHelper.get_download_dirs",
        lambda _self: [SimpleNamespace(
            storage="local",
            download_path="/downloads",
            category=None,
            media_type=None,
        )],
    )
    monkeypatch.setattr(download_submission, "TorrentHelper", _FakeTorrentHelper)
    monkeypatch.setattr(
        download_submission.MediaChain,
        "supplement_tmdb_info",
        lambda _self, media, _meta: media,
    )


def test_download_chain_reuses_success_without_second_downloader_call(tmp_path) -> None:
    """重叠入口在首个提交成功后复用 hash，不再次调用下载器。"""
    repository, _factory = _repository(tmp_path)
    chain = _download_chain(repository)
    chain.download = MagicMock(return_value=("qb", "hash-1", "Original", "accepted"))
    governance = SubscriptionDownloadGovernance(subscription_id=7, mode="normal")

    first = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=governance,
    )
    second = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=governance,
    )

    assert first == second == "hash-1"
    chain.download.assert_called_once()
    chain._settle_download_success.assert_called_once()


def test_download_chain_freezes_when_local_settlement_fails(tmp_path) -> None:
    """下载器接受而历史结算失败时进入待对账，后续入口不得盲重试。"""
    repository, _factory = _repository(tmp_path)
    chain = _download_chain(repository)
    chain.download = MagicMock(return_value=("qb", "hash-2", "Original", "accepted"))
    chain._settle_download_success.side_effect = RuntimeError("history unavailable")
    governance = SubscriptionDownloadGovernance(subscription_id=7, mode="normal")

    with pytest.raises(DownloadReconciliationRequired):
        chain.download_single(
            context=_context(),
            torrent_content=b"torrent",
            episodes={1},
            save_path="/downloads",
            governance=governance,
        )
    with pytest.raises(DownloadReconciliationRequired):
        chain.download_single(
            context=_context(),
            torrent_content=b"torrent",
            episodes={1},
            save_path="/downloads",
            governance=governance,
        )

    chain.download.assert_called_once()


def test_download_chain_freezes_when_downloader_result_is_uncertain(tmp_path) -> None:
    """下载器调用抛错可能已产生副作用，重启后的新实例仍不得自动重试。"""
    repository, _factory = _repository(tmp_path)
    chain = _download_chain(repository)
    chain.download = MagicMock(side_effect=TimeoutError("response timeout"))
    governance = SubscriptionDownloadGovernance(subscription_id=7, mode="normal")

    with pytest.raises(DownloadReconciliationRequired):
        chain.download_single(
            context=_context(),
            torrent_content=b"torrent",
            episodes={1},
            save_path="/downloads",
            governance=governance,
        )

    restarted = _download_chain(repository)
    restarted.download = MagicMock()
    with pytest.raises(DownloadReconciliationRequired):
        restarted.download_single(
            context=_context(),
            torrent_content=b"torrent",
            episodes={1},
            save_path="/downloads",
            governance=governance,
        )
    restarted.download.assert_not_called()


def test_download_chain_delays_retry_after_explicit_rejection(tmp_path) -> None:
    """下载器明确拒绝且无 hash 时进入冷却，不把不确定和已拒绝混为一谈。"""
    repository, factory = _repository(tmp_path)
    chain = _download_chain(repository)
    chain.download = MagicMock(return_value=("qb", None, "Original", "downloader rejected"))
    governance = SubscriptionDownloadGovernance(subscription_id=7, mode="normal")

    first = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=governance,
    )
    second = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=governance,
    )

    assert first is None and second is None
    chain.download.assert_called_once()
    with factory() as session:
        record = session.query(SubscriptionDownloadSubmission).one()
        assert record.state == "retryable"
        assert record.available_at > record.updated_at


def test_download_chain_reads_legacy_history_before_new_ledger(tmp_path) -> None:
    """迁移前同订阅同 torrent 同覆盖的成功历史仍能阻止重复提交。"""
    repository, factory = _repository(tmp_path)
    chain = _download_chain(repository)
    chain.download = MagicMock()
    chain.download_history_repository.get_by_media_identity.return_value = [
        SimpleNamespace(
            download_hash="legacy-hash",
            torrent_name="Demo Show S01E01 1080p",
            torrent_site="TestSite",
            episodes="E01",
            seasons="S01",
            episode_group=None,
            note={"source": 'Subscribe|{"id": 7}'},
        )
    ]

    result = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=SubscriptionDownloadGovernance(subscription_id=7, mode="normal"),
    )

    assert result == "legacy-hash"
    chain.download.assert_not_called()
    with factory() as session:
        assert session.query(SubscriptionDownloadSubmission).count() == 0


def test_download_chain_cancels_before_external_side_effect(tmp_path) -> None:
    """取消在下载器边界前生效时不得创建下载任务或成功事实。"""
    repository, _factory = _repository(tmp_path)
    chain = _download_chain(repository)
    chain.download = MagicMock()
    governance = SubscriptionDownloadGovernance(
        subscription_id=7,
        mode="normal",
        task_id="cancel-task",
        cancelled=lambda: True,
    )

    result = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=governance,
    )

    assert result is None
    chain.download.assert_not_called()
    assert not repository.has_started_for_task("cancel-task")


def test_subscription_policy_reloads_facts_and_threads_governance(monkeypatch) -> None:
    """提交策略必须使用当前订阅和重算缺集，而不是直接消费准备阶段快照。"""
    prepared = SubscriptionSnapshot(
        id=7,
        name="Demo Show",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB,
        media_id="77",
        season=1,
        state="R",
        save_path="/old",
        downloader="old",
    )
    current = SubscriptionSnapshot(
        **{
            **prepared.to_dict(),
            "save_path": "/current",
            "downloader": "current",
            "lack_episode": 1,
        }
    )
    context = _context()
    stale_missing = {"themoviedb:77": {1: SimpleNamespace(episodes=[1, 2])}}
    fresh_missing = {"themoviedb:77": {1: SimpleNamespace(episodes=[1])}}
    captured = {}

    class _FakeDownloadChain:
        """捕获提交策略传入的当前事实与治理上下文。"""

        def batch_download(self, **kwargs):
            """记录一次批量提交并返回当前缺集。"""
            captured.update(kwargs)
            return [], kwargs["no_exists"]

    chain = SubscribeChain.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(get=lambda _subscribe_id: current)
    chain.check_and_handle_existing_media = MagicMock(return_value=(False, fresh_missing))
    monkeypatch.setattr(
        chain,
        "_SubscribeChain__revalidate_download_contexts",
        lambda _subscribe, contexts: contexts,
    )
    monkeypatch.setattr(subscribe_policy, "DownloadChain", _FakeDownloadChain)

    _downloads, lefts = chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[context],
        no_exists=stale_missing,
        subscribe=prepared,
        mediakey="themoviedb:77",
        save_path="/old",
        downloader="old",
    )

    assert lefts is fresh_missing
    chain.check_and_handle_existing_media.assert_called_once_with(
        subscribe=current,
        meta=context.meta_info,
        mediainfo=context.media_info,
        mediakey="themoviedb:77",
    )
    assert captured["no_exists"] is fresh_missing
    assert captured["save_path"] == "/current"
    assert captured["downloader"] == "current"
    assert captured["governance"].subscription_id == 7
    assert captured["governance"].mode == "normal"


def test_subscription_policy_discards_candidates_after_filter_change(monkeypatch) -> None:
    """准备后筛选合同变化时必须放弃旧候选，不允许用旧快照提交。"""
    prepared = SubscriptionSnapshot(
        id=7,
        name="Demo Show",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB,
        media_id="77",
        season=1,
        state="R",
        quality="1080p",
    )
    current = SubscriptionSnapshot(**{**prepared.to_dict(), "quality": "2160p"})
    chain = SubscribeChain.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(get=lambda _subscribe_id: current)
    batch_download = MagicMock(side_effect=AssertionError("stale candidate submitted"))
    monkeypatch.setattr(subscribe_policy, "DownloadChain", lambda: SimpleNamespace(
        batch_download=batch_download,
    ))

    downloads, lefts = chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[_context()],
        no_exists={"themoviedb:77": {}},
        subscribe=prepared,
        mediakey="themoviedb:77",
    )

    assert downloads == []
    assert lefts == {"themoviedb:77": {}}
    batch_download.assert_not_called()


def test_subscription_download_migration_is_idempotent_and_reversible(tmp_path, monkeypatch) -> None:
    """3.0.21 迁移可重复升级，并只移除自身新增的兼容表。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    migration = importlib.import_module("database.versions.e1b6d4f8a2c7_3_0_21")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
        migration.upgrade()
        inspector = sa.inspect(connection)
        assert "subscriptiondownloadsubmission" in inspector.get_table_names()
        indexes = {item["name"] for item in inspector.get_indexes("subscriptiondownloadsubmission")}
        assert "ix_subscriptiondownloadsubmission_task_state" in indexes
        migration.downgrade()
        assert "subscriptiondownloadsubmission" not in sa.inspect(connection).get_table_names()


def test_model_metadata_registers_submission_table() -> None:
    """显式模型注册必须让 fresh create_all 包含订阅提交账本。"""
    assert SubscriptionDownloadSubmission.__tablename__ == "subscriptiondownloadsubmission"
    assert "subscriptiondownloadsubmission" in Base.metadata.tables
