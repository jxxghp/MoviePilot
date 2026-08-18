import json
from pathlib import Path
from unittest.mock import MagicMock

from app import schemas
from app.modules._base.storage import StorageBase
from app.monitor.poller import RemotePoller
from app.monitor.snapshot import SnapshotStore
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
    chain = _mock_storage_chain(monkeypatch, [{
        '/mon/a.mkv': {'size': 1, 'modify_time': 100},
        '/mon/b.mkv': {'size': 2, 'modify_time': 200}
    }])

    file_count = poller.poll("u115", [Path("/mon")])

    assert file_count == 2
    saved_snapshot = store.save.call_args.args[1]
    assert set(saved_snapshot.keys()) == {'/mon/a.mkv', '/mon/b.mkv'}
    assert chain.snapshot_storage.call_args.kwargs["previous_snapshot"] == BASELINE["snapshot"]
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


def test_poll_removes_deleted_files_from_count(monkeypatch):
    """
    已移出监控目录的文件应从完整基线和动态间隔计数中移除。
    """
    poller, store, dispatcher = _build_poller()
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(monkeypatch, [{}])

    assert poller.poll("alist", [Path("/mon")]) == 0
    assert store.save.call_args.args[1] == {}
    assert store.save.call_args.args[2] == 0
    dispatcher.handle_file.assert_not_called()


def test_storage_snapshot_reconciles_deleted_children_and_keeps_skipped_subtree():
    """
    增量遍历应删除已消失的直接子项，同时保留未变化子目录的旧基线。
    """
    storage = MagicMock()
    storage.snapshot_check_folder_modtime = True
    root = schemas.FileItem(storage="alist", type="dir", path="/mon/", name="mon", modify_time=200)
    kept_dir = schemas.FileItem(
        storage="alist", type="dir", path="/mon/keep/", name="keep", modify_time=50
    )
    storage.get_item.return_value = root
    storage.list.return_value = [kept_dir]
    previous_snapshot = {
        '/mon/gone.mkv': {'size': 1, 'modify_time': 100},
        '/mon/keep/a.mkv': {'size': 2, 'modify_time': 50}
    }

    snapshot = StorageBase.snapshot(
        storage,
        Path("/mon"),
        last_snapshot_time=100,
        previous_snapshot=previous_snapshot
    )

    assert snapshot == {'/mon/keep/a.mkv': {'size': 2, 'modify_time': 50}}


def test_storage_snapshot_always_lists_monitor_root_for_deletions():
    """
    即使根目录修改时间未推进，也应列举其直接子项以清理已移走文件。
    """
    storage = MagicMock()
    storage.snapshot_check_folder_modtime = True
    storage.get_item.return_value = schemas.FileItem(
        storage="alist", type="dir", path="/mon/", name="mon", modify_time=50
    )
    storage.list.return_value = []

    snapshot = StorageBase.snapshot(
        storage,
        Path("/mon"),
        last_snapshot_time=100,
        previous_snapshot={'/mon/gone.mkv': {'size': 1, 'modify_time': 100}}
    )

    assert snapshot == {}
    storage.list.assert_called_once()


def test_snapshot_store_marks_reconciled_format_and_keeps_cursor():
    """
    新快照应标记对账格式，删除最新文件后游标也不能倒退。
    """
    cache = MagicMock()
    store = SnapshotStore(cache=cache)

    assert store.save(
        "alist",
        {'/mon/a.mkv': {'size': 1, 'modify_time': 50}},
        file_count=1,
        last_snapshot_time=100
    ) is True

    payload = json.loads(cache.set.call_args.args[1].decode("utf-8"))
    assert payload["version"] == SnapshotStore.VERSION
    assert payload["timestamp"] == 100
    assert payload["file_count"] == 1


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


def test_poll_partial_failure_pins_incremental_cursor(monkeypatch):
    """
    部分路径失败时增量游标必须固定在旧值，否则失败路径中时间落在新旧游标
    之间的变更会被后续增量查询永久跳过。
    """
    poller, store, _ = _build_poller()
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(monkeypatch, [None, {'/mon2/b.mkv': {'size': 2, 'modify_time': 999}}])

    poller.poll("u115", [Path("/mon"), Path("/mon2")])

    # 游标固定为旧基线时间 100，而不是成功路径产生的 999
    assert store.save.call_args.kwargs["snapshot_time"] == 100


def test_poll_full_success_advances_cursor(monkeypatch):
    """
    全部路径成功时不固定游标，由快照内容推进增量游标。
    """
    poller, store, _ = _build_poller()
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(monkeypatch, [{'/mon/b.mkv': {'size': 2, 'modify_time': 999}}])

    poller.poll("u115", [Path("/mon")])

    assert store.save.call_args.kwargs["snapshot_time"] is None


def test_force_full_scan_aborts_on_baseline_load_error(monkeypatch):
    """
    全量扫描读取基线失败时必须放弃落盘，否则会抹掉同存储其他监控路径的基线。
    """
    poller, store, _ = _build_poller()
    store.load_checked.return_value = (None, False)
    _mock_storage_chain(monkeypatch, [{'/mon/a.mkv': {'size': 1, 'modify_time': 100}}])

    assert poller.force_full_scan("u115", Path("/mon")) is False
    store.save.assert_not_called()


def test_force_full_scan_reports_save_failure(monkeypatch):
    """
    全量扫描持久化失败必须传播为失败，不能返回成功掩盖基线未更新。
    """
    poller, store, _ = _build_poller()
    store.load_checked.return_value = (dict(BASELINE), True)
    store.save.return_value = False
    _mock_storage_chain(monkeypatch, [{'/mon/b.mkv': {'size': 2, 'modify_time': 200}}])

    assert poller.force_full_scan("u115", Path("/mon")) is False


def test_force_full_scan_merges_with_existing_baseline(monkeypatch):
    """
    全量扫描只覆盖单个路径，应与已有基线合并后落盘。
    """
    poller, store, _ = _build_poller()
    store.load_checked.return_value = (dict(BASELINE), True)
    _mock_storage_chain(monkeypatch, [{'/mon2/b.mkv': {'size': 2, 'modify_time': 200}}])

    assert poller.force_full_scan("u115", Path("/mon2")) is True
    saved_snapshot = store.save.call_args.args[1]
    assert set(saved_snapshot.keys()) == {'/mon/a.mkv', '/mon2/b.mkv'}


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
