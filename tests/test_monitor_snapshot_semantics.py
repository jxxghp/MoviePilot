from pathlib import Path
from unittest.mock import MagicMock

from app.monitor.poller import RemotePoller
from app.monitor.watcher import LocalDirectoryWatcher


def _build_poller(alert_cb=None):
    """
    构造测试用远程轮询监控。
    :param alert_cb: 告警回调替身
    :return: (poller, store, dispatcher)
    """
    store = MagicMock()
    store.save.return_value = True
    dispatcher = MagicMock()
    dispatcher.is_transfer_candidate_path.return_value = True
    dispatcher.handle_file.return_value = True
    poller = RemotePoller(store=store, dispatcher=dispatcher, alert_cb=alert_cb)
    return poller, store, dispatcher


def _mock_storage_chain(monkeypatch, side_effect):
    """
    替换 StorageChain 的快照返回。
    :param monkeypatch: pytest monkeypatch
    :param side_effect: snapshot_storage 的返回序列
    :return: StorageChain 实例替身
    """
    chain_instance = MagicMock()
    chain_instance.snapshot_storage.side_effect = side_effect
    monkeypatch.setattr("app.monitor.poller.StorageChain", MagicMock(return_value=chain_instance))
    return chain_instance


BASELINE = {
    'timestamp': 100,
    'file_count': 1,
    'snapshot': {'/mon/a.mkv': {'size': 1, 'modify_time': 100}}
}


def test_poll_merges_incremental_into_baseline(monkeypatch):
    """
    增量快照应与基线合并落盘，未扫到的旧文件不能从基线消失。
    """
    poller, store, dispatcher = _build_poller()
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(monkeypatch, [{'/mon/b.mkv': {'size': 2, 'modify_time': 200}}])

    file_count = poller.poll("u115", [Path("/mon")])

    assert file_count == 2
    saved_snapshot = store.save.call_args.args[1]
    assert set(saved_snapshot.keys()) == {'/mon/a.mkv', '/mon/b.mkv'}
    dispatcher.handle_file.assert_called_once()
    assert dispatcher.handle_file.call_args.kwargs["event_path"] == Path('/mon/b.mkv')


def test_poll_detects_modified_files(monkeypatch):
    """
    增量中已有文件的大小变化应作为修改事件分发，并更新基线。
    """
    poller, store, dispatcher = _build_poller()
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(monkeypatch, [{'/mon/a.mkv': {'size': 5, 'modify_time': 300}}])

    file_count = poller.poll("u115", [Path("/mon")])

    assert file_count == 1
    saved_snapshot = store.save.call_args.args[1]
    assert saved_snapshot['/mon/a.mkv']['size'] == 5
    dispatcher.handle_file.assert_called_once()


def test_poll_partial_failure_merges_success_and_keeps_baseline(monkeypatch):
    """
    部分路径快照失败时，成功路径合并落盘，失败路径保留旧基线。
    """
    alert_cb = MagicMock()
    poller, store, dispatcher = _build_poller(alert_cb)
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(monkeypatch, [None, {'/mon2/b.mkv': {'size': 2, 'modify_time': 200}}])

    file_count = poller.poll("u115", [Path("/mon"), Path("/mon2")])

    assert file_count == 2
    saved_snapshot = store.save.call_args.args[1]
    assert set(saved_snapshot.keys()) == {'/mon/a.mkv', '/mon2/b.mkv'}
    # 单次失败未达告警阈值
    alert_cb.assert_not_called()


def test_poll_all_paths_failed_skips_save(monkeypatch):
    """
    全部路径快照失败时本轮不落盘，基线保持不变。
    """
    poller, store, dispatcher = _build_poller()
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(monkeypatch, [None])

    assert poller.poll("u115", [Path("/mon")]) is None
    store.save.assert_not_called()
    dispatcher.handle_file.assert_not_called()


def test_poll_first_snapshot_failure_builds_no_empty_baseline(monkeypatch):
    """
    首次快照失败时不得落盘空基线，否则下一轮会把全部存量当作新增。
    """
    poller, store, dispatcher = _build_poller()
    store.load_checked.return_value = (None, True)
    _mock_storage_chain(monkeypatch, [None])

    assert poller.poll("u115", [Path("/mon")]) is None
    store.save.assert_not_called()


def test_poll_first_snapshot_success_saves_baseline_without_dispatch(monkeypatch):
    """
    首次快照成功仅建立基准，不应处理存量文件。
    """
    poller, store, dispatcher = _build_poller()
    store.load_checked.return_value = (None, True)
    _mock_storage_chain(monkeypatch, [{'/mon/a.mkv': {'size': 1, 'modify_time': 100}}])

    assert poller.poll("u115", [Path("/mon")]) == 1
    store.save.assert_called_once()
    dispatcher.handle_file.assert_not_called()


def test_poll_load_error_skips_round(monkeypatch):
    """
    基线读取失败不能当作首次快照，应跳过本轮避免丢弃已有基线。
    """
    poller, store, dispatcher = _build_poller()
    store.load_checked.return_value = (None, False)
    chain = _mock_storage_chain(monkeypatch, [{'/mon/a.mkv': {'size': 1, 'modify_time': 100}}])

    assert poller.poll("u115", [Path("/mon")]) is None
    chain.snapshot_storage.assert_not_called()
    store.save.assert_not_called()


def test_poll_failure_alert_threshold_and_recovery(monkeypatch):
    """
    连续异常达到阈值只告警一次，恢复后推送恢复消息。
    """
    alert_cb = MagicMock()
    poller, store, dispatcher = _build_poller(alert_cb)
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(
        monkeypatch,
        [None] * RemotePoller.FAILURE_ALERT_THRESHOLD + [{'/mon/b.mkv': {'size': 2, 'modify_time': 200}}]
    )

    for _ in range(RemotePoller.FAILURE_ALERT_THRESHOLD):
        poller.poll("u115", [Path("/mon")])
    assert alert_cb.call_count == 1

    poller.poll("u115", [Path("/mon")])
    assert alert_cb.call_count == 2
    assert "已恢复" in alert_cb.call_args.args[1]


def test_watcher_poll_delay_defaults_and_override(tmp_path):
    """
    轮询扫描间隔默认取本地值，显式传入网络值时生效。
    """
    default_watcher = LocalDirectoryWatcher(tmp_path, callback=MagicMock(), force_polling=True)
    assert default_watcher.poll_delay_ms == LocalDirectoryWatcher.POLL_DELAY_LOCAL_MS

    network_watcher = LocalDirectoryWatcher(
        tmp_path, callback=MagicMock(), force_polling=True,
        poll_delay_ms=LocalDirectoryWatcher.POLL_DELAY_NETWORK_MS
    )
    assert network_watcher.poll_delay_ms == LocalDirectoryWatcher.POLL_DELAY_NETWORK_MS
