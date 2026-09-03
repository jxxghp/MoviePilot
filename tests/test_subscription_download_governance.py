"""订阅下载取消边界、普通失败语义与账本移除迁移测试。"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

import app.chain.download.submission as download_submission
from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.execution import (
    SubscriptionExecutionAdmission,
    SubscriptionExecutionContext,
)
from app.chain.download import DownloadChain
from app.chain.subscribe import policy as subscribe_policy
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType


class _FakeTorrentHelper:
    """返回固定种子目录和文件清单，隔离真实 bencode 解析。"""

    @staticmethod
    def get_fileinfo_from_torrent_content(_content):
        """返回单文件种子结构。"""
        return "Demo.Show.S01", ["Demo.Show.S01E01.mkv"]


def _download_chain() -> DownloadChain:
    """构造只执行下载提交边界的 Chain 测试实例。"""
    chain = DownloadChain.__new__(DownloadChain)
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


def test_download_chain_uses_normal_submission_for_each_execution() -> None:
    """下载边界不保留本地提交账本，由下载器处理相同 torrent 的复用。"""
    chain = _download_chain()
    chain.download = MagicMock(return_value=("qb", "hash-1", "Original", "accepted"))
    governance = SubscriptionDownloadGovernance()

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
    assert chain.download.call_count == 2
    assert chain._settle_download_success.call_count == 2


def test_download_chain_retries_after_downloader_exception() -> None:
    """下载器异常不冻结后续执行，下一轮仍按普通下载合同重新提交。"""
    chain = _download_chain()
    chain.download = MagicMock(side_effect=[
        TimeoutError("response timeout"),
        ("qb", "hash-after-timeout", "Original", "accepted"),
    ])

    with pytest.raises(TimeoutError, match="response timeout"):
        chain.download_single(
            context=_context(),
            torrent_content=b"torrent",
            episodes={1},
            save_path="/downloads",
            governance=SubscriptionDownloadGovernance(),
        )

    result = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=SubscriptionDownloadGovernance(),
    )

    assert result == "hash-after-timeout"
    assert chain.download.call_count == 2
    chain._settle_download_success.assert_called_once()


def test_download_chain_retries_after_local_settlement_failure() -> None:
    """本地结算异常直接失败，下一轮不依赖补偿状态即可重新执行。"""
    chain = _download_chain()
    chain.download = MagicMock(return_value=("qb", "hash-2", "Original", "accepted"))
    chain._settle_download_success.side_effect = [RuntimeError("history unavailable"), None]

    with pytest.raises(RuntimeError, match="history unavailable"):
        chain.download_single(
            context=_context(),
            torrent_content=b"torrent",
            episodes={1},
            save_path="/downloads",
            governance=SubscriptionDownloadGovernance(),
        )

    result = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=SubscriptionDownloadGovernance(),
    )

    assert result == "hash-2"
    assert chain.download.call_count == 2
    assert chain._settle_download_success.call_count == 2


def test_download_chain_records_explicit_rejection_as_normal_failure() -> None:
    """下载器明确拒绝仍写入既有资源失败冷却，不建立提交恢复状态。"""
    chain = _download_chain()
    chain.download = MagicMock(return_value=("qb", None, "Original", "downloader rejected"))

    result = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=SubscriptionDownloadGovernance(),
    )

    assert result is None
    chain.download_failure_repository.record_failure.assert_called_once()
    chain.post_message.assert_called_once()


def test_download_chain_cancels_before_external_side_effect() -> None:
    """取消在下载器边界前生效时不得创建下载任务或成功事实。"""
    chain = _download_chain()
    chain.download = MagicMock()
    governance = SubscriptionDownloadGovernance(cancelled=lambda: True)

    result = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=governance,
    )

    assert result is None
    chain.download.assert_not_called()
    chain._settle_download_success.assert_not_called()


def test_download_chain_marks_side_effect_boundary_before_downloader_call() -> None:
    """下载器调用前必须标记副作用起点，使晚到取消按真实执行结果收口。"""
    chain = _download_chain()
    order: list[str] = []

    def download(**_kwargs):
        """记录下载器调用顺序并返回成功结果。"""
        order.append("download")
        return "qb", "hash-3", "Original", "accepted"

    chain.download = download
    governance = SubscriptionDownloadGovernance(
        mark_started=lambda: order.append("started"),
    )

    result = chain.download_single(
        context=_context(),
        torrent_content=b"torrent",
        episodes={1},
        save_path="/downloads",
        governance=governance,
    )

    assert result == "hash-3"
    assert order == ["started", "download"]


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
    admission = SubscriptionExecutionAdmission()
    lease = admission.try_acquire(
        subscription_id=current.id,
        operation="search",
        ttl_seconds=60,
    )
    assert lease is not None
    execution_context = SubscriptionExecutionContext(
        lease=lease,
        admission=admission,
        task_id="search-task-7",
    )

    _downloads, lefts = chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[context],
        no_exists=stale_missing,
        subscribe=prepared,
        mediakey="themoviedb:77",
        save_path="/old",
        downloader="old",
        execution_context=execution_context,
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
    assert captured["governance"].cancelled() is False
    captured["governance"].mark_started()
    assert execution_context.download_started is True
    assert admission.release(lease) is True


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


def test_subscription_download_ledger_removal_migration_is_reversible(
    tmp_path,
    monkeypatch,
) -> None:
    """3.0.25 删除旧账本；降级只恢复结构，不伪造已删除的运行状态。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    create_ledger = importlib.import_module("database.versions.e1b6d4f8a2c7_3_0_21")
    add_delivery_scope = importlib.import_module("database.versions.a7d9e2c4f6b1_3_0_23")
    remove_ledger = importlib.import_module("database.versions.c8f2e6a1d4b9_3_0_25")

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(create_ledger, "op", operations)
        monkeypatch.setattr(add_delivery_scope, "op", operations)
        monkeypatch.setattr(remove_ledger, "op", operations)
        create_ledger.upgrade()
        add_delivery_scope.upgrade()
        connection.execute(sa.text(
            "INSERT INTO subscriptiondownloadsubmission "
            "(idempotency_key, subscription_id, logical_identity, resource_key, coverage, mode, "
            "delivery_scope, state, attempt_count, created_at, updated_at) VALUES "
            "('legacy-key', 7, '{}', 'resource', 'full', 'normal', 'legacy', "
            "'reconcile_required', 1, 'now', 'now')"
        ))

        remove_ledger.upgrade()
        remove_ledger.upgrade()
        assert "subscriptiondownloadsubmission" not in sa.inspect(connection).get_table_names()

        remove_ledger.downgrade()
        remove_ledger.downgrade()
        inspector = sa.inspect(connection)
        assert "subscriptiondownloadsubmission" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns(
            "subscriptiondownloadsubmission"
        )} >= {"idempotency_key", "delivery_scope", "state", "download_hash"}
        indexes = {
            item["name"]
            for item in inspector.get_indexes("subscriptiondownloadsubmission")
        }
        assert indexes == {
            "ix_subscriptiondownloadsubmission_subscription_state",
            "ix_subscriptiondownloadsubmission_task_state",
        }
        assert connection.execute(sa.text(
            "SELECT COUNT(*) FROM subscriptiondownloadsubmission"
        )).scalar_one() == 0
