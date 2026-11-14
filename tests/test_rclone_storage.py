"""
验证 Rclone 目录创建的并发串行化及延迟可见回退。
"""

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Union
from unittest.mock import patch

import pytest

from app import schemas
from app.modules.filemanager.storages import rclone as rclone_module
from app.modules.filemanager.storages.rclone import Rclone


@pytest.fixture
def _isolate_folder_locks():
    """
    在空目录锁表下运行用例，并在结束时恢复原有锁记录。
    """
    with rclone_module._folder_locks_guard:
        original_locks = dict(rclone_module._folder_locks)
        rclone_module._folder_locks.clear()
    try:
        yield
    finally:
        with rclone_module._folder_locks_guard:
            rclone_module._folder_locks.clear()
            rclone_module._folder_locks.update(original_locks)


def _normalize(path: Union[Path, str]) -> str:
    """
    调用 Rclone 的远端路径规范化逻辑。
    """
    return Rclone._Rclone__normalize_remote_path(path)


def _make_dir_item(path: Union[Path, str]) -> schemas.FileItem:
    """
    按规范化路径生成 Rclone 目录项。
    """
    normalized = _normalize(path)
    name = Path(normalized).name or "/"
    return schemas.FileItem(
        storage="rclone",
        type="dir",
        path="/" if normalized == "/" else f"{normalized}/",
        name=name,
        basename=name,
    )


@pytest.mark.usefixtures("_isolate_folder_locks")
def test_get_folder_serializes_same_target_directory_creation() -> None:
    """
    多个线程创建相同目录时仅执行一次实际创建。
    """
    storage = Rclone()
    thread_count = 4
    start_event = threading.Event()
    missing_barrier = threading.Barrier(thread_count)
    state_lock = threading.Lock()
    existing_paths = {"/"}
    mkdir_calls = []
    results = []
    errors = []

    def fake_get_item(_self, path: Path):
        """
        按测试共享状态返回目录项，模拟目录创建前后及延迟可见场景。
        """
        normalized = _normalize(path)
        with state_lock:
            exists = normalized in existing_paths
        if not exists and normalized == "/Show":
            try:
                missing_barrier.wait(timeout=0.1)
            except threading.BrokenBarrierError:
                pass
            with state_lock:
                exists = normalized in existing_paths
        if exists:
            return _make_dir_item(normalized)
        return None

    def fake_run(cmd, *args, **kwargs):
        """
        记录目录创建命令并更新共享目录集合，不启动真实 Rclone 进程。
        """
        target = _normalize(cmd[-1].removeprefix("MP:"))
        with state_lock:
            mkdir_calls.append(target)
            existing_paths.add(target)
        return SimpleNamespace(returncode=0)

    def worker():
        """
        在线程中申请目标目录，并记录返回结果或异常供主线程断言。
        """
        try:
            start_event.wait()
            results.append(storage.get_folder(Path("/Show/Season 1")))
        # 收集线程中的意外异常，由主线程统一断言，避免工作线程失败被忽略。
        except Exception as err:  # pragma: no cover
            errors.append(err)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]

    with patch.object(Rclone, "get_item", autospec=True, side_effect=fake_get_item):
        with patch(
            "app.modules.filemanager.storages.rclone.subprocess.run",
            side_effect=fake_run,
        ):
            for thread in threads:
                thread.start()
            start_event.set()
            for thread in threads:
                thread.join(timeout=1)

    assert not errors
    assert all((not thread.is_alive() for thread in threads))
    assert thread_count == len(results)
    assert all((result and result.path == "/Show/Season 1/" for result in results))
    assert 1 == mkdir_calls.count("/Show")
    assert 1 == mkdir_calls.count("/Show/Season 1")


@pytest.mark.usefixtures("_isolate_folder_locks")
def test_get_folder_serializes_shared_parent_creation() -> None:
    """
    不同目标共享父目录时串行创建公共父目录。
    """
    storage = Rclone()
    thread_count = 4
    start_event = threading.Event()
    missing_barrier = threading.Barrier(thread_count)
    state_lock = threading.Lock()
    existing_paths = {"/"}
    mkdir_calls = []
    results = []
    errors = []
    targets = [
        Path("/Show/Season 1"),
        Path("/Show/Season 2"),
        Path("/Show/Season 1"),
        Path("/Show/Season 2"),
    ]

    def fake_get_item(_self, path: Path):
        """
        按测试共享状态返回目录项，模拟目录创建前后及延迟可见场景。
        """
        normalized = _normalize(path)
        with state_lock:
            exists = normalized in existing_paths
        if not exists and normalized == "/Show":
            try:
                missing_barrier.wait(timeout=0.1)
            except threading.BrokenBarrierError:
                pass
            with state_lock:
                exists = normalized in existing_paths
        if exists:
            return _make_dir_item(normalized)
        return None

    def fake_run(cmd, *args, **kwargs):
        """
        记录目录创建命令并更新共享目录集合，不启动真实 Rclone 进程。
        """
        target = _normalize(cmd[-1].removeprefix("MP:"))
        with state_lock:
            mkdir_calls.append(target)
            existing_paths.add(target)
        return SimpleNamespace(returncode=0)

    def worker(target: Path):
        """
        在线程中申请目标目录，并记录返回结果或异常供主线程断言。
        """
        try:
            start_event.wait()
            results.append(storage.get_folder(target))
        # 收集线程中的意外异常，由主线程统一断言，避免工作线程失败被忽略。
        except Exception as err:  # pragma: no cover
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(target,)) for target in targets]

    with patch.object(Rclone, "get_item", autospec=True, side_effect=fake_get_item):
        with patch(
            "app.modules.filemanager.storages.rclone.subprocess.run",
            side_effect=fake_run,
        ):
            for thread in threads:
                thread.start()
            start_event.set()
            for thread in threads:
                thread.join(timeout=1)

    assert not errors
    assert all((not thread.is_alive() for thread in threads))
    assert 4 == len(results)
    assert 1 == mkdir_calls.count("/Show")
    assert 1 == mkdir_calls.count("/Show/Season 1")
    assert 1 == mkdir_calls.count("/Show/Season 2")


@pytest.mark.usefixtures("_isolate_folder_locks")
def test_create_folder_retries_visibility_after_successful_mkdir() -> None:
    """
    目录创建成功但暂不可见时重试读取元数据。
    """
    storage = Rclone()
    expected = _make_dir_item("/Show")
    responses = [None, expected]

    def fake_get_item(_self, path: Path):
        """
        按测试共享状态返回目录项，模拟目录创建前后及延迟可见场景。
        """
        return responses.pop(0)

    with patch.object(Rclone, "get_item", autospec=True, side_effect=fake_get_item):
        with patch(
            "app.modules.filemanager.storages.rclone.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run_mock:
            with patch("app.modules.filemanager.storages.rclone.time.sleep", return_value=None):
                folder = storage.create_folder(
                    schemas.FileItem(storage="rclone", type="dir", path="/"),
                    "Show",
                )

    assert "/Show/" == folder.path
    run_mock.assert_called_once()


@pytest.mark.usefixtures("_isolate_folder_locks")
def test_create_folder_accepts_existing_directory_after_failed_mkdir() -> None:
    """
    创建命令失败后若目录已存在，仍返回有效目录项。
    """
    storage = Rclone()
    expected = _make_dir_item("/Show")
    responses = [None, expected]

    def fake_get_item(_self, path: Path):
        """
        按测试共享状态返回目录项，模拟目录创建前后及延迟可见场景。
        """
        return responses.pop(0)

    with patch.object(Rclone, "get_item", autospec=True, side_effect=fake_get_item):
        with patch(
            "app.modules.filemanager.storages.rclone.subprocess.run",
            return_value=SimpleNamespace(returncode=1),
        ) as run_mock:
            with patch("app.modules.filemanager.storages.rclone.time.sleep", return_value=None):
                folder = storage.create_folder(
                    schemas.FileItem(storage="rclone", type="dir", path="/"),
                    "Show",
                )

    assert "/Show/" == folder.path
    run_mock.assert_called_once()


@pytest.mark.usefixtures("_isolate_folder_locks")
def test_folder_lock_table_evicts_old_unlocked_paths() -> None:
    """
    路径锁表超过上限时应优先淘汰未占用的旧锁。
    """
    with patch.object(rclone_module, "_MAX_FOLDER_LOCKS", 2):
        first_lock = Rclone._Rclone__get_path_lock(Path("/A"))
        second_lock = Rclone._Rclone__get_path_lock(Path("/B"))
        third_lock = Rclone._Rclone__get_path_lock(Path("/C"))

    assert "/A" not in rclone_module._folder_locks
    assert "/B" in rclone_module._folder_locks
    assert "/C" in rclone_module._folder_locks
    assert first_lock is not third_lock
    assert second_lock is rclone_module._folder_locks["/B"]
