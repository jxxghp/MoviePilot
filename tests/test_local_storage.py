"""
验证本地文件复制、移动及链接修复的权限、原子性与失败保护。
"""

import errno
import os
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from app import schemas
from app.modules.filemanager.storages import local as local_storage_module
from app.schemas.exception import StorageQueryError

SOURCE_MTIME_NS = 1_000_000_000_000_000_000


def _make_storage() -> local_storage_module.LocalStorage:
    """
    绕过配置初始化，构造仅用于文件操作测试的本地存储实例。
    """
    return object.__new__(local_storage_module.LocalStorage)


def _default_file_mode(directory: Path) -> int:
    """
    获取当前进程在目标目录中新建文件时的默认权限。
    """
    sentinel = directory / "mode-sentinel.bin"
    sentinel.write_bytes(b"")
    return sentinel.stat().st_mode & 0o777


def _prepare_source(source: Path, target_mode: int) -> int:
    """
    创建与目标默认权限不同的可读源文件，并设置固定修改时间。
    """
    source.write_bytes(b"moviepilot-acl-test")
    source_mode = 0o600 if target_mode != 0o600 else 0o640
    source.chmod(source_mode)
    os.utime(source, ns=(SOURCE_MTIME_NS, SOURCE_MTIME_NS))
    return source_mode


def _assert_copied_file(source_content: bytes, target: Path, target_mode: int) -> None:
    """
    校验复制结果保留内容和时间戳，同时沿用目标目录权限。
    """
    assert target.stat().st_mode & 0o777 == target_mode
    assert target.stat().st_mtime_ns == SOURCE_MTIME_NS
    target.chmod(target_mode | 0o400)
    assert target.read_bytes() == source_content


def test_copy_with_progress_keeps_target_permissions(tmp_path: Path) -> None:
    """
    进度复制应保留时间戳，但不得用源权限覆盖目标目录赋予的权限。
    """
    source = tmp_path / "progress-source.bin"
    target = tmp_path / "progress-target.bin"
    target_mode = _default_file_mode(tmp_path)
    source_mode = _prepare_source(source, target_mode)
    source_content = source.read_bytes()
    storage = _make_storage()

    with patch.object(
        local_storage_module,
        "transfer_process",
        return_value=lambda *_args, **_kwargs: None,
    ):
        result = storage._copy_with_progress(source, target)

    assert result is True
    assert target_mode != source_mode
    _assert_copied_file(source_content, target, target_mode)


def test_copy_keeps_target_permissions(tmp_path: Path) -> None:
    """
    普通复制应让新文件继承目标目录权限，并继续保留源文件时间戳。
    """
    source = tmp_path / "copy-source.bin"
    target = tmp_path / "copy-target.bin"
    target_mode = _default_file_mode(tmp_path)
    source_mode = _prepare_source(source, target_mode)
    source_content = source.read_bytes()
    storage = _make_storage()

    with patch.object(
        local_storage_module.LocalStorage,
        "_LocalStorage__should_show_progress",
        return_value=False,
    ):
        result = storage.copy(
            schemas.FileItem(path=source.as_posix()),
            tmp_path,
            target.name,
        )

    assert result is True
    assert target_mode != source_mode
    _assert_copied_file(source_content, target, target_mode)


def test_cross_device_move_keeps_target_permissions(tmp_path: Path) -> None:
    """
    跨盘移动降级为复制时应继承目标权限，成功后再删除源文件。
    """
    source = tmp_path / "cross-device-source.bin"
    target = tmp_path / "cross-device-target.bin"
    target_mode = _default_file_mode(tmp_path)
    source_mode = _prepare_source(source, target_mode)
    source_content = source.read_bytes()
    storage = _make_storage()

    real_replace = local_storage_module.os.replace

    def _exdev_for_source(src_path, dst_path, *args, **kwargs):
        """
        只让「源 → 最终目标」的直接移动以 EXDEV 失败；降级路径里
        「临时文件 → 目标」的替换仍需正常工作，否则整个移动都会失败。
        """
        if Path(src_path) == source:
            raise OSError(errno.EXDEV, "跨设备移动")
        return real_replace(src_path, dst_path, *args, **kwargs)

    with (
        patch.object(
            local_storage_module.LocalStorage,
            "_LocalStorage__should_show_progress",
            return_value=False,
        ),
        # move 现在显式用 os.replace 做同盘原子移动，EXDEV 失败才降级为
        # 「写临时名 → 替换」的复制路径，因此在这里注入跨设备错误
        patch.object(
            local_storage_module.os,
            "replace",
            side_effect=_exdev_for_source,
        ),
    ):
        result = storage.move(
            schemas.FileItem(path=source.as_posix()),
            tmp_path,
            target.name,
        )

    assert result is True
    assert not source.exists()
    assert target_mode != source_mode
    _assert_copied_file(source_content, target, target_mode)


def test_same_device_move_still_uses_rename(tmp_path: Path) -> None:
    """
    同盘移动应继续使用原子重命名，不复制文件或改变原有权限。
    """
    source = tmp_path / "same-device-source.bin"
    target = tmp_path / "same-device-target.bin"
    source.write_bytes(b"same-device")
    source.chmod(0o600)
    source_stat = source.stat()
    storage = _make_storage()

    with (
        patch.object(
            local_storage_module.LocalStorage,
            "_LocalStorage__should_show_progress",
            return_value=False,
        ),
        patch.object(
            storage,
            "_copy_with_target_permissions",
            side_effect=AssertionError("同盘移动不应复制文件"),
        ) as copy_mock,
    ):
        result = storage.move(
            schemas.FileItem(path=source.as_posix()),
            tmp_path,
            target.name,
        )

    assert result is True
    assert not source.exists()
    assert target.stat().st_ino == source_stat.st_ino
    assert target.stat().st_mode & 0o777 == 0o600
    copy_mock.assert_not_called()


def test_list_skips_subitems_that_disappear_before_stat(tmp_path: Path) -> None:
    """
    列举目录时子项在枚举后消失应被跳过，不能让整个目录浏览失败。
    """
    storage = _make_storage()
    (tmp_path / "keep.txt").write_bytes(b"keep")
    (tmp_path / "keepdir").mkdir()
    (tmp_path / "vanished.txt").write_bytes(b"gone")

    original_stat = local_storage_module.fsproxy.stat

    def _stat(path, *args, **kwargs):
        """
        模拟文件在目录枚举后、读取元数据之前被删除。
        """
        if Path(path).name == "vanished.txt":
            raise OSError(errno.ENOENT, "No such file or directory")
        return original_stat(path, *args, **kwargs)

    with patch.object(local_storage_module.fsproxy, "stat", side_effect=_stat):
        items = storage.list(schemas.FileItem(path=tmp_path.as_posix()))

    names = {item.name for item in items}
    assert "keep.txt" in names
    assert "keepdir" in names
    assert "vanished.txt" not in names


def test_softlink_failed_replacement_preserves_original_link(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    原子替换失败时，源文件和原链接均保留，已创建的私有链接须清理。
    """
    source, target = tmp_path / "download.mkv", tmp_path / "Movie.mkv"
    source.write_bytes(b"movie")
    original_link = tmp_path / "missing.mkv"
    target.symlink_to(original_link)

    def fail_replace(staged: Path, destination: Path) -> None:
        """
        验证新链接已创建、旧链接仍在，再模拟替换时被拒绝访问。
        """
        assert staged.is_symlink() and staged.resolve() == source.resolve()
        assert destination.readlink() == original_link
        raise PermissionError(errno.EACCES, "permission denied")

    monkeypatch.setattr(local_storage_module.os, "replace", fail_replace)

    assert not _make_storage().softlink(schemas.FileItem(path=source.as_posix()), target)
    assert source.read_bytes() == b"movie"
    assert target.is_symlink() and not target.exists()
    assert target.readlink() == original_link
    assert not list(tmp_path.glob(".*.mp"))


@pytest.mark.parametrize("new_target", ["file", "symlink"])
def test_softlink_repair_preserves_target_that_changes_during_staging(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_target: str,
) -> None:
    """
    暂存期间被其他任务替换的文件或有效链接，不能再被旧任务覆盖。
    """
    source, target = tmp_path / "download.mkv", tmp_path / "Movie.mkv"
    source.write_bytes(b"movie")
    target.symlink_to(tmp_path / "missing.mkv")
    replacement = tmp_path / "replacement.mkv"
    replacement.write_bytes(b"another-task")
    real_softlink = local_storage_module.SystemUtils.softlink

    def create_then_change_target(src: Path, dest: Path) -> tuple[int, str]:
        """
        创建私有链接后模拟另一个任务已更新最终目标。
        """
        assert dest != target
        result = real_softlink(src, dest)
        target.unlink()
        if new_target == "symlink":
            target.symlink_to(replacement)
        else:
            target.write_bytes(b"another-task")
        return result

    monkeypatch.setattr(local_storage_module.SystemUtils, "softlink", create_then_change_target)

    assert not _make_storage().softlink(schemas.FileItem(path=source.as_posix()), target)
    assert source.read_bytes() == b"movie"
    assert target.read_bytes() == b"another-task"
    assert target.is_symlink() is (new_target == "symlink")
    assert replacement.read_bytes() == b"another-task"
    assert not list(tmp_path.glob(".*.mp"))


@pytest.mark.parametrize("failed_query", [1, 2])
def test_softlink_repair_requires_confirmed_missing_target(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_query: int,
) -> None:
    """
    创建前或替换前无法确认目标状态时均应停止修复并保留原链接。
    """
    source, target = tmp_path / "download.mkv", tmp_path / "Movie.mkv"
    source.write_bytes(b"movie")
    original_link = tmp_path / "missing.mkv"
    target.symlink_to(original_link)
    storage = _make_storage()
    real_query = storage.get_item_strict
    queries = 0

    def query(path: Path) -> Optional[schemas.FileItem]:
        """
        在指定的严格查询阶段模拟挂载不可用。
        """
        nonlocal queries
        queries += 1
        if queries == failed_query:
            raise StorageQueryError("测试挂载不可用")
        return real_query(path)

    monkeypatch.setattr(storage, "get_item_strict", query)

    assert not storage.softlink(schemas.FileItem(path=source.as_posix()), target)
    assert queries == failed_query
    assert source.read_bytes() == b"movie"
    assert target.is_symlink() and not target.exists()
    assert target.readlink() == original_link
    assert not list(tmp_path.glob(".*.mp"))
