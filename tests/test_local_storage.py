import errno
import os
import shutil
from pathlib import Path
from unittest.mock import patch

from app import schemas
from app.modules.localstorage import local as local_storage_module


SOURCE_MTIME_NS = 1_000_000_000_000_000_000


def _make_storage() -> local_storage_module.LocalStorage:
    """绕过配置初始化，构造仅用于文件操作测试的本地存储实例。"""
    return object.__new__(local_storage_module.LocalStorage)


def _default_file_mode(directory: Path) -> int:
    """获取当前进程在目标目录中新建文件时的默认权限。"""
    sentinel = directory / "mode-sentinel.bin"
    sentinel.write_bytes(b"")
    return sentinel.stat().st_mode & 0o777


def _prepare_source(source: Path, target_mode: int) -> int:
    """创建与目标默认权限不同的可读源文件，并设置固定修改时间。"""
    source.write_bytes(b"moviepilot-acl-test")
    source_mode = 0o600 if target_mode != 0o600 else 0o640
    source.chmod(source_mode)
    os.utime(source, ns=(SOURCE_MTIME_NS, SOURCE_MTIME_NS))
    return source_mode


def _assert_copied_file(source_content: bytes, target: Path, target_mode: int) -> None:
    """校验复制结果保留内容和时间戳，同时沿用目标目录权限。"""
    assert target.stat().st_mode & 0o777 == target_mode
    assert target.stat().st_mtime_ns == SOURCE_MTIME_NS
    target.chmod(target_mode | 0o400)
    assert target.read_bytes() == source_content


def test_copy_with_progress_keeps_target_permissions(tmp_path: Path) -> None:
    """进度复制应保留时间戳，但不得用源权限覆盖目标目录赋予的权限。"""
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
    """普通复制应让新文件继承目标目录权限，并继续保留源文件时间戳。"""
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
    """跨盘移动降级为复制时应继承目标权限，成功后再删除源文件。"""
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
    """同盘移动应继续使用原子重命名，不复制文件或改变原有权限。"""
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
