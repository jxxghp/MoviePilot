"""暂停订阅中已接纳搜索的真实下载边界回归。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.chain.download.batch as download_batch
import app.chain.download.selection as download_selection
from app.application.subscription.contract import SubscriptionSnapshot
from app.application.subscription.execution import (
    SubscriptionExecutionAdmission,
    SubscriptionExecutionContext,
)
from app.chain.subscribe import policy as subscribe_policy
from app.chain.subscribe.facade import SubscribeChain
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaSource, MediaType
from tests.test_subscription_download_governance import (
    _context,
    _download_chain,
)
from tests.test_subscription_download_governance import (
    _submission_dependencies as _submission_dependencies,
)

pytestmark = pytest.mark.usefixtures("_submission_dependencies")


@pytest.fixture
def download_scope(monkeypatch):
    """保留订阅策略、候选选择与单资源提交，只隔离存储和外部下载器。"""
    current = SubscriptionSnapshot(
        id=7,
        name="Demo Show",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB,
        media_id="77",
        season=1,
        total_episode=1,
        state="S",
        save_path="/downloads",
        downloader="qb",
    )
    context = _context()
    missing = {"tmdb:77": {1: NotExistMediaInfo(season=1, episodes=[1], total_episode=1)}}
    download = _download_chain()
    download.download_torrent = MagicMock(return_value=(b"torrent", None, [1]))
    download._active_download_failure_fingerprints = MagicMock(return_value={})
    download.download = MagicMock(return_value=("qb", "hash-paused-search", "Original", "accepted"))
    chain = SubscribeChain.__new__(SubscribeChain)
    chain.subscription_repository = SimpleNamespace(get=MagicMock(return_value=current))
    chain.check_and_handle_existing_media = MagicMock(return_value=(False, missing))
    helper = SimpleNamespace(sort_torrents=lambda contexts: contexts)
    monkeypatch.setattr(download_selection, "_new_torrent_helper", lambda: helper)
    monkeypatch.setattr(download_batch, "runtime_stop_state", SimpleNamespace(is_system_stopped=False))
    monkeypatch.setattr(download_selection, "runtime_stop_state", SimpleNamespace(is_system_stopped=False))
    monkeypatch.setattr(subscribe_policy, "DownloadChain", lambda: download)
    return SimpleNamespace(
        current=current,
        context=context,
        missing=missing,
        download=download,
        chain=chain,
    )


def _execution(operation="search", cancelled=lambda: False):
    """固定执行时钟，保留真实租约、取消与副作用阶段信号。"""
    admission = SubscriptionExecutionAdmission(clock=lambda: 0)
    lease = admission.try_acquire(subscription_id=7, operation=operation, ttl_seconds=60)
    assert lease is not None
    return SubscriptionExecutionContext(
        lease=lease,
        admission=admission,
        task_id="search-task-7",
        cancel_requested=cancelled,
    )


def _submit(scope, prepared, execution_context):
    return scope.chain._SubscribeChain__download_best_version_with_full_pack_first(
        contexts=[scope.context],
        no_exists={"tmdb:77": {1: NotExistMediaInfo(season=1, episodes=[1], total_episode=1)}},
        subscribe=prepared,
        mediakey="tmdb:77",
        execution_context=execution_context,
    )


@pytest.mark.parametrize("prepared_state", ["R", "S"])
def test_accepted_search_downloads_while_subscription_is_paused(download_scope, prepared_state):
    """搜索中途暂停和指定暂停订阅补搜均能提交，不主动恢复订阅状态。"""
    scope = download_scope
    execution = _execution()

    downloads, remaining = _submit(scope, replace(scope.current, state=prepared_state), execution)

    assert downloads == [scope.context]
    assert remaining == {}
    scope.download.download.assert_called_once()
    assert scope.download.download.call_args.kwargs["downloader"] == "qb"
    assert scope.download.download.call_args.kwargs["content"] == b"torrent"
    scope.download._settle_download_success.assert_called_once()
    assert execution.download_started is True
    assert scope.current.state == "S"
    assert execution.admission.release(execution.lease) is True


@pytest.mark.parametrize("operation", [None, "match"])
def test_paused_subscription_blocks_download_without_search_execution(download_scope, operation):
    """无搜索上下文和自动匹配不能借暂停订阅创建下载任务。"""
    scope = download_scope
    execution = _execution(operation) if operation else None

    downloads, remaining = _submit(scope, scope.current, execution)

    assert downloads == []
    assert remaining["tmdb:77"][1].episodes == [1]
    scope.chain.check_and_handle_existing_media.assert_not_called()
    scope.download.download.assert_not_called()
    scope.download._settle_download_success.assert_not_called()
    if execution:
        assert execution.download_started is False
        assert execution.admission.release(execution.lease) is True


def test_paused_search_still_honors_cancellation_at_downloader_boundary(download_scope):
    """资源准备期间的取消仍在真实下载器调用前生效。"""
    scope = download_scope
    cancelled = [False]
    execution = _execution(cancelled=lambda: cancelled[0])

    def prepare_torrent(_torrent, **_kwargs):
        cancelled[0] = True
        return b"torrent", None, [1]

    scope.download.download_torrent.side_effect = prepare_torrent

    downloads, remaining = _submit(scope, scope.current, execution)

    scope.chain.check_and_handle_existing_media.assert_called_once()
    scope.download.download_torrent.assert_called_once()
    assert downloads == []
    assert remaining["tmdb:77"][1].episodes == [1]
    scope.download.download.assert_not_called()
    scope.download._settle_download_success.assert_not_called()
    assert execution.download_started is False
    assert execution.admission.release(execution.lease) is True


@pytest.mark.parametrize("change", ["deleted", "filter", "identity"])
def test_paused_search_discards_invalidated_candidates(download_scope, change):
    """暂停搜索的放行不能绕过删除、筛选或媒体身份变化。"""
    scope = download_scope
    prepared = replace(scope.current, state="R")
    current = {
        "deleted": None,
        "filter": replace(scope.current, quality="2160p"),
        "identity": replace(scope.current, media_id="88"),
    }[change]
    scope.chain.subscription_repository.get.return_value = current
    execution = _execution()

    downloads, remaining = _submit(scope, prepared, execution)

    assert downloads == []
    assert remaining["tmdb:77"][1].episodes == [1]
    scope.chain.check_and_handle_existing_media.assert_not_called()
    scope.download.download.assert_not_called()
    scope.download._settle_download_success.assert_not_called()
    assert execution.download_started is False
    assert execution.admission.release(execution.lease) is True
