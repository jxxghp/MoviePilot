import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Union
from unittest.mock import patch

from app import schemas
from app.modules.filemanager.storages import rclone as rclone_module
from app.modules.filemanager.storages.rclone import Rclone


class RcloneStorageTest(unittest.TestCase):
    def setUp(self):
        with rclone_module._folder_locks_guard:
            rclone_module._folder_locks.clear()

    @staticmethod
    def _normalize(path: Union[Path, str]) -> str:
        return Rclone._Rclone__normalize_remote_path(path)

    def _make_dir_item(self, path: Union[Path, str]) -> schemas.FileItem:
        normalized = self._normalize(path)
        name = Path(normalized).name or "/"
        return schemas.FileItem(
            storage="rclone",
            type="dir",
            path="/" if normalized == "/" else f"{normalized}/",
            name=name,
            basename=name,
        )

    def test_get_folder_serializes_same_target_directory_creation(self):
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
            normalized = self._normalize(path)
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
                return self._make_dir_item(normalized)
            return None

        def fake_run(cmd, *args, **kwargs):
            target = self._normalize(cmd[-1].removeprefix("MP:"))
            with state_lock:
                mkdir_calls.append(target)
                existing_paths.add(target)
            return SimpleNamespace(returncode=0)

        def worker():
            try:
                start_event.wait()
                results.append(storage.get_folder(Path("/Show/Season 1")))
            except Exception as err:  # pragma: no cover - 仅用于调试失败
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

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(thread_count, len(results))
        self.assertTrue(all(result and result.path == "/Show/Season 1/" for result in results))
        self.assertEqual(1, mkdir_calls.count("/Show"))
        self.assertEqual(1, mkdir_calls.count("/Show/Season 1"))

    def test_get_folder_serializes_shared_parent_creation(self):
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
            normalized = self._normalize(path)
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
                return self._make_dir_item(normalized)
            return None

        def fake_run(cmd, *args, **kwargs):
            target = self._normalize(cmd[-1].removeprefix("MP:"))
            with state_lock:
                mkdir_calls.append(target)
                existing_paths.add(target)
            return SimpleNamespace(returncode=0)

        def worker(target: Path):
            try:
                start_event.wait()
                results.append(storage.get_folder(target))
            except Exception as err:  # pragma: no cover - 仅用于调试失败
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

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(4, len(results))
        self.assertEqual(1, mkdir_calls.count("/Show"))
        self.assertEqual(1, mkdir_calls.count("/Show/Season 1"))
        self.assertEqual(1, mkdir_calls.count("/Show/Season 2"))

    def test_create_folder_retries_visibility_after_successful_mkdir(self):
        storage = Rclone()
        expected = self._make_dir_item("/Show")
        responses = [None, expected]

        def fake_get_item(_self, path: Path):
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

        self.assertEqual("/Show/", folder.path)
        run_mock.assert_called_once()

    def test_create_folder_accepts_existing_directory_after_failed_mkdir(self):
        storage = Rclone()
        expected = self._make_dir_item("/Show")
        responses = [None, expected]

        def fake_get_item(_self, path: Path):
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

        self.assertEqual("/Show/", folder.path)
        run_mock.assert_called_once()

    def test_folder_lock_table_evicts_old_unlocked_paths(self):
        """路径锁表超过上限时应优先淘汰未占用的旧锁。"""
        with patch.object(rclone_module, "_MAX_FOLDER_LOCKS", 2):
            first_lock = Rclone._Rclone__get_path_lock(Path("/A"))
            second_lock = Rclone._Rclone__get_path_lock(Path("/B"))
            third_lock = Rclone._Rclone__get_path_lock(Path("/C"))

        self.assertNotIn("/A", rclone_module._folder_locks)
        self.assertIn("/B", rclone_module._folder_locks)
        self.assertIn("/C", rclone_module._folder_locks)
        self.assertIsNot(first_lock, third_lock)
        self.assertIs(second_lock, rclone_module._folder_locks["/B"])
